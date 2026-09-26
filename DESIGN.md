# psort — Design Specification

psort is a local photo curation tool. It takes the chaotic, date-dumped folders from your phones, removes duplicates, groups near-identical shots into *moments*, picks the best shot in each, and builds a clean, private, date-organized library on your PC. When you pick photos for a blog post, psort exports web-ready copies that plug into the existing blog workflow.

## 1. Constraints

1. **Private by default.** The whole library stays on your PC or OneDrive. Nothing is uploaded unless you explicitly pick it for a post.
2. **Raw photos are never modified.** psort only reads the inbox. It never renames, moves or deletes anything there.
3. **The library becomes the master copy.** Once a batch is curated and verified, you may delete it from the inbox. So psort copies **every** photo it keeps; it never uses links back to the inbox.
4. **No cost, no sudo.** Python 3.12 and pip-installable libraries only. Everything runs locally, with no cloud APIs.
5. **Idempotent.** Re-running any command is safe. Photos are identified by their **content**, not their path, so moving or renaming inbox folders doesn't cause reprocessing, and your review decisions survive re-runs.
6. **The blog stays unchanged in Phase 1.** The existing `blogupdate.html` → Cloudinary → GitHub Action → Turbify flow keeps working exactly as it does today (§8).
7. **Nothing from the inbox is lost.** Every file is copied somewhere:
   - photos → the library
   - iPhone Live Photo clips → beside their photo in the library, with the same name
   - videos → the parallel `videos/` tree (§5.8)
   - everything else, including helper files, documents and unreadable files → `unsorted_files/`, under its original folder path

   The only files not copied are OS caches that Windows and macOS rebuild themselves (`Thumbs.db`, `desktop.ini`, `.DS_Store`). `psort verify` checks a batch, or the whole inbox, before you delete anything.
8. **No spaces in names.** Every folder and file psort creates uses lowercase letters, digits, `-` and `_` only. Event names you type are converted: "Birthday Party" → `birthday-party`.

## 2. Folders

| Folder | Example location | Written by psort? | Contents |
|---|---|---|---|
| **Inbox** | `/mnt/d/psort-inbox/` (USB or Windows drive) | Never | Batches you drop in as subfolders, e.g. `2019-phone-dump/`, `sarah-iphone-2024/` |
| **Library** | `/mnt/c/Users/steve/OneDrive/Pictures/psort-library/` | Yes | The curated master library (§5.4) |
| **Videos** | `/mnt/c/Users/steve/OneDrive/photos/psort/videos/` (beside the library) | Yes | Videos, in the same year/day/event folders as the library (§5.8) |
| **Unsorted files** | `…/psort/unsorted_files/` (beside the library) | Yes | Every non-photo, non-video file, under its original inbox path with spaces turned into hyphens |
| **Highlights** | `…/psort/highlights/` (beside the library) | Yes | Web-size copies of your ★ favorites, kept in sync automatically (§5.9) |
| **Outbox** | `/mnt/c/Users/steve/Pictures/psort-outbox/` | Yes | Web-ready exports for blog posts, one folder per post |
| **State** | `~/.local/share/psort/` (inside WSL) | Yes | SQLite database and thumbnail/hash cache |

- All paths are set in `psort.toml`.
- **The state database lives inside WSL.** SQLite locks unreliably on Windows drives mounted in WSL, and the WSL filesystem is much faster. After each run, psort writes a JSON copy of the state to `<library>/.psort/manifest.json`, so the library holds its own record and the database can be rebuilt from it.
- **OneDrive:** fine for the library, where it doubles as a backup. Keep the inbox *outside* OneDrive. Otherwise OneDrive uploads raw photos you're about to delete, and "files on demand" placeholders can stall scans.
- **USB vs. Windows drive for the inbox:** both work through WSL at similar speed. Use whichever is convenient.

## 3. Photo Formats

- **JPEG:** Android phones and most cameras.
- **HEIC:** the iPhone's default format. It's smaller than JPEG at the same quality. It's read with `pillow-heif`.
- **Live Photos** are an iPhone HEIC still plus a short `.mov` clip with the same name. psort keeps the still and skips the clip.
- **PNG:** mostly screenshots. psort ingests them and flags them as screenshots (§5.1).
- **Format policy:**
  - The **library keeps the original file, byte-for-byte**, whether HEIC or JPEG. There's no quality loss and no information lost.
  - **Exports are always JPEG**, since the blog and browsers need it.
  - Windows can display HEIC after installing the free "HEIF Image Extensions" from the Microsoft Store.
- RAW formats (DNG, CR2) aren't handled now, but they fit later without redesign.

## 4. Pipeline

`psort run` runs all the stages in order. Each stage can also run on its own.

```
inbox ──ingest──► state DB ──cluster──► moments ──score──► best picks ──curate──► library
                                                                                     │
                                                         review UI ◄─────────────────┤
                                                                                     │
                                                         export ──► outbox ──► blogupdate.html
```

## 5. Stages

### 5.1 Ingest

- Scans the inbox recursively. For each image:
  - **Content hash** (SHA-256). This is the photo's identity. An exact duplicate, such as the same dump imported twice, is recorded as a *copy* of the original and never curated twice.
  - **Timestamp**, taken from the first of these that works:
    1. EXIF `DateTimeOriginal` (plus `OffsetTimeOriginal` when present)
    2. a date in the filename (`IMG_20260703_145633`, `PXL_20260703_145633123`, `20260703_145633`, `Screenshot_2026-07-03-…`)
    3. a date in a **folder name**, nearest folder first:
       - `2016-01-03 - Marathon` gives that day. The time is unknown, so it's set to noon.
       - `2016-04 - PhotoPass` gives only the month. Those photos go in `2016/2016-04_unknown-day/` and are listed for review.
       - Photos dated from a folder are never grouped into bursts or events, since they have no real time.
       - Photos ingested before this rule existed are re-dated automatically.
    4. the file's modified time, marked **date uncertain** so it shows up for review

    On the Undated page you can set an exact date and time for one photo, or **one date for all ticked photos**. The second option files them in that day's folder with the time unknown, so they're never grouped into bursts.
  - **Other data:** camera model, width and height, orientation, screenshot flag (PNG, no camera EXIF, or screenshot-style name).
- Records every skipped file (videos, unreadable files, unknown types), with its reason.

- **Safe to run while files are still being copied in:**
  - **What Windows does**, verified with Explorer-style and robocopy copies on this PC: during a copy the destination has its **full size from the start**, and its modified/changed time **updates every second**. When the copy completes, the modified time is set back to the original's.
  - So any file changed within `settle_seconds` (default 120, set under `[inbox]`) is skipped as **still arriving** and picked up on a later run. A finished copy carries its original date, so it's processed right away.
  - psort also re-checks each file after reading it. If it changed meanwhile, it's skipped rather than recorded.
  - If a partial file ever does slip through and later changes at the same path, psort removes the copies it made of the old version. It does this only when no other inbox file has that content.
  - Moving a finished batch into the inbox **on the same drive** is instant, and always safe.

### 5.2 Cluster (moments)

- A **moment** is a burst of near-identical shots.
- Photos from the same camera are sorted by time. Consecutive photos join the same moment when both are true:
  - they were taken within `burst_gap_seconds` of each other (default 10), and
  - their perceptual hashes are within `phash_threshold` (Hamming distance, default 10 of 64 bits).
- A photo with no near-duplicate is a moment of one.
- **Moment ID** = the content hash of its earliest photo. That keeps the ID stable when later batches add photos.

### 5.3 Score

These scores are only compared **within a moment**, so each photo is ranked against its siblings rather than against every photo ever taken.

| Signal | Method |
|---|---|
| Sharpness | Variance of the Laplacian on a downscaled grayscale image |
| Faces | OpenCV YuNet face detector (a small model file, downloaded once): number of faces, plus how sharp the face regions are |
| Exposure | Share of the histogram that's crushed to black or blown to white, and distance from mid-tones |

- The composite is a weighted sum, with weights set in `psort.toml`. The highest score becomes the **best** pick unless you've overridden it.
- **Visual duplicates:** the same picture saved more than once with different bytes, like two PhotoPass downloads, a re-compressed copy, or a resized "shared" copy. Exact duplicates are already skipped at ingest (§5.1). Within a moment, two photos count as copies when all of these hold:
  - they're within `duplicate_gap_seconds` (1) of each other
  - their perceptual hashes differ by at most `duplicate_phash_threshold` (2)
  - their exposure is the same
  - they're either the same size with sharpness within 10%, or the same shape at a different size (a resized copy)

  The sharpness test matters: a blurry burst frame looks identical at hash level, but it's a real alternative, not a copy. The copy with the most pixels, then the sharpest, then the largest file, stays in the moment. The others go to `_duplicates/<kept-name>/`. They never compete for best and never make a close call. Picking a copy as best counts as picking the copy that was kept.
- **Close calls:** when the runner-up scores within `close_call_margin` of the best (default 5%), the moment is flagged. `psort close-calls` lists them, and the review UI shows them first, so you only check the near ties. Once you pick a shot, the flag clears.
- Eyes-open and smile detection are future ideas.

### 5.4 Curate (library layout)

```
psort-library/
  2026/
    2026-07-02/                     ← a day with no named event
      20260702_174935.jpg
    2026-07-03_birthday-party/      ← a named event (§5.6)
      20260703_145633.heic          ← best shot of each moment, and single photos
      _alternates/
        20260703_145633/            ← the other shots from that moment
          20260703_145634.heic
      _duplicates/
        20260703_145633/            ← extra copies of the same picture (§5.3)
          20260703_145633_1.jpg
    2026-07-03_fireworks/           ← a second event on the same day
    2026-07-04_summer-trip/         ← a multi-day event: one folder per day
    2026-07-05_summer-trip/
  _undated/                         ← photos whose date is uncertain, until reviewed
  .psort/manifest.json
```

- **Filenames:** `YYYYMMDD_HHMMSS[_n].<ext>`. This matches the names the blog already uses. `_n` separates photos taken in the same second. The original path and filename are kept in the state database.
- **Alternates are kept, not deleted.** A later `psort prune` could remove them, and it would show you exactly what it will delete first.
- Photos are copied, and each copy is verified by hash.
- Choosing a different best shot in the review UI moves the files to match.

### 5.5 Verify (safe to delete)

- `psort verify` with no batch name checks the **whole inbox**.

- `psort verify <inbox-batch>` reports, for every file in a batch, either:
  - ✅ its content is in the library, or it's an exact duplicate of something in the library; or
  - ⚠️ it's **not** in the library: a skipped video, an unreadable file, and so on.
- Delete an inbox batch only when it's all ✅, or you accept the ⚠️ items.
- psort itself never deletes from the inbox.

### 5.6 Events

- **Suggestions:** `psort events` lists suggested events. A new one starts wherever shooting pauses for more than `gap_hours` (default 3). Each event's ID is its start time, like `20260703_145633`.
- **Naming:**
  - `psort events name <id> <name>` names one event.
  - Add `--through <id>` to name a multi-day trip in one go.
  - Folders are renamed immediately.
- **How it works:**
  - A named event is stored as a **time range**. Photos added later that fall inside it join the event automatically.
  - Named ranges can't overlap. Naming again with the same name replaces its range.
  - `psort events unname <name>` puts the plain date folders back.
- Days with no named event keep plain `YYYY-MM-DD` folders.

### 5.7 Faces

- **Scan:** after curate, `psort run` finds faces in each library photo with YuNet. It stores a 128-number **embedding** (a face "fingerprint") for each, using OpenCV's SFace model (38 MB, downloaded once). Faces smaller than 32px at analysis size are ignored as too blurry to recognize.
- **Grouping:** unnamed faces are linked to their nearest look-alikes and grouped. Each group is named after its smallest face ID.
- **Naming:**
  - `psort faces crops` writes thumbnails to `~/.local/share/psort/faces/`, one folder per person and per unnamed group. Browse them in File Explorer at `\\wsl.localhost\...`.
  - `psort faces label <name> --group <id>` names a group. `--face <id>` names individual faces.
  - Similar faces then get the name automatically, marked `auto`.
  - `psort faces unlabel --face <id>` fixes a wrong one.
- **Privacy:**
  - Embeddings are biometric data. They stay **only** in the state database inside WSL.
  - The library manifest records just the names of the people in each photo.
  - Nothing is uploaded, and exports never include face data.
- **Uses:**
  - now: "who's in this photo" in the manifest
  - planned: people filters in the review UI, a warning before publishing a photo that shows your daughter, and optionally favoring shots where family faces are sharp
- Dogs' faces are often detected too, so pets can be named the same way.

### 5.8 Videos

- **Formats:** phone and camera formats, including `.mp4 .mov .m4v .3gp .avi .mpg .mpeg .mts .m2ts .mod .tod .vob .wmv .mkv .webm`.
- **Copying and layout:**
  - Videos are copied and verified like photos, and exact duplicates are skipped.
  - They're filed in `videos/` under the **same folder names** the library uses for that date: `YYYY/YYYY-MM-DD[_event]/`, `YYYY-MM_unknown-day/` or `_undated/`.
  - Naming an event renames the folder in both trees.
- **Dates**, from the first of these that works:
  1. the MP4/MOV creation time (stored in UTC, shown in local time)
  2. the date inside the video's `.THM` companion file, which older cameras like your Sony write
  3. the filename
  4. the folder name
  5. the file's modified time
- **Live Photo clips:** a `.mov`/`.mp4` of 5 seconds or less, sitting beside a same-named HEIC or JPEG, is a Live Photo clip.
  - It's copied into the **library beside its photo, with the same name**, like `20260705_120000.heic` + `20260705_120000.mov`.
  - It moves whenever the photo moves (best pick, events).
  - The review UI marks the photo ◉ Live and can play the clip.
- **Helper files:** `.THM` (camera video preview) and `.AAE` (iPhone edit settings) files are copied to `unsorted_files/` with everything else. A `.THM` also supplies the date for its video.
- **Review UI:**
  - Each day or event page has a 🎬 section with a still frame, the video's length, and its Windows path.
  - MP4, MOV and WebM files play in the page.
  - A **Videos** page lists every video by folder.
  - The Library page marks folders that contain videos.
- Videos picked up before this feature existed (previously "unsupported") are ingested on the next run.

### 5.9 Favorites and highlights

- **Favorites:** click ☆ on any photo card or details page to star it. The **★ Favorites** page lists them newest first and can filter by year and person.
- **The `highlights/` folder:** psort keeps a web-size JPEG of every favorite here: 2048px, upright, about 0.5 MB, with GPS stripped. It's browsable in File Explorer, Windows Photos, or on a phone through OneDrive, without keeping a second full-size copy.
- **Finding the original:**
  - A highlight uses the **same folder path and filename** as its original in the library, dropping any `_alternates` or `_duplicates` level. For example, `highlights/2016/2016-04-17/20160417_062036.jpg` ↔ `a_library/2016/2016-04-17/20160417_062036.jpg`.
  - In Windows Properties, its **Title/Subject** is `psort library: <original's path>`.
  - Its **Tags** are the people in it plus your psort tags, so File Explorer can search by person.
- **Always up to date:**
  - un-favoriting deletes the copy and any empty folders
  - moving the original (best pick, event name, date fix) moves the copy
  - changing people or tags re-renders it
  - a copy deleted by accident is rewritten on the next `psort run`
- psort only touches files it wrote there. Anything else you put in `highlights/` is left alone.

### 5.10 Deleting photos

- **How to delete:**
  - **🗑 Delete** on a photo's details page, or tick photos on a day page and click **Delete ticked**.
  - The photo and its Live Photo clip move to `library/_trash/<same path>`, and the photo disappears from every view, pick, favorite and tray.
  - If you delete a moment's best shot, the next best takes its place.
- **Deleted photos stay deleted:** psort remembers each one, so its inbox copy is **never copied back**. `verify` counts it as safe to delete ("you deleted this photo").
- **Trash page:** **Restore** puts a photo back in its folder, with faces re-found on the next run. **Empty trash**, or `psort empty-trash`, deletes the files for good. psort still remembers them.

### 5.11 Reconcile

`psort reconcile` catches up with changes made by hand in the library, `videos/` or `unsorted_files/`. It only reports unless you add `--apply`:
- **Moved or renamed files** (including whole renamed folders) are found by their contents and adopted. psort then files them back in its usual place, **without re-copying** from the inbox and without making duplicates.
- **Photos whose files are gone everywhere** are listed. With `--apply` they're recorded as deleted for good, so they're never copied back.
- **Files psort didn't put there** are listed and never touched.

To change *where* something lives, use psort: best pick, events or dates. Its layout always follows those.

## 6. Review UI

- **`psort review`** starts a local Flask app at `http://localhost:5000`. It listens on 127.0.0.1 only and has no login. It refuses requests whose Host isn't `localhost`/`127.0.0.1`, and every change needs a token that's created fresh each time the app starts. Together these stop a web page elsewhere from driving it.
- **Pages:**
  - **Library:** years → days and events, with moment counts, close calls, and a ✓ for reviewed days.
  - **Day or event:** a grid of best shots, with badges for shot count, close call, in tray, people and tags. There's a "Mark day reviewed" button.
  - **Moment:** every shot with its score breakdown, and:
    - **Make this the best**, which sticks, plus "let psort pick again"
    - add to the post tray
    - tags
    - fix date
  - **Close calls:** each near-tie moment side by side, with a "Pick this" button per shot.
  - **Events:** suggested events with name and "through" fields, plus unname.
  - **Faces:** unnamed groups as face thumbnails. Untick any face that doesn't belong, then name the whole group or just the ticked faces. Each person has a page with a "Not <name>" button for mistakes.
  - **Undated:** enter the real date, and the photo moves into its day folder and gets a name from that date.
  - **Post tray:** photos picked for the next post, for export (§7).
- Every decision updates the library right away: files move, folders are renamed, and the manifest is rewritten.
- **Face rejections:** unticking a face or clicking "Not <name>" is remembered. That face is never auto-matched to that person again, unless you name it that person yourself.
- **Appearance:** dark mode is on by default. The ☀/☾ button switches to light, and each browser remembers the choice.
- **Thumbnails** (320px and 1280px, including HEIC converted to JPEG) and face crops are cached in the state folder. The files are named by photo content, so they never go stale.

## 7. Export for a Post

- **Two ways to export:**
  - On the **Post tray** page, type a post name and click **Export**.
  - Or run `psort export <post-name>`. Add library names to export specific photos without using the tray.
- Photos are written to `<outbox>/<post-slug>/`. The post name becomes a slug, like `go-dogs-go`, so there are no spaces.
- **Each exported photo:**
  - is a **JPEG** at quality 85, with HEIC converted
  - is **rotated upright**, with the rotation built into the pixels. That's usually the cause of sideways blog photos, so `rotate_pic.sh` should rarely be needed.
  - is resized to at most **2048px** on the long edge, the blog's largest size. Smaller photos are never enlarged.
  - is flattened onto white if it has a transparent background (screenshots)
  - keeps only an **allowlist** of metadata: camera make and model, the date taken, and its time zone. **GPS, serial numbers, owner and lens info, maker notes, embedded thumbnails, XMP and comments are all dropped.** The color profile is kept so colors stay right.
  - keeps its library filename (`20260703_145633.jpg`), so the blog's naming stays consistent
- **After export:**
  - Exported photos leave the tray, unless you use `--keep-tray` on the command line.
  - Each export is recorded, and photo cards show "posted in: …".
- **People warning:** the tray page lists who's in the photos before you export, since blog posts are public.

## 8. Blog Integration

### Phase 1: no blog changes (MVP)

1. In the review UI, add photos to a post and click **Export**. Or run `psort export go-dogs-go`.
2. Open `blogupdate.html` as you do today. In each image block, the Cloudinary widget's file picker opens the outbox folder.
3. Everything after that is unchanged: Cloudinary → `process-blog-post.yml` makes the 640–2048 sizes → uploads to Turbify `pics/blog/` → commits the post.

### Phase 2: direct publishing (built)

The **Post tray** page is a composer:
- **Post settings:**
  - new post, or add to an existing one
  - title, and a date that defaults to the first photo's day
  - author, chosen from `_authors`
  - categories and tags, with suggestions from your existing posts
- **Content:**
  - opening text
  - each photo with a **text-before** box and an **alt-text** box, reorderable with ↑/↓
  - YouTube ID, closing text, and a quote with attribution
- **Save draft**, **Preview post** (the exact `.md` file), **Dry run** (builds everything locally, uploads and commits nothing), and **Publish to blog**.

Publish does the following:
1. **Images.** For each photo: the web copy (upright, ≤2048px, GPS-free) plus the blog's responsive sizes 2048/1920/1600/1366/1024/768/640, sized by width and never enlarged. These match `create_responsive_images.py`.
2. **Upload** over **FTP with TLS** (explicit FTPS, certificate verified) to `cpanel292.turbify.biz`, into `pics/blog/` and `pics/blog/<size>/`.
   - Turbify's certificate names that server, not `ftp.itsallonesong.com`.
   - Files already there are skipped.
   - A *different* file with the same name is never overwritten; the publish stops before anything is committed.
3. **Post.** Written in exactly the format `process-blog-post.yml` produces, as `YYYY-MM-DD-<title-slug>.md`, or appended to the chosen post.
4. **Git.** `git pull --ff-only` first, so phone posts don't conflict. Then psort commits **only that post file** and pushes. Your deploy workflow rebuilds the site.
5. **Link.** The page shows the post's URL: `/<categories>/YYYY/MM/DD/<slug>.html`. The photos are recorded as posted, and the tray and draft are cleared.

**Setup:** `psort blog-login` tests the login, confirms `pics/blog/1024` has photos, and saves the settings.
- Settings go in `[blog]` in `psort.toml`.
- The password goes in `~/.config/psort/secrets.toml`, which is readable only by you. psort refuses to use it if other users can read it.

The old route (Export only → outbox → `blogupdate.html`) still works.

### Out of scope

- Uploading the whole library to Turbify, which was the original draft's "sync everything with lftp"
- Jekyll YAML metadata for every photo. The blog doesn't use it, and it would reveal the library's structure.

## 9. Commands

| Command | Does |
|---|---|
| `psort init` | Creates `psort.toml` and the state database, and checks that the folders are reachable |
| `psort run [--dry-run]` | Ingest → cluster → score → curate |
| `psort ingest` / `cluster` / `score` / `curate` | Runs one stage |
| `psort status` | Totals: photos, moments, duplicates, skipped files, undated photos, unreviewed days |
| `psort verify <batch>` | Safe-to-delete report (§5.5) |
| `psort close-calls` | Moments where the automatic best pick was a near tie |
| `psort events` / `events name` / `events unname` | Suggested events, and naming them (§5.6) |
| `psort faces list` / `crops` / `label` / `unlabel` / `scan` | Face groups and people (§5.7) |
| `psort fetch-models` | Downloads the face models if they're missing |
| `psort reconcile [--apply]` | Catches up with files moved, renamed or deleted by hand (§5.11) |
| `psort empty-trash` | Permanently deletes trashed photos (§5.10) |
| `psort blog-login` | Tests and saves the Turbify FTP login for publishing (§8) |
| `psort review` | Starts the review UI |
| `psort export <slug> [files…]` | Exports for a post (§7) |
| `psort rebuild-state` | Rebuilds the database from `library/.psort/manifest.json` |

- `--dry-run` shows what *would* be copied or moved without touching the library.

## 10. Tech Stack

- **Python 3.12**, installed as a package with `pyproject.toml`. It runs in a virtualenv, and the CLI is built with `typer`.
- **Libraries:**
  - `Pillow` and `pillow-heif` for images, EXIF and HEIC
  - `imagehash` for perceptual hashes
  - `opencv-python-headless` for sharpness, exposure and the YuNet face detector
  - `Flask` for the review UI
  - `sqlite3` from the standard library
- **Tests:** `pytest`, using a small set of test photos: a burst, an exact duplicate, a HEIC file, a rotated photo, a photo with GPS data, one with no EXIF, and a video.
- No system packages are required. `exiftool` and `lftp` from the original draft aren't needed.

## 11. Decisions

- **Library size:** you'll make sure OneDrive has room.
- **Events:** event names go in the day folder names (option A), as in §5.6.
- **Faces:** recognition is included (§5.7).
- **Close calls:** they're flagged for review (§5.3).

# psort — Design Specification

psort is a local photo curation tool. It takes the chaotic, date-dumped folders from your phones, removes duplicates, groups near-identical shots into *moments*, picks the best shot in each, and builds a clean, private, date-organized library on your PC. From there you can star favorites, and publish chosen photos straight to the Jekyll blog at blog.itsallonesong.com.

> **Picking this project up later, or handing it to another agent?** Start with **§12 Status & Handoff**. It covers what's built, what's pending, the user's environment, a code map, the data model, and known gotchas. `CLAUDE.md` has the short version for coding agents.

## 1. Constraints

1. **Private by default.** The library stays on the PC and OneDrive. Nothing is uploaded unless you publish it to the blog.
2. **Inbox files are never modified.** psort only reads the inbox. It never renames, moves or deletes anything there.
3. **The library becomes the master copy.** Once `psort verify` says a batch is safe, you may delete it from the inbox. So psort copies files; it never links back to the inbox.
4. **No cost, no sudo.** Python 3.12 and pip-installable libraries only, managed with `uv`. Everything runs locally, with no cloud APIs.
5. **Idempotent.** Re-running is always safe. Files are identified by their **content** (SHA-256), not their path, so moving or renaming inbox folders doesn't cause reprocessing, and review decisions survive re-runs.
6. **Nothing from the inbox is lost.** Every file is copied somewhere (§5.1):
   - photos → the library
   - Live Photo clips and Rich Capture packages → beside their photo in the library
   - videos → the parallel `videos/` tree
   - everything else → `unsorted_files/`

   The only files not copied are OS caches that Windows and macOS rebuild themselves (`Thumbs.db`, `desktop.ini`, `.DS_Store`).
7. **No spaces in names.** Every folder and file psort creates uses lowercase letters, digits, `-` and `_` only. Names you type, such as event names, are converted: "Birthday Party" → `birthday-party`.
8. **The existing blog flow keeps working.** `blogupdate.html` → Cloudinary → GitHub Action → Turbify is untouched. psort's direct publishing (§8) is an alternative, not a replacement.

## 2. Folders

The user's actual locations are set in `~/.config/psort/psort.toml`:

| Folder | Location (WSL path) | Written by psort? | Contents |
|---|---|---|---|
| **Inbox** | `/mnt/c/shared/media/psort_inbox` (outside OneDrive) | Never | Batches dropped in as subfolders of any shape and depth |
| **Library** | `/mnt/c/Users/steve/OneDrive/photos/psort/a_library` | Yes | The curated master library (§5.4) |
| **Videos** | `…/OneDrive/photos/psort/videos` | Yes | Videos, in the same year/day/event folders as the library (§5.8) |
| **Unsorted files** | `…/OneDrive/photos/psort/unsorted_files` | Yes | Every other file, under its original inbox path with spaces → hyphens |
| **Highlights** | `…/OneDrive/photos/psort/highlights` | Yes | Web-size copies of ★ favorites, kept in sync (§5.9) |
| **Outbox** | `/mnt/c/shared/media/psorted_outbox` | Yes | "Export only" copies for `blogupdate.html` (§7) |
| **State** | `~/.local/share/psort/` (inside WSL) | Yes | SQLite database, thumbnail and poster caches, face models |

- **The state database lives inside WSL.** SQLite locks unreliably on Windows drives mounted in WSL, and the WSL filesystem is much faster. After changes, psort writes a JSON copy of the state to `<library>/.psort/manifest.json`, so the library documents itself. Face embeddings are deliberately excluded (§5.7).
- **Moving a folder:** move it in File Explorer, then edit its line in `psort.toml`. Inbox records are stored relative to the inbox, and library records relative to each root, so nothing is reprocessed. Don't re-run `psort init` for this; `init --force` would rewrite the whole config.

## 3. File Types

| Type | Handling |
|---|---|
| **JPEG, HEIC/HEIF, PNG** | Photos. HEIC is read with `pillow-heif`. The **library keeps each original byte-for-byte**, and exports are always JPEG. PNG screenshots are flagged. |
| **iPhone Live Photo clip** | A `.mov`/`.mp4` of 5 seconds or less beside a same-named HEIC or JPEG. It's kept beside its photo, with the same name (§5.8). |
| **Nokia Lumia Rich Capture `.nar`** | A ZIP of flash, no-flash and blend frames behind a `_Rich.jpg`. The package is kept beside its photo, and each frame becomes a photo (§5.12). |
| **Videos** | `.mp4 .mov .m4v .3gp .avi .mpg .mpeg .mts .m2ts .mod .tod .vob .wmv .mkv .webm .flv .dv`: copied to `videos/` (§5.8) |
| **Helpers** | `.THM` (camera video preview, which also dates its video) and `.AAE` (iPhone edits): copied to `unsorted_files/` |
| **Anything else** | Documents, `.picasa.ini`, unreadable or corrupt files: copied to `unsorted_files/` |

- **Very large images** (up to 300 MP) are allowed, raised from Pillow's "decompression bomb" guard, for posters and panoramas.
- **Orientation:** psort applies the EXIF orientation itself instead of using Pillow's `exif_transpose`, which crashes on some real-world metadata, such as Windows Phone photos.
- RAW formats (DNG, CR2) aren't handled yet. They would currently go to `unsorted_files`.

## 4. Pipeline

`psort run` runs six steps and shows progress for each (§4.1). Most steps can also run on their own.

```
inbox ─[1 scan]─► state DB ─[2 group moments]─► ─[3 score]─► ─[4 arrange library]─► library / videos / unsorted_files
                                                                                          │
                                          ─[5 find faces]─► ─[6 update highlights]─► highlights/
                                                                                          │
                     review UI (localhost:5000): pick, name, date, delete, favorite, compose posts
                                                                                          │
                                             publish ─► Turbify (FTPS) + blog repo (git push)
```

### 4.1 Progress
- **In a terminal,** a single line updates in place, with a spinner:
  ```
  ⠹ [1/6] Scanning inbox  3,412 / 7,512 files  45%  ·  overall 23% · 4m12s elapsed, about 11m left
  ```
- **Something always shows.** The first line appears immediately ("looking at the inbox…", with a running file count). The spinner is driven by its own timer, so it keeps turning even while psort waits on a slow disk or a big file: a heartbeat that shows it's alive.
- **When output is redirected,** it prints a line every 10%, plus "… still working on <step> (elapsed)" after 30 seconds of silence.
- Listing the inbox checks each file once, and the scan reuses those results. On the user's inbox (~9,700 files on `/mnt/c`), listing alone takes about 75 s.
- **The overall percentage and time left** are weighted by this run's actual work, estimated up front: new files, photos needing a face scan, and so on. The faces step is re-estimated once its exact count is known.
- **Speed:** about 0.5 s per new photo (analysis plus faces). A 5,000-photo batch takes roughly 40 minutes. Ctrl+C is safe, and the next run resumes.

## 5. Stages

### 5.1 Scan (ingest)

- Walks the inbox recursively, at any depth, and records **every** file in `sources`: its path relative to the inbox, size, mtime, status and content hash.
- **For each photo:**
  - **Content hash (SHA-256).** This is its identity. An exact duplicate is recorded but curated only once.
  - **Capture time**, from the first of these that works:
    1. EXIF `DateTimeOriginal` (+ `OffsetTimeOriginal`)
    2. a date in the filename: `IMG_20260703_145633`, `PXL_…`, `20260703_145633`, `Screenshot_2026-07-03-…`, `WP_20151004_06_56_36`
    3. a date in a **folder name**, nearest folder first. `2016-01-03 - Marathon` gives that day (`folder`); `2016-04 - PhotoPass` gives only the month (`folder-month`).
    4. the file's modified time (`mtime`), which counts as **undated**
  - **Analysis:** camera, upright size, perceptual hash, sharpness, exposure, and face count/sharpness (YuNet), all computed on a ≤1024px copy.
- **Date confidence:**
  - `exif`, `filename`, `meta` and `user` have a real time.
  - `folder` and `user-day` give a day with the time unknown.
  - `folder-month` gives a month only.
  - `mtime` is a guess.
  - Anything without a real time is never grouped into bursts or events.
  - `folder-month` and `mtime` photos are listed on the Undated page.
- **Deleted photos** (§5.10) seen again in the inbox are not copied back.
- **Unreadable files** are copied to `unsorted_files`, and **retried on every run**. If a retry succeeds, the photo goes into the library and its unsorted copy is removed.
- **Safe while files are still being copied in:**
  - **Verified Windows behavior on this PC:** a copy's destination has its full size from the start, and its mtime/ctime update every second. When the copy completes, the original mtime is restored.
  - Files changed within `settle_seconds` (default 120, `[inbox]`) are skipped as "still arriving."
  - Each file is re-checked after reading. One that changed is left for the next run.
  - If a file at a known path changes content, psort's copies of the old version are removed, but only if no other inbox file has that content.

### 5.2 Group moments (cluster)

- A **moment** is a burst of near-identical shots from one camera. Consecutive photos join when they're taken within `burst_gap_seconds` (10) of each other **and** their perceptual hashes are within `phash_threshold` (10 of 64 bits).
- **Moment ID** = the content hash of its earliest photo, which stays stable as later batches add photos.

### 5.3 Score

Scores are compared only **within a moment**. Weights live in `[weights]`:

| Signal | Method |
|---|---|
| Sharpness (0.5) | Variance of the Laplacian on a downscaled grayscale image |
| Exposure (0.2) | Clipped shadows and highlights, and distance from mid-tone |
| Faces (0.15) | Number of faces found by YuNet |
| Face sharpness (0.15) | Sharpness of the face regions |

- The highest score is the **best** shot unless you've picked another. Your pick always wins.
- **Close calls:** when the runner-up is within `close_call_margin` (5%) of the best, the moment is flagged for review. The flag clears once you pick.
- **Visual duplicates:** the same picture saved again with different bytes (re-downloads, re-compressions, resized shares). Two photos count as copies when all of these hold:
  - they're within 1 s of each other, with a hash distance of 2 or less and the same exposure
  - they're either the same size with sharpness within 10%, or the same shape at a different size

  The sharpness test keeps blurry burst frames as real alternates. The best-quality copy (most pixels, then sharpest, then largest file) stays. The others go to `_duplicates/` and never compete for best.

### 5.4 Arrange library (curate)

```
a_library/
  2016/
    2016-04-15/                        ← a day with no named event
      20160415_125618.jpg              ← best shot of each moment, and single photos
      20160415_125618.mov              ← its Live Photo clip (same name)
      _alternates/20160415_125618/…    ← the other shots of that moment
      _duplicates/20160415_125618/…    ← extra copies of the same picture
    2016-04-17_disney/                 ← a named event (§5.6); multi-day events repeat the name per day
    2016-04_unknown-day/               ← photos dated only to the month
  2015/2015-10-04/20151004_065637.jpg + .nar   ← Rich Capture package beside its photo
  _undated/                            ← photos with no date at all
  _trash/…                             ← deleted photos, restorable (§5.10)
  .psort/manifest.json
```

- **Filenames:** `YYYYMMDD_HHMMSS[_n].<ext>`, assigned once and kept. This matches the blog's naming. A name changes only if you fix a photo's date before publishing it.
- Every copy is **verified by hash**. Moves (best-pick changes, events, dates) are renames within the library.
- **The layout is psort's.** Don't rearrange it by hand. If you do, `psort reconcile` (§5.11) repairs psort's records.

### 5.5 Verify (safe to delete)

- `psort verify [batch]` checks a batch, or with no argument **the whole inbox**. Each file is:
  - ✅ copied into the library, videos or unsorted files; a duplicate of something already copied; an OS cache file; or a photo you deleted on purpose, or
  - ⚠️ not ingested yet (or still arriving), changed since ingest, or not copied yet.
- It exits with status 2 if anything is ⚠️. psort itself never deletes from the inbox.

### 5.6 Events

- **Suggestions:** `psort events` (or the Events page) suggests events wherever shooting pauses for more than `gap_hours` (3). Each event's ID is its start time.
- **Naming:** name an event, adding `--through <id>` for multi-day trips.
  - A named event is a **time range**: photos with a real time inside it go in `YYYY-MM-DD_<slug>` folders, in both the library and `videos/`.
  - Photos added later that fall inside it join automatically.
  - Ranges can't overlap. **Unname** restores plain date folders.

### 5.7 Faces

- **Scan:** after arranging, each new library photo is scanned. YuNet detects faces (≥32px at analysis size), and **SFace** turns each into a 128-number embedding. The models are downloaded once into the state folder.
- **Grouping:** unnamed faces are linked to their 10 nearest look-alikes above `cluster_threshold` (0.5), but **only when the link is mutual** (each is among the other's nearest). Groups are the connected components, named after their smallest face ID.
  - One-way links let a face that resembles two people chain them together. On the user's library, the old rule produced one mixed group of 3,558 faces. With mutual links, the largest groups are 548 and 372, and there are 480 real groups instead of 203.
- **Naming:**
  - Group faces are listed **most typical first** (closest to the group's average face), so the odd ones out come last.
  - **Faces page:** shows 8 faces per group. **Name whole group** appears only when the whole group is visible; bigger groups get **See all N faces**.
  - **Group page:** shows up to 400 faces with tick/untick-all. **Name ticked faces** names only those; unticked faces stay undecided, with no rejection recorded.
  - **"Name ticked only" on the Faces page** records the unticked *shown* faces as rejections.
  - Faces above `match_threshold` (0.45) similarity to a named person get that name automatically (`auto`). Those are listed first, least certain first, on the person's page.
  - Unticked faces and "Not <name>" are stored as **rejections**, so they're never auto-matched to that person again.
- **Privacy:** embeddings stay **only** in the WSL database. The manifest and highlight copies carry only people's names.
- **Uses:** "who's in this photo," the Favorites person filter, Windows Tags on highlights, and the post tray's "these show people" warning.
- Pets' faces are often detected too.

### 5.8 Videos and Live Photo clips

- **Videos** are copied and verified into `videos/`, under the **same folder names** the library uses for their date. Event names apply to both trees.
- **Video dates**, from the first that works:
  1. MP4/MOV creation time (UTC → local)
  2. the `.THM` companion's EXIF (older cameras, e.g. a Sony DSC-T700)
  3. the filename
  4. the folder name
  5. the file's mtime
- **Live Photo clips** are kept **beside their photo** with the same name. They follow it (best pick, events, trash), and the review UI marks the photo ◉ Live and can play the clip.
- **Review UI:** each day page has a 🎬 section with a still frame, the length, in-page playback (for MP4/MOV/WebM) and the Windows path. A **Videos** page lists them all.

### 5.9 Favorites and highlights

- **Favorites:** ☆/★ on any photo. The **★ Favorites** page filters by year and person.
- **`highlights/`** holds a 2048px, upright, GPS-free JPEG of each favorite, at the **same folder path and filename** as its original (without any `_alternates`/`_duplicates` level). Each copy's Windows **Title/Subject** is `psort library: <original path>`, and its **Tags** are people plus psort tags.
- **Kept in sync:**
  - un-favoriting removes the copy
  - moving the original moves it
  - changing people or tags re-renders it
  - a missing copy is restored on the next run
- psort touches only files it wrote there.

### 5.10 Deleting photos

- **🗑 Delete** on a photo's page, or **Delete ticked** on a day page. The photo, plus its Live Photo clip and any Rich Capture package it hosts, moves to `library/_trash/`, and the photo leaves every view.
- If you delete a moment's best shot, the next best takes over.
- **Deleted photos stay deleted:** they're remembered in `deleted_photos`, so the inbox copy is never copied back, and `verify` counts it as safe.
- **Trash page:** **Restore** puts a photo back (faces are re-found on the next run). **Empty trash**, or `psort empty-trash`, deletes the files for good, while psort still remembers them.

### 5.11 Reconcile

`psort reconcile` (report only), or `--apply`, catches up with changes made by hand in the library, `videos/` or `unsorted_files/`:
- **Moved or renamed files**, including renamed folders, are found by their contents and adopted, then put back in psort's layout without re-copying.
- **Photos whose files are gone** are recorded as deleted for good.
- **Files psort didn't create** are listed and never touched.

### 5.12 Nokia Lumia Rich Capture (`.nar`)

- A Lumia saved `WP_…_Rich.jpg` (the finished blend) plus `WP_…_Rich.nar`: a ZIP of `Extra1.jpg` (no flash), `Reference.jpg` (flash) and `FnF.jpg` (flash + no-flash blend), plus `content.xml` and `richsettings.xml`.
- **The package** is kept **beside its finished photo**, with the same name and a `.nar` extension, and follows it. If the finished photo is missing, or deleted while its frames remain, it sits beside the flash frame.
- **Each frame becomes a photo** (with its own EXIF date and camera) in the same moment as the finished photo, so the best shot wins. Frames are copied straight out of the `.nar` in the inbox; no extracted copies are kept elsewhere.
- **Review UI badges:** ◈ Rich, and ◈ flash / no flash / blend frame.
- The user has 667 packages (about 8.7 GB); 3 have no finished photo.

## 6. Review UI

- **`psort review`** starts Flask on **127.0.0.1:5000**, with no login. Requests whose Host isn't localhost are refused, and every change needs a per-launch token (a Jinja global, so imported macros see it). Dark mode is the default, with a light toggle remembered per browser.
- **Pages:**
  - **Library:** years → days and events, with counts, close calls, 🎬 and ✓ reviewed.
  - **Day/event:** best shots with badges (shots, duplicates, close call, ◉ Live, ◈ Rich, in tray, people, tags, posted in), ☆ favorite, tick → **Delete ticked**, 🎬 videos, and **Mark day reviewed**.
  - **Moment:** every shot with its score breakdown:
    - **Make this the best** / let psort pick again
    - ☆, tray, tags, fix date, 🗑 delete
    - play the Live clip
  - **★ Favorites** (year/person filters) · **Close calls** ("Pick this") · **Events** (name/through/unname) · **Faces** (name groups; person pages with "Not <name>") · **Videos** · **Undated** (exact date per photo, or **one date for all ticked**) · **🗑 Trash** (Restore / Empty) · **Post tray** (the composer, §8)
- Every decision updates the library right away (files move, folders rename), along with highlights.
- **Clicks stay fast:** a decision re-scores in memory and moves only the files it affects. Files that aren't moving are trusted from the database rather than re-checked on disk. Checking all ~10,000 library files on OneDrive took about 2 minutes per click; now a click takes about 0.2 s. `psort run` still verifies every file.
- **`manifest.json`** (about 10 MB) is written in the background 4 s after the last change, and again when the review page stops.
- **Thumbnails, posters and face crops** are cached in the state folder, named by content, so they're never stale.

## 7. Export only (for `blogupdate.html`)

**Post tray → Export only**, or `psort export <post>`, writes `<outbox>/<post-slug>/*.jpg`. Each file is:
- upright (rotation built in) and at most 2048px (never enlarged)
- flattened onto white if transparent, with HEIC converted
- limited to allowlisted metadata only (make, model, date and time zone). GPS, serial numbers, maker notes, XMP and comments are dropped; the ICC profile is kept.

Exports are recorded ("posted in: …").

## 8. Publishing to the blog

The **Post tray** page is a composer:
- new post or add to an existing one
- title, and a date that defaults to the **first photo's day**
- author (from `_authors`), plus categories and tags (suggested from existing posts)
- opening text, then each photo with **text-before** and **alt text** (↑/↓ to reorder), YouTube ID, closing text, and a quote with attribution
- **Save draft · Preview post · Dry run · Publish to blog**

**Publish** (pushes immediately, as the user chose):
1. **Images:** the web copy of each photo (upright, ≤2048, GPS-free) plus sizes 2048/1920/1600/1366/1024/768/640, by width and never enlarged. These match the blog repo's `scripts/create_responsive_images.py`.
2. **Upload** over **explicit FTPS (TLS, certificate verified)** to `pics/blog/<name>.jpg` and `pics/blog/<size>/<name>-<size>.jpg`.
   - Identical files are skipped.
   - A **different** file with the same name aborts the publish **before anything is committed**.
3. **Post file:** written in exactly the format of `.github/workflows/process-blog-post.yml`, as `YYYY-MM-DD-<slug>.md` (or appended to the chosen post).
4. **Git:** `git pull --ff-only`, then commit **only that post file**, then push. The blog's deploy workflow rebuilds GitHub Pages in about 2 minutes.
5. **Link:** the page shows the URL, `https://blog.itsallonesong.com/<categories>/YYYY/MM/DD/<slug>.html`. The tray and draft are then cleared.

**Turbify FTP facts, verified 2026-09-26:**
- Only port 21 is open. SFTP (22) is closed, so use FTPS.
- The TLS certificate names **`cpanel292.turbify.biz`**, not `ftp.itsallonesong.com`, so psort connects to cpanel292.
- The account **`sjnelson@itsallonesong.com`** starts at the website root, so blog photos are at **`pics/blog`**. (`sjn@…` is a different, empty account.)
- The hosting home is `/home/atl8cjtp4teu59el`.

**Setup:** `psort blog-login` tests the login and checks that `pics/blog/1024` has photos. It then writes `[blog]` into `psort.toml`, and the password into `~/.config/psort/secrets.toml` (0600; psort refuses to use it if others can read it).

## 9. Commands

Run with `uv run psort …` from the repo.

| Command | Does |
|---|---|
| `init --inbox … --library … --outbox …` | Creates `psort.toml` and the DB, and downloads the face models (`--no-face-model` skips them) |
| `run [--dry-run]` | The six steps with progress. `--dry-run` shows planned copies and moves without touching the library (the DB is still updated) |
| `ingest` / `cluster` / `score` / `curate [--dry-run]` | Single steps |
| `status` | Totals, including undated, close calls, trash, videos, Rich Capture, favorites and "not copied yet" |
| `verify [batch] [--all]` | Safe-to-delete report (§5.5) |
| `review [--port]` | Review UI |
| `close-calls` | Lists near-tie moments |
| `events` · `events name <id> <name> [--through <id>]` · `events unname <name>` | Events (§5.6) |
| `faces list` · `crops` · `label <name> --group/--face` · `unlabel --face` · `scan` | Faces from the command line (§5.7) |
| `highlights` | Syncs `highlights/` (also part of `run`) |
| `export <post> [names…] [--keep-tray]` | Export only (§7) |
| `blog-login` | Saves and tests the FTPS login (§8) |
| `reconcile [--apply]` | Repairs records after hand edits (§5.11) |
| `empty-trash` | Permanently deletes trashed photos |
| `fetch-models` | Downloads the face models if missing |

## 10. Tech Stack

- **Python 3.12**, packaged with `pyproject.toml` and managed by **uv** (installed in `~/.local/bin`). No sudo was needed; Ubuntu's `python3-venv` isn't installed.
- **Libraries:**
  - `typer` (CLI)
  - `Pillow`, `pillow-heif` (images/HEIC)
  - `imagehash` (perceptual hashes)
  - `opencv-python-headless` (sharpness, YuNet/SFace faces, video frames)
  - `numpy`, `scipy` (face grouping)
  - `Flask` (review UI)
  - stdlib `sqlite3`, `ftplib`, `zipfile`, `tomllib`
- **Dev:** `pytest`, `pyftpdlib` (a real local FTP server for publish tests).
- **Tests** (`uv run pytest`, 99 tests, about 80 s) use generated images, videos (OpenCV writer) and `.nar` packages, a local FTP server, and a git repo with a bare "GitHub" remote. They never touch the user's real config, DB, secrets or library.

## 11. Decisions

- Events go in folder names (option A) · face recognition included · close calls flagged
- Visual duplicates → `_duplicates/` (option 1) · dark mode by default
- Videos in a parallel `videos/` tree beside the library · Live Photo clips kept beside their photos
- Every inbox file copied somewhere (`unsorted_files/` for the rest)
- Favorites + an automatic `highlights/` folder, instead of a full second copy
- Blog publishing pushes immediately, and posts are dated by their first photo
- Rich Capture: option B (package beside its photo, frames as photos)
- Progress with steps, per-step and overall percentages, and time left

## 12. Status & Handoff

### Where things stand (2026-09-26)
- **All of the above is built, tested and pushed** to `git@github.com:stevenelson35/psort.git` (`main`).
- **The user's library** holds roughly 7,500 inbox files ingested, with more batches still arriving from OneDrive.
- **The first large run** found 1 unreadable Windows Phone photo, since fixed; it's retried automatically. It also filed 667 `.nar` files as "other"; the next `psort run` upgrades them to Rich Capture (§5.12).
- **Blog publishing** is built, but **`psort blog-login` hasn't been run with the real password yet**, so nothing has been published to the live blog through psort. Suggest a Dry run first.
- **The user's review work** (closes, faces, events, undated) is ongoing. There are 15 Disney photos dated only to "April 2016."

### Backlog and ideas (not built)
- **People filter** on day pages and in search, beyond Favorites. Optionally, favor shots where family faces are sharp.
- **Eyes-open / smile** scoring.
- **`psort prune`:** delete `_alternates`/`_duplicates` after review, with a preview.
- **`rebuild-state`:** rebuild the DB from `library/.psort/manifest.json`. Face embeddings aren't in the manifest, so faces would need re-scanning.
- **Delete for videos** (the trash currently covers photos and their companions only).
- **Moving a photo to a different event by hand** (today, folders follow dates and event ranges only).
- **RAW support** (DNG/CR2).
- **Faster big runs:** parallel analysis. It's single-threaded today at about 0.5 s per photo.
- **The blog's GitHub Action** still uses plain FTP. It could switch to FTPS with `cpanel292.turbify.biz`.
- **Phase 3 idea:** a `blogupdate.html` picker of photos psort has already uploaded.

### Code map (`src/psort/`)
| Module | Role |
|---|---|
| `cli.py` | Typer commands; `run` orchestrates steps with `progress.Progress` |
| `config.py` | `psort.toml` loading and defaults; paths for library/videos/unsorted/highlights/state |
| `db.py` | Schema plus lightweight migrations (`_ADDED_COLUMNS`) |
| `ingest.py` | Scan, classify (`kind_of`), photos/videos/Live clips/Rich packages/other files, settle and retry logic, folder re-dating |
| `imaging.py` | Hashing, `load_small`, `upright` (safe EXIF rotation), `analyze`, YuNet wrapper |
| `dates.py` | EXIF/filename/folder date parsing; `UNCERTAIN` / `NO_TIME` date sources |
| `moments.py` | Clustering, visual duplicates, scoring, close calls |
| `library.py` | Names, desired paths, curate (copy/move), companions (`_sync`), verify, manifest |
| `videos.py` | Video probing (mvhd, OpenCV), Live Photo detection, video layout and curate, posters |
| `rich.py` | Rich Capture package reading and frame extraction |
| `events.py` | Event suggestions and named ranges; `slugify` |
| `faces.py` | Face scan, grouping (kNN + connected components), label/unlabel with rejections, crops |
| `highlights.py` | Favorites and the `highlights/` sync |
| `trash.py` | Delete/restore/empty trash, companions |
| `reconcile.py` | Repairing records after hand edits |
| `export.py` | Web JPEG rendering (allowlisted metadata) and outbox export |
| `blog.py` | Composer draft, post rendering, responsive sizes, FTPS uploader, git publish |
| `actions.py` | Review decisions shared by UI and CLI; `refresh()` re-scores, curates, syncs highlights, writes the manifest |
| `progress.py` | Step/overall progress display |
| `review/` | Flask app (`__init__.py`), Jinja templates, `static/style.css` |

### Data model (SQLite, `~/.local/share/psort/psort.db`)
- **`sources`:** every inbox file (path relative to the inbox, size, mtime, status `image|video|livephoto|rich|sidecar|other|error|junk`, reason, sha256).
- **`photos`:** one row per unique photo. Holds its date and `date_source`, analysis values, `moment_id`, `score`, `is_best`/`user_best`, `close_call`, `duplicate_of`, permanent `name`, `library_path` and `faces_scanned`.
- **Other files:**
  - `videos`, `live_clips` (with `photo_path`), `rich_packages` (with `photo_path`), `derived_frames` (frames unpacked from packages), `other_files`
  - each has a `library_path` relative to its root
- **Review state:**
  - `named_events`, `people`, `faces` (embedding BLOB, `person_id`, `label_source`, `cluster`), `face_rejections`
  - `tags`, `favorites`, `highlights` (the files psort wrote), `tray` (with `position`), `post_draft`, `exports`, `reviewed`
  - `deleted_photos` (with `trash_path`, `purged`, and the full row as JSON)

### Gotchas
- **Running commands:** use `unset VIRTUAL_ENV` first. The user's shell sets it to the blog repo's venv, which confuses `uv`.
- **Reading the user's live DB:** open it read-only (`sqlite3.connect("file:…?mode=ro", uri=True)`).
- **Don't modify the user's library or config** without asking. For previews, copy the DB to `/tmp` and run the logic against the copy.
- **Timezone:** WSL is America/New_York, and video creation times are converted to local time.
- **Windows copies:** files are preallocated, and mtime/ctime tick every second during a copy. That's the basis for `settle_seconds`.
- **Headless Chrome** (`/mnt/c/Program Files/Google/Chrome/Application/chrome.exe`) can screenshot localhost pages served from WSL. It's useful for checking the UI.
- **Pushing:** the repos push over SSH (`~/.ssh/id_ed25519`, registered on GitHub as "WSL BACH").

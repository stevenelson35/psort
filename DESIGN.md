# psort — Design Specification

psort is a local photo curation tool. It takes the chaotic, date-dumped folders from your phones, removes duplicates, groups near-identical shots into *moments*, picks the best shot in each, and builds a clean, private, date-organized library on your PC. When you pick photos for a blog post, psort exports web-ready copies that plug into the existing blog workflow.

## 1. Constraints

1. **Private by default.** The whole library stays on your PC or OneDrive. Nothing is uploaded unless you explicitly pick it for a post.
2. **Raw photos are never modified.** psort only reads the inbox. It never renames, moves or deletes anything there.
3. **The library becomes the master copy.** Once a batch is curated and verified, you may delete it from the inbox. So psort copies **every** photo it keeps; it never uses links back to the inbox.
4. **No cost, no sudo.** Python 3.12 and pip-installable libraries only. Everything runs locally, with no cloud APIs.
5. **Idempotent.** Re-running any command is safe. Photos are identified by their **content**, not their path, so moving or renaming inbox folders doesn't cause reprocessing, and your review decisions survive re-runs.
6. **The blog stays unchanged in Phase 1.** The existing `blogupdate.html` → Cloudinary → GitHub Action → Turbify flow keeps working exactly as it does today (§8).
7. **No videos.** Video files, including Live Photo `.mov` companions, are skipped. They're always *reported*, so nothing is silently lost before you delete an inbox batch.
8. **No spaces in names.** Every folder and file psort creates uses lowercase letters, digits, `-` and `_` only. Event names you type are converted: "Birthday Party" → `birthday-party`.

## 2. Folders

| Folder | Example location | Written by psort? | Contents |
|---|---|---|---|
| **Inbox** | `/mnt/d/psort-inbox/` (USB or Windows drive) | Never | Batches you drop in as subfolders, e.g. `2019-phone-dump/`, `sarah-iphone-2024/` |
| **Library** | `/mnt/c/Users/steve/OneDrive/Pictures/psort-library/` | Yes | The curated master library (§5.4) |
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
    3. the file's modified time, marked **date uncertain** so it shows up for review
  - **Other data:** camera model, width and height, orientation, screenshot flag (PNG, no camera EXIF, or screenshot-style name).
- Records every skipped file (videos, unreadable files, unknown types), with its reason.

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

## 6. Review UI

- **`psort review`** starts a local Flask app at `http://localhost:5000`. It's only reachable from your own PC and has no login.
- **Browse:** Year → day or event → a grid of moments. Close calls are listed first. Each moment shows its best shot, with a count badge for alternates.
- **Actions:**
  - choose a different best shot
  - fix the date on an uncertain photo
  - add tags
  - mark a day or moment as reviewed
  - **add photos to a post**, which puts them in the export tray (§7)
- Overrides are stored per photo content hash, so they survive re-runs.

## 7. Export for a Post

- `psort export <post-slug>` writes every photo in the export tray (or ones picked on the command line) to `<outbox>/<post-slug>/`. Each exported photo:
  - is converted to **JPEG**, quality 85
  - is **rotated upright** using its EXIF orientation. That's usually the cause of sideways blog photos, so `rotate_pic.sh` should rarely be needed.
  - is resized to at most **2048px** on the long edge, the blog's largest size
  - has **GPS and other location data stripped**, along with camera serial numbers. The date is kept.
  - keeps its library filename (`20260703_145633.jpg`), so the blog's naming stays consistent
- Each export is logged, so the UI can show which photos have already appeared in which posts.

## 8. Blog Integration

### Phase 1: no blog changes (MVP)

1. In the review UI, add photos to a post and click **Export**. Or run `psort export go-dogs-go`.
2. Open `blogupdate.html` as you do today. In each image block, the Cloudinary widget's file picker opens the outbox folder.
3. Everything after that is unchanged: Cloudinary → `process-blog-post.yml` makes the 640–2048 sizes → uploads to Turbify `pics/blog/` → commits the post.

### Phase 2: direct publishing (future)

- psort uploads the exported photos and their 640–2048 sizes straight to Turbify `pics/blog/<size>/<name>-<size>.jpg` over **SFTP** (with `paramiko`, which needs no sudo).
- psort writes a small index of published photos that `blogupdate.html` can offer as a picker.
- The workflow gets a small change: when an image block has a filename but no Cloudinary URL, it skips downloading and resizing, because the photo is already on Turbify.
- This removes the Cloudinary round-trip and its free-tier limits.

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

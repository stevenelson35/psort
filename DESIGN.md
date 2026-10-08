# psort — Design Specification

psort is a local photo curation tool. It takes the chaotic, date-dumped folders from your phones, removes duplicates, groups near-identical shots into *moments*, picks the best shot in each, and builds a clean, private, date-organized library on your PC. From there you can star favorites, and publish chosen photos straight to the Jekyll blog at blog.itsallonesong.com.

> **Picking this project up later, or handing it to another agent?** Start with **§12 Status & Handoff**. It covers what's built, what's pending, the user's environment, a code map, the data model, and known gotchas. `CLAUDE.md` has the short version for coding agents.

## 1. Constraints

1. **Local library, chosen publishing.** The library stays on the PC and OneDrive. Photos leave it only through the blog (§8), an export (§7), or the browse page (`publish-browse`, which uploads every ★ favorite and ◆ top pick). A photo marked **🔒 private** (§5.9) is never published by any of them.
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
| **Input directories** | Ordered `paths.inboxes` array in `psort.toml` (outside OneDrive) | Never | Each directory is scanned recursively; batches may be at any depth |
| **Library** | `/mnt/c/Users/steve/OneDrive/photos/psort/a_library` | Yes | The curated master library (§5.4) |
| **Videos** | `…/OneDrive/photos/psort/videos` | Yes | Videos, in the same year/day/event folders as the library (§5.8) |
| **Unsorted files** | `…/OneDrive/photos/psort/unsorted_files` | Yes | Every other file, under its original inbox path with spaces → hyphens |
| **Highlights** | `…/OneDrive/photos/psort/highlights` | Yes | Web-size copies of ★ favorites, kept in sync (§5.9) |
| **Top picks** | `…/OneDrive/photos/psort/top_picks` | Yes | Web-size copies of ◆ top picks, **flat** (one folder, `<name>.jpg`), kept in sync (§5.9). `paths.top_picks` |
| **Outbox** | `/mnt/c/shared/media/psorted_outbox` | Yes | "Export only" copies for `blogupdate.html` (§7) |
| **State** | `~/.local/share/psort/` (inside WSL) | Yes | SQLite database, thumbnail and poster caches, face models |

- **The state database lives inside WSL.** SQLite locks unreliably on Windows drives mounted in WSL, and the WSL filesystem is much faster. After changes, psort writes a JSON copy of the state to `<library>/.psort/manifest.json`, so the library documents itself. Face embeddings are deliberately excluded (§5.7).
- **Moving an input directory:** move it, then edit its entry in `paths.inboxes` without changing its position. Source records are relative to their ordered root, and library records are relative to each output root. Don't re-run `psort init` for this; `init --force` rewrites the config.
- **Inbox order is part of the data.** Source keys record an inbox's *position* (root 0 unprefixed, later roots `_psort_inbox_N/`). So never delete an entry from `paths.inboxes` or reorder them; change only the path in an entry (`psort relocate`).
- **A missing inbox is skipped, not fatal.** If an entry's directory is gone (drive unplugged, folder deleted), `run`/`ingest`/`verify` warn `Inbox N not found, skipped` and carry on with the others. Its entry keeps its position, so later inboxes keep their keys. Nothing is forgotten, re-keyed or removed from the library because a source is absent (records are only dropped when a *present* file changes content); photos already in the library never need the inbox again. `run` stops only if **no** inbox exists. If a photo's library copy is also gone and its source is in the missing inbox, `curate` reports it as missing rather than guessing. Reordering the list would re-key sources, but photos are content-addressed (SHA-256) and every copy is hash-verified, so nothing is duplicated, lost or mixed up; the old keys are just stale until you restore the order.
- **Moving to another computer:** `psort restore ARCHIVE --relocate` (§12). Or by hand: copy the complete state directory, library, all input directories and the separate videos/unsorted/highlights trees; then update the TOML paths. The database is required to retain review decisions and face embeddings. With the DB copied, hashes identify previously analyzed photos even if file timestamps changed; the scan still walks the inputs and may hash them. Preserve input-root order when editing paths.

## 3. File Types

| Type | Handling |
|---|---|
| **JPEG, HEIC/HEIF, PNG, TIFF, GIF, WebP** | Photos. HEIC is read with `pillow-heif`. The **library keeps each original byte-for-byte**, and exports are always JPEG. PNG screenshots are flagged. `.tiff` is stored as `.tif`. Animated GIF/WebP and multi-page TIFF are analyzed and thumbnailed from their first frame. 16-bit and float TIFFs are scaled into 8-bit for analysis and thumbnails (`to_rgb`) rather than clipped to white. Files with no EXIF are dated from the filename, folder name, then file time. |
| **iPhone Live Photo clip** | A `.mov`/`.mp4` of 5 seconds or less beside a same-named HEIC or JPEG. It's kept beside its photo, with the same name (§5.8). |
| **Nokia Lumia Rich Capture `.nar`** | A ZIP of flash, no-flash and blend frames behind a `_Rich.jpg`. The package is kept beside its photo, and each frame becomes a photo (§5.12). |
| **Videos** | `.mp4 .mov .m4v .3gp .avi .mpg .mpeg .mts .m2ts .mod .tod .vob .wmv .mkv .webm .flv .dv`: copied to `videos/` (§5.8) |
| **Helpers** | `.THM` (camera video preview, which also dates its video) and `.AAE` (iPhone edits): copied to `unsorted_files/` |
| **Anything else** | Documents, `.picasa.ini`, unreadable or corrupt files: copied to `unsorted_files/`. Corrupt images can be recovered (§5.13) |

- **Very large images** (up to 300 MP) are allowed, raised from Pillow's "decompression bomb" guard, for posters and panoramas.
- **Orientation:** psort applies the EXIF orientation itself instead of using Pillow's `exif_transpose`, which crashes on some real-world metadata, such as Windows Phone photos.
- RAW formats (DNG, CR2) aren't handled yet. They would currently go to `unsorted_files`.

## 4. Pipeline

`psort run` runs six steps and shows progress for each (§4.1). Most steps can also run on their own.

```
input directories ─[1 scan]─► state DB ─[2 group moments]─► ─[3 score]─► ─[4 arrange library]─► library / videos / unsorted_files
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
- **Multiple input directories:** each configured root is announced as `Inbox 1/N`, `Inbox 2/N`, and so on. Each root reports its file count, new/changed count, scan time, processing time, new photos and unchanged files, so an already-processed root can be compared with a newly added root.
- Listing the inbox checks each file once, and the scan reuses those results. On the user's inbox (~9,700 files on `/mnt/c`), listing alone takes about 75 s.
- **The overall percentage and time left** are weighted by this run's actual work, estimated up front: new files, photos needing a face scan, and so on. The faces step is re-estimated once its exact count is known.
- **Speed:** about 0.5 s per new photo (analysis plus faces). A 5,000-photo batch takes roughly 40 minutes. Ctrl+C is safe, and the next run resumes.

## 5. Stages

### 5.1 Scan (ingest)

- Walks every configured input directory recursively, at any depth, and records **every** file in `sources`: its path relative to its root, size, mtime, status and content hash. `paths.inboxes` is ordered; root order must remain stable when reusing a database. Root zero keeps legacy source keys; later roots have `_psort_inbox_N/` source-key prefixes so identical relative paths do not collide.
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
- **Unreadable files** are copied to `unsorted_files`, and **retried on every run**. If a retry succeeds, the photo goes into the library and its unsorted copy is removed. Damaged images can also be re-saved (§5.13).
- **Types psort learns later:** a file recorded as "other" whose type is now supported (e.g. `.tif`, `.gif`, `.webp`, added 2026-10-04) is re-examined on the next `psort run`, filed in the library, and its `unsorted_files/` copy removed.
- **Safe while files are still being copied in:**
  - **Verified Windows behavior on this PC:** a copy's destination has its full size from the start, and its mtime/ctime update every second. When the copy completes, the original mtime is restored.
  - Files changed within `settle_seconds` (default 120, `[inbox]`) are skipped as "still arriving."
  - Each file is re-checked after reading. One that changed is left for the next run.
  - If a file at a known path changes content, psort's copies of the old version are removed, but only if no other inbox file has that content.

### 5.2 Group moments (cluster)

- A **moment** is a burst of near-identical shots from one camera. Consecutive photos join when they're taken within `burst_gap_seconds` (10) of each other **and** their perceptual hashes are within `phash_threshold` (10 of 64 bits).
- **Moment ID** = the content hash of its earliest photo, which stays stable as later batches add photos.
- **Manual groups:** best-shot selections on a day page can combine separate moments (**Combine selected moments**); selected shots on a moment page can be split out into a new moment (**Move selected to a new moment**). Per-photo overrides are stored in `moment_overrides` and reapplied after every automatic cluster pass, so these edits survive `psort run`.

### 5.3 Score

Scores are compared only **within a moment**. Weights live in `[weights]`:

| Signal | Method |
|---|---|
| Sharpness (0.5) | Variance of the Laplacian on a downscaled grayscale image |
| Exposure (0.2) | Clipped shadows and highlights, and distance from mid-tone |
| Faces (0.15) | Number of faces found by YuNet |
| Face sharpness (0.15) | Sharpness of the face regions |
| Resolution (0.1) | Pixel count, relative to the largest shot in the moment |

- The highest score is the **best** shot unless you've picked another. Your pick always wins.
- **Resolution** breaks ties (or near-ties) toward the higher-resolution shot when two photos in a moment otherwise look alike but one has fewer pixels (e.g. a screenshot or a resized copy that wasn't close enough to be flagged a visual duplicate). Photo cards also show each shot's width × height, so you can tell them apart by eye too.
- **A favorite is preferred next:** if no shot in the moment is explicitly picked, but one is already starred (☆), it becomes the best shot instead of the plain top-scoring one (highest-scoring starred shot, if more than one is starred). Starring a shot re-scores its moment immediately, so this takes effect right away, not just on the next `psort run`. An explicit pick still always wins over a favorite.
- **Tuning:** psort scores only sharpness, exposure and faces — not framing, composition or subject distance/detail. If the automatic pick is consistently the technically sharper but less well-framed shot, lowering `sharpness` and raising `exposure` in `[weights]` (they don't need to sum to 1) shifts the balance; there's no "framing" signal to weight today, so close calls involving framing differences still need a manual pick.
- **Close calls:** when the runner-up is within `close_call_margin` (5%) of the best, the moment is flagged for review. The flag clears once you pick (or once a favorite settles it).
- A user pick always resolves to a real photo in its moment, even if a photo's `duplicate_of` pointer is stale (e.g. left over from an older run); scoring falls back to the picked photo itself rather than silently leaving the moment with no best shot.
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
  - **Ignore:** "Ignore ticked" (Faces page or a group page) marks faces `ignored` — not a person to identify, e.g. a stranger caught in a shot. Ignored faces are dropped from grouping and auto-matching entirely, so they never come up again. **See ignored faces** lists them, each with an **Un-ignore** to put one back into review.
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

- **Favorites:** ☆/★ on any photo. The **★ Favorites** page filters by year and person, and by **◆ top picks only**.
- **Top picks:** ◇/◆ beside the star (every day card, the moment page) marks a photo as one of your very best. **A top pick is always a favorite**: one click on ◆ makes it both (and shows its moment on the Library page), so it has its highlights copy too. Un-marking ◆ leaves it a favorite; un-starring ☆ removes both. Each top pick also gets the same 2048px copy in **`top_picks/`**, but **flat**: just `<name>.jpg` in one folder with no year/day levels (names are unique), so the whole set is easy to copy or show off. `top_picks/` is kept in sync exactly like `highlights/` (table `top_picks`; `favorites.top` marks them) and `psort top-picks [--out DIR] [--originals]` syncs it and copies the set, flat, to any folder (web-size JPEGs, or the original files with `--originals`; files already there are left alone).
- **`highlights/`** holds a 2048px, upright, GPS-free JPEG of each favorite, at the **same folder path and filename** as its original (without any `_alternates`/`_duplicates` level). Each copy's Windows **Title/Subject** is `psort library: <original path>`, and its **Tags** are people plus psort tags.
- **Kept in sync:**
  - un-favoriting removes the copy
  - moving the original moves it
  - changing people or tags re-renders it
  - a missing copy is restored on the next run
- psort touches only files it wrote there.
- **🔒 Private** (2026-10-08): a per-photo flag (`photos.private`, default off), toggled with 🔒 on any day card or on the moment page. A private photo stays everywhere locally (library, `highlights/`, `top_picks/`, `psort top-picks --out`), but is **never published**: the blog publish and Export only refuse a tray holding one (the tray page warns first), and `publish-browse` leaves it out of the manifest and **removes its copies from the server** if it was published before. To review them: the Library's **🔒 Private (n)** view lists every private photo by year and month, and the day page's **🔒 Private** filter shows them, including private shots that aren't their moment's best (labeled "private alternate"). The flag lives on the photo row, so it survives Trash/Restore, and it's in `manifest.json`.

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

### 5.13 Recovering unreadable images

- **What counts:** sources recorded with status `error` and an image extension, whether found during this run or long ago, whether or not the inbox copy still exists (the byte-identical copy in `unsorted_files/` is used if not). Not tried before, unless `--retry`.
- **How:** `recover.py` opens the file with Pillow, tolerating truncation (decodes what's there; the missing part is blank/grey), then falls back to OpenCV. It rejects results that are tiny or blank, and anything over 512 MB. The picture is saved as a quality-95 JPEG keeping the original's EXIF and color profile, so the capture date usually survives. Re-encoding is lossy and the recovered photo is a **separate photo** (new SHA-256), not a repair of the original.
- **Originals are never touched:** the inbox file and the `unsorted_files/` copy stay. The recovered JPEG is kept in `<state_dir>/recovered/<sha>.jpg` (not a cache: it's included in backups) until `curate` copies it into the library (`library._find_source` falls back to it, so a lost library copy is rebuilt too).
- **Each attempt is recorded** in `recoveries` (success or failure) so it isn't offered again; `psort recover --retry` retries failures.
- **When:** `psort run` ends by listing unreadable images and, from a terminal, asking whether to try. Not from a terminal, it just mentions `psort recover`. `--recover` answers yes, `--no-recover` suppresses it. `psort recover [--yes] [--retry]` does it any time. Recovered photos then go through grouping, scoring, curation, faces and highlights immediately.
- **Limits:** a file that isn't really an image (garbage, a disk image with a `.jpg` name) is reported as not recoverable. The user's real data (2026-10-04) had one truncated photo (recoverable, 15 of 3008 rows lost) and one 72 GB "`.jpg`".

## 6. Review UI

- **`psort review`** starts Flask on **127.0.0.1:5000**, with no login. Requests whose Host isn't localhost are refused, and every change needs a per-launch token (a Jinja global, so imported macros see it). Dark mode is the default, with a light toggle remembered per browser.
- **Errors:** Flask runs with debug off, so an unexpected exception would normally just show a bare "Internal Server Error." A global handler instead logs the full traceback to the terminal running `psort review` and shows a short message (with the exception type and text) as a flash on the page you were on, or an error page for a broken link.
- **Pages:**
  - **Library:** five views, chosen with buttons and remembered in the `psort-library-view` cookie: **Years & months** (collapsible years and months, each with a collage of up to 6 photos / one per moment, day counts, a mini progress bar and an "N to review" badge; open/closed state is remembered, **Expand all / Collapse all**), **List** (month headings), and **Calendar** (a heatmap, one square per day: green reviewed, amber changed since review, blue not reviewed, grey no photos; click a square to open the day). **★ Favorites (n)**, **◆ Top picks (n)** and **🔒 Private (n)** show only those photos, by year then month (no best-shot stand-ins for days without picks), newest year first with a jump bar; click one to open its moment. Review-progress controls don't apply to those two. A sticky **year jump bar** and an **Only days needing review** filter (remembered) apply to all views. Days show counts, close calls, 🎬 and a colored status badge (✓ reviewed, **pics added** / **moments updated** amber, • new), plus an overall **progress bar** (days and photos reviewed vs. the whole library). A remembered **Show day thumbnails** toggle shows, under each day, the best shot of every moment you ticked **show on Library** on that day's page (up to 12 per day, then "+N more"; hidden thumbnails aren't loaded).
  - **Day/event:** best shots with badges (shots, duplicates, close call, ◉ Live, ◈ Rich, in tray, people, tags, posted in), ☆ favorite, tick → **Delete ticked** or **Combine selected moments**, 🎬 videos, **Mark day reviewed**, and **Mark reviewed & return to Library**. Photo cards show each shot's width × height, and default to **Fit** (uncropped); a remembered **Fill** mode crops to the card frame. Filter the visible picks by identified person, close calls, favorites, 🔒 private, or photos with no identified people; **All** resets the view and **Toggle all filters** selects or clears all specific filters. A starred shot that isn't its moment's best is a **favorite alternate**: it's hidden normally and shown, labeled, only with the **Favorites** filter (a private one likewise with the **🔒 Private** filter). Combining moments carries a **show on Library** pin over to the merged moment. Starring a photo also ticks its moment's **show on Library** box (un-starring leaves it as is). Zoom controls resize photo panels and reflow the grid; sizes are remembered per view.
  - **Day status:** each day is **new** (never reviewed), **reviewed**, **pics added**, or **moments updated**. Marking a day reviewed stores a fingerprint of its photo set and of how those photos are grouped into moments (`daystatus.py`); status is derived by comparing that fingerprint with the day as it is now. So a `psort run` that adds photos to a day, or re-clusters it (e.g. after changing `[cluster]` settings), flags just that day, and days whose photos and grouping didn't change stay reviewed — nothing is reset during a run. Clicking **Mark day reviewed** on a flagged day re-marks it as it is now. Your own combine/split/delete on a reviewed day re-marks it afterward, so your edits don't flag it. Favorites, picks and pins aren't part of the fingerprint. Days marked reviewed by an older psort are fingerprinted (as they are at that moment) by the next command or review start, before any re-clustering.
  - **Day navigation:** a breadcrumb (Library › year › month › day) and one toolbar of matching buttons in three groups: **← Library** (back to that month), **‹ Previous day / Next day ›**, **« Previous unreviewed / Next unreviewed »** (any status but reviewed; labels show the destination date); and, for an unreviewed day, **✓ Mark day reviewed**, **✓ & back to Library**, **✓ & « previous unreviewed** and **✓ & next unreviewed »**. Unavailable directions are shown disabled. The moment page uses the same breadcrumb. Each best-shot card has a **show on Library** checkbox (stored per moment in `library_pins`).
  - **Moment:** every shot with its score breakdown; select shots and **Move selected to a new moment** to split them:
    - **Make this the best** / let psort pick again
    - ☆, tray, tags, fix date, 🗑 delete
    - Detected faces beside their photo: **Not <name>**, **Ignore face**, **Un-ignore face**, and add/change identification
    - play the Live clip
  - **★ Favorites** (year/person filters, **◆ top picks only**) · **Close calls** ("Pick this" or confirm "Keep this as best") · **Events** (name/through/unname) · **Faces** (name groups; person pages with "Not <name>") · **Videos** · **Undated** (exact date per photo, or **one date for all ticked**) · **🗑 Trash** (Restore / Empty) · **Post tray** (the composer, §8)
- Every decision updates the library right away (files move, folders rename), along with highlights.
- **Clicks stay fast** (reworked 2026-10-08 for the ~100,000-photo library):
  - **Only the changed moments are redone.** ☆/◆, best-shot picks and combine/split pass their moments to `actions.refresh(moments=…)`. That re-scores, re-curates (photos plus their Live clips and Rich packages) and re-syncs highlights for just those moments. Combine/split write `moment_overrides` and apply them directly (`moments.regroup`) instead of re-clustering everything; the next full `cluster()` reapplies the overrides with the same result (`tests/test_fast_clicks.py` checks this). Measured on the real DB: re-scoring the whole library took ~1.5 s, curating ~0.75 s and clustering ~4.3 s; a scoped click takes ~15 ms, plus any file moves and highlight renders (~0.4 s for a new favorite).
  - Delete, restore and date changes still do a full re-cluster (they're rarer, and they can change moment IDs).
  - Files that aren't moving are trusted from the database rather than re-checked on disk; `psort run` still verifies every file.
  - **One change at a time:** POST requests hold a lock (Flask serves requests on threads), so quick clicks can't interleave database updates and file moves. The deferred manifest hook is set only while that lock is held.
  - **The day page doesn't reload:** ☆ ◇ 🔒, **show on Library** and **Combine selected moments** are sent with `fetch()` (`X-Requested-With: fetch` → JSON `{ok, error}`). The star flips at once, then only the changed moments' cards are re-fetched (`GET /folder/<key>/cards?m=<moment>`, rendered by the shared `_day_card.html` macro) and swapped in, so the page keeps its scroll position and filters. Errors show as a message at the bottom of the page. Without JavaScript the same forms post normally and return to the card's `#m-<moment>` anchor. **Delete ticked** still reloads the page.
  - **Selecting and combining on a day page:** **shift+click** a select box to tick (or untick) every visible card between it and the last one clicked. The Delete/Combine row stays at the top while you scroll, with **N selected · Clear**. **Drag a photo onto another** to combine their moments (the target gets a dashed outline). If the dragged photo is ticked, all ticked photos come along. Only best-shot cards can be dragged or dropped on (not favorite/private alternates). Every combine, by button or drag, shows **Combined N moments · Undo** for 15 s. The combine response carries a snapshot (`actions.moments_snapshot`: each member's moment, override and explicit pick, plus pins), and **Undo** posts it back to `/folder/<key>/uncombine` (`actions.restore_moments`), which restores it exactly.
- **`manifest.json`** (about 10 MB) is written in the background 4 s after the last change, and again when the review page stops.
- **Thumbnails, posters and face crops** are cached in the state folder, named by content, so they're never stale.
- **Backup page:** archives the config directory and the state directory (database, face models) into a dated, commit-tagged `.tar.gz` under `~/psort-backups/` (same as `psort backup`, §9). Thumbnail/face-crop/poster caches are skipped by default since they're regenerated automatically.

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

**Publish** (pushes immediately, as the user chose). A tray holding a 🔒 private photo is refused before anything is built (also for Dry run and Export only):
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
| `init --inbox … [--inbox …] --library … --outbox …` | Creates `psort.toml` and the DB, and downloads the face models (`--no-face-model` skips them) |
| `run [--dry-run]` | The six steps with progress. `--dry-run` shows planned copies and moves without touching the library (the DB is still updated). **Only one `run` at a time:** it holds an OS lock on `<state_dir>/run.lock` (released however the process ends, even `kill -9`; a run paused with Ctrl+Z still holds it), and a second `run` refuses to start, naming the other's process id. The review UI isn't blocked |
| `ingest` / `cluster` / `score` / `curate [--dry-run]` | Single steps |
| `status` | Totals, including undated, close calls, trash, videos, Rich Capture, favorites and "not copied yet" |
| `verify [batch] [--all]` | Safe-to-delete report (§5.5) |
| `review [--port]` | Review UI |
| `close-calls` | Lists near-tie moments |
| `events` · `events name <id> <name> [--through <id>]` · `events unname <name>` | Events (§5.6) |
| `faces list` · `crops` · `label <name> --group/--face` · `unlabel --face` · `ignore --face` · `unignore --face` · `scan` | Faces from the command line (§5.7) |
| `highlights` | Syncs `highlights/` and `top_picks/` (also part of `run`) |
| `top-picks [--out DIR] [--originals]` | Syncs `top_picks/`, and optionally copies the top picks, flat, to DIR (web-size, or full-resolution originals) (§5.9) |
| `export <post> [names…] [--keep-tray]` | Export only (§7) |
| `blog-login` | Saves and tests the FTPS login (§8) |
| `reconcile [--apply]` | Repairs records after hand edits (§5.11) |
| `recover [--yes] [--retry]` | Re-saves what can be read of unreadable images as new library photos; the damaged originals stay (§5.13). `run` offers it at the end: `--recover` / `--no-recover` |
| `empty-trash` | Permanently deletes trashed photos |
| `fetch-models` | Downloads the face models if missing |
| `backup [--out DIR] [--include-caches]` | Archives the config directory and the state directory (DB, face models) into a dated, commit-tagged `.tar.gz`; `--out` defaults to `~/psort-backups/` (§12). Also available as a **Backup** page in the review UI |
| `restore ARCHIVE [--relocate] [--force]` | Restores `psort.toml` (plus other config files) and the state directory from a backup. `--relocate` walks every path in the restored config (each inbox in order, library, outbox, videos, unsorted, highlights, state_dir), showing which exist on this computer; Enter keeps one. Everything is unpacked and edited in a staging folder first; existing config/state is only replaced with `--force` and is moved aside (`*.before-restore-<time>`), never deleted. Close `psort review` first (§12) |
| `relocate` | Same path-by-path review for the current `psort.toml` (new drive letter, moved folder). Keeps the previous file as `psort.toml.before-relocate`; comments are preserved |
| `publish-browse [--dry-run] [--remote-dir]` | Publishes every favorite that isn't 🔒 private plus a JSON manifest for the browse page, with a CORS rule for the blog's origin. Afterwards it removes from the server the copies of photos no longer wanted (un-starred or marked private), found from the `640/` listing; only psort's photo file names are deleted. `--remote-dir` (default `pics/browse`) may not be the blog's photo folder |

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
- **Tests** (`uv run pytest`) use generated images, videos (OpenCV writer) and `.nar` packages, a local FTP server, and a git repo with a bare "GitHub" remote. They never touch the user's real config, DB, secrets or library.

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
- **2026-09-27:** added ignoring faces (§5.7), auto-un-review of days when new photos land in them, and a library-wide review progress bar (§6).
- **2026-09-29:** multiple ordered `[paths.inboxes]` input roots (§2), per-inbox scan/processing progress during `psort run` (§4.1), and `psort publish-browse` now writes a CORS rule so the blog origin can fetch its manifest.
- **2026-09-30 to 2026-10-01:** day pages got Fit/Fill photo framing, person/close-call/unidentified filters, and a zoom control for panel size (also on Favorites/Undated/Trash/moment pages); moments can be combined (day page) or split (moment page) by hand, persisted in `moment_overrides` and reapplied after every recluster; detected faces can be identified/corrected/ignored right from a photo on its moment page, not just from the Faces pages; an unexpected review-UI error now logs a traceback to the terminal and shows a useful message instead of a bare 500; and a starred favorite is now preferred over the plain top score when psort picks a moment's best shot (§5.3), including right after combining moments.
- **2026-10-01 (later):** day pages show each photo's width × height and a **Favorites** filter (surfaces a moment's favorited shot even when it's not that moment's current best); scoring gained a **Resolution** weight so a higher-pixel-count shot wins close/ambiguous comparisons; and `psort backup` / a **Backup** page in the review UI archive the config and state directories into a dated, commit-tagged `.tar.gz` (§9, §6).
- **2026-10-01 (evening):** day review status is now derived from a stored fingerprint (new / reviewed / pics added / moments updated), replacing the old "drop the reviewed row when a new photo lands"; day pages got previous/next and previous/next-unreviewed navigation; and moments can be pinned to show a thumbnail beside their day on the Library page (§6).
- **2026-10-02:** Library redesign (Years & months / List / Calendar views, year jump bar, only-days-needing-review filter, collages), day-page breadcrumb and matching toolbar with destination dates and a **✓ & « previous unreviewed** button (§6); `psort restore [--relocate]` and `psort relocate` for moving to a new computer (§9); a missing inbox is skipped with a warning instead of aborting the run (§2).
- **2026-10-04:** TIFF, GIF and WebP are photos now (files already in `unsorted_files/` move to the library on the next run, §3); `psort recover` and an end-of-run offer re-save unreadable images as new photos (§5.13); `psort restore`/`relocate` and missing-inbox handling from 2026-10-02 are described in §2 and §9.
- **2026-10-05:** **◆ top picks** (§5.9): a ranked-higher favorite, kept flat in `top_picks/`, with `psort top-picks --out DIR` to export; the Library page gained **★ Favorites** and **◆ Top picks** views (every pick by year and month, each linking to its moment); the browse manifest flags top picks (`"top": true`) and the blog's browse page got a **Top picks only** filter.
- **2026-10-06:** `psort run` refuses to start while another `run` is active (a run had been left paused with Ctrl+Z while a second one ran for 17 hours) (§9).
- **2026-10-08:** favorites and the browse page are fine to publish (§1 reworded). Added the **🔒 private** flag (§5.9): never published (blog, export, browse page), with a Library **🔒 Private** view and a day-page **🔒 Private** filter for reviewing them; `publish-browse` now also removes un-starred/private photos from the server. Fixed the Library page hiding the nav-bar counts. The library is now ~100,000 photos (73,000 moments).
- **2026-10-08 (later):** fast clicks (§6): a review click took ~3 s and a combine ~7 s; both now redo only the moments they change (~15 ms plus file moves), POSTs are serialized by a lock, and the day page updates cards in place with `fetch()`. Checked in headless Chrome with a test library.
- **2026-10-08 (evening):** day page shift+click range select, a sticky selection row (N selected · Clear), drag a photo onto another to combine, and Undo after every combine (§6).

### Backlog and ideas (not built)
- **Faster review, next steps** (scoped clicks, in-place day page, shift+click, drag-to-combine and Undo are done, §6): keyboard shortcuts (f/t/x/c), and Undo for split and delete. Opening a day page still takes ~0.5–0.7 s on the big library because `folders()` scans every photo to build the day navigation; cache or narrow that.
- **Big time-less days:** photos dated only by folder are never grouped (e.g. 2025-12-01 has 3,020 single-photo moments). Group them by perceptual hash within the day; page very large days.
- **`publish-browse` efficiency:** keep renders in a cache instead of re-rendering every favorite each run, record uploads in the DB instead of a SIZE round trip per file, skip the unused base `<name>.jpg`, replace the manifest atomically (upload then rename), add `Options -Indexes`.
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
| `moments.py` | Clustering, visual duplicates, scoring (favorite- and user-pick-aware), close calls |
| `library.py` | Names, desired paths, curate (copy/move), companions (`_sync`), verify, manifest |
| `videos.py` | Video probing (mvhd, OpenCV), Live Photo detection, video layout and curate, posters |
| `rich.py` | Rich Capture package reading and frame extraction |
| `events.py` | Event suggestions and named ranges; `slugify` |
| `faces.py` | Face scan, grouping (kNN + connected components), label/unlabel/ignore with rejections, crops |
| `highlights.py` | Favorites, top picks, and the `highlights/` + `top_picks/` sync and export |
| `trash.py` | Delete/restore/empty trash, companions |
| `reconcile.py` | Repairing records after hand edits |
| `recover.py` | Re-saving unreadable images (§5.13) |
| `export.py` | Web JPEG rendering (allowlisted metadata) and outbox export |
| `blog.py` | Composer draft, post rendering, responsive sizes, FTPS uploader, git publish |
| `actions.py` | Review decisions shared by UI and CLI; `refresh()` re-scores, curates, syncs highlights, writes the manifest; `combine_moments`/`split_moment` edit `moment_overrides` |
| `progress.py` | Step/overall progress display |
| `backup.py` | Archives the config and state directories into a dated, commit-tagged `.tar.gz` |
| `daystatus.py` | Per-day review fingerprints and derived status (new / reviewed / pics added / moments updated) |
| `review/` | Flask app (`__init__.py`), Jinja templates, `static/style.css` |

### Data model (SQLite, `~/.local/share/psort/psort.db`)
- **`sources`:** every inbox file (path relative to its configured input root — root 0 unprefixed, later roots as `_psort_inbox_N/…` — size, mtime, status `image|video|livephoto|rich|sidecar|other|error|junk`, reason, sha256).
- **`photos`:** one row per unique photo. Holds its date and `date_source`, analysis values, `moment_id`, `score`, `is_best`/`user_best`, `close_call`, `duplicate_of`, permanent `name`, `library_path` and `faces_scanned`.
- **`moment_overrides`:** manual `sha256 → moment_id` pins from combining/splitting moments by hand; reapplied after every automatic `cluster()` pass so they survive `psort run`.
- **Other files:**
  - `videos`, `live_clips` (with `photo_path`), `rich_packages` (with `photo_path`), `derived_frames` (frames unpacked from packages), `other_files`
  - each has a `library_path` relative to its root
- **Review state:**
  - `named_events`, `people`, `faces` (embedding BLOB, `person_id`, `label_source`, `cluster`, `ignored`), `face_rejections`
  - `photos.private` (never published), `tags`, `favorites`, `highlights` (the files psort wrote), `tray` (with `position`), `post_draft`, `exports`, `reviewed` (day, plus `photos_sig`/`moments_sig`/`photo_count` fingerprint), `library_pins` (moments shown on the Library page)
  - `deleted_photos` (with `trash_path`, `purged`, and the full row as JSON)

### Gotchas
- **Running commands:** use `unset VIRTUAL_ENV` first. The user's shell sets it to the blog repo's venv, which confuses `uv`.
- **Reading the user's live DB:** open it read-only (`sqlite3.connect("file:…?mode=ro", uri=True)`).
- **Don't modify the user's library or config** without asking. For previews, copy the DB to `/tmp` and run the logic against the copy.
- **Timezone:** WSL is America/New_York, and video creation times are converted to local time.
- **Windows copies:** files are preallocated, and mtime/ctime tick every second during a copy. That's the basis for `settle_seconds`.
- **Headless Chrome** (`/mnt/c/Program Files/Google/Chrome/Application/chrome.exe`) can screenshot localhost pages served from WSL. It's useful for checking the UI.
- **Pushing:** the repos push over SSH (`~/.ssh/id_ed25519`, registered on GitHub as "WSL BACH").

# psort

Local photo curation. psort removes duplicates, groups near-identical bursts into moments, picks the best shot of each, and builds a private, date-organized library. It keeps videos, Live Photo clips and Nokia Rich Capture packages alongside, lets you star favorites into an automatic `highlights/` folder (and mark a few ◆ top picks into a flat `top_picks/` folder), and can publish posts straight to the Jekyll blog.

**Docs:** [DESIGN.md](DESIGN.md) is the full spec. Its §12 "Status & Handoff" is the place to start when picking the project up. [CLAUDE.md](CLAUDE.md) has notes for coding agents.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (installed in `~/.local/bin`). No sudo or system packages are needed.

```sh
unset VIRTUAL_ENV        # if your shell sets it to another project's venv
uv sync
uv run psort init --inbox <inbox> --library <library> --outbox <outbox>
# Add another --inbox <directory> for each additional input root.
```

Repeat `--inbox` for each input directory. This writes `~/.config/psort/psort.toml`, creates the database in `~/.local/share/psort/`, and downloads the face models. The `videos/`, `unsorted_files/` and `highlights/` folders default to living beside the library.

During `psort run`, each input is announced as `Inbox 1/N`, `Inbox 2/N`, and so on. psort reports each root's file count, new/changed count, scan time, processing time, new photos, and unchanged files before moving to the next root.

## Move to another computer

The database is essential to preserving analysis and review decisions. The library's `.psort/manifest.json` is not a replacement: it omits face embeddings and other local state. `uv run psort backup` (or the **Backup** page in the review UI) archives the config directory and the state directory for you in one step; to continue without re-analyzing photos. The quick way:

1. On the old computer, `uv run psort backup` and copy the `psort-backup-*.tar.gz` (default folder `~/psort-backups/`) to the new one, along with the library, every input directory, and the `videos/`, `unsorted_files/` and `highlights/` directories.
2. On the new computer, install Python 3.12+ and `uv`, clone this repository and run `uv sync`.
3. Run `uv run psort restore psort-backup-….tar.gz --relocate`. It shows each path from the old `psort.toml` (each input directory in order, library, outbox, videos, unsorted, highlights, top picks, state directory), marks the ones that don't exist here, and lets you type a new path or press Enter to keep one. Nothing is overwritten unless you add `--force`, and then the old files are moved aside, not deleted. Use `uv run psort relocate` any time later to fix paths again.
4. Run `uv run psort status`, then `uv run psort run` and `uv run psort verify`.

Keep input directories in the same order (never delete or reorder entries): the database records each file's input by its position. If an input directory no longer exists, leave its entry in place; psort warns `Inbox N not found, skipped` and carries on with the others. The manual steps follow, if you'd rather not use `restore`:

1. Stop psort on the old computer. Copy the complete configured state directory (normally `~/.local/share/psort/`, including `psort.db` and `models/`), the library, every input directory, and the separate `videos/`, `unsorted_files/`, and `highlights/` directories if present.
2. Install Python 3.12 or newer, install `uv`, clone this repository, and run `uv sync`.
3. Copy `~/.config/psort/psort.toml` to the new computer and edit its `[paths]` entries for `inboxes`, `library`, `outbox`, `videos`, `unsorted`, `highlights`, and `state_dir` to the new locations. Use an ordered TOML array for inputs, for example `inboxes = ["/photos/imports-a", "/photos/imports-b"]`. Keep input directories in the same order as before; the database uses that order to find recorded source files. Keep `state_dir` pointed at the copied state directory.
4. Run `uv run psort run`, then `uv run psort verify`.

Do not run `psort init` with an empty state directory for this migration. It creates a fresh database, which loses review decisions and causes existing inputs to be processed as new. With the copied database, existing photo hashes, best-shot choices, face labels, and other review state are reused. psort still walks and stats the inputs; if copy tools changed file timestamps, it may hash files again, but matching photos are not re-analyzed. Face models in the copied state directory also avoid redownloading them. If you choose not to copy the database, expect to rebuild state by importing the inputs; the manifest cannot restore the full database, notably face embeddings and source/ingest history.

## Everyday use

```sh
uv run psort run            # new inbox files → library, with step-by-step progress
uv run psort review         # review UI at http://localhost:5000 (Ctrl+C to stop)
uv run psort status         # totals
uv run psort verify         # are all configured input directories safely copied? (or: verify <batch>)
```

In the review UI you can:
- pick best shots and resolve close calls, or combine/split moments by hand when bursts should (or shouldn't) be grouped together: on a day page, drag one photo onto another (or shift+click a range and Combine), with Undo
- name events and faces, and fix dates, one photo at a time or many at once
- identify, correct, or ignore a detected face right from its photo, not just from the Faces pages
- star favorites (preferred automatically as a moment's best shot over the plain top score), mark ◆ top picks (always favorites too; copied flat to `top_picks/`), and delete junk (it goes to a trash you can restore from)
- mark a photo 🔒 private so it's never published (blog, export, browse page); review them all from the Library's 🔒 Private view or a day page's 🔒 Private filter
- filter day pages by person, close call, or no identified people, and zoom photo panels larger or smaller
- see every ★ favorite or ◆ top pick by year, each linking back to its moment, from the Library page's Favorites / Top picks views
- browse the Library by years and months (collapsible, with photo collages), as a list, or as a calendar heatmap; jump by year, or show only days still needing review
- move through days with a breadcrumb and one toolbar: previous/next day, previous/next unreviewed day, and "✓ & next/previous unreviewed" to finish a day and keep going
- write blog posts in the Post tray, then Preview, Dry run, or Publish

## Other commands

```sh
uv run psort run --dry-run          # show planned copies/moves without touching the library
uv run psort blog-login             # once: test + save the Turbify FTPS login for publishing
uv run psort export <post>          # "export only" to the outbox, for blogupdate.html
uv run psort reconcile [--apply]    # after moving/renaming/deleting library files by hand
uv run psort empty-trash            # permanently delete trashed photos
uv run psort recover [--yes] [--retry]   # re-save unreadable/truncated images as new library photos (originals stay); `run` offers it too
uv run psort highlights             # sync highlights/ and top_picks/ with your ★ favorites and ◆ top picks (also part of run)
uv run psort top-picks --out DIR [--originals]   # copy the ◆ top picks, flat, to DIR (web-size, or originals)
uv run psort close-calls            # list near-tie moments
uv run psort events | events name <id> <name> [--through <id>] | events unname <name>
uv run psort faces list | crops | label <name> --group <id> | unlabel --face <id> | ignore --face <id> | unignore --face <id>
uv run psort backup [--out DIR] [--include-caches]    # archive config + state (DB, face models) to a dated .tar.gz; caches skipped unless asked
uv run psort restore <archive> --relocate  # restore a backup on a new computer, fixing paths as you go
uv run psort relocate                      # review/edit the paths in psort.toml
uv run psort publish-browse [--dry-run] [--verify]   # update the browse page: uploads only new favorites (not 🔒 private), removes un-starred/private ones
```

Photos can be JPEG, HEIC/HEIF, PNG, TIFF, GIF or WebP. Anything psort can't read as an image (including damaged photos) is kept in `unsorted_files/`; `psort recover` (also offered at the end of `psort run`) re-saves what can be read of a damaged image as a new library photo and leaves the original alone.

psort never writes to the inbox, and every inbox file ends up copied somewhere, except OS cache files. Delete a batch yourself once `psort verify` says it's safe.

## Tests

```sh
uv run pytest -q     # tests use temporary folders only, never your real library
```

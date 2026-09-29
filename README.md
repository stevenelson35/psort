# psort

Local photo curation. psort removes duplicates, groups near-identical bursts into moments, picks the best shot of each, and builds a private, date-organized library. It keeps videos, Live Photo clips and Nokia Rich Capture packages alongside, lets you star favorites into an automatic `highlights/` folder, and can publish posts straight to the Jekyll blog.

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

The database is essential to preserving analysis and review decisions. The library's `.psort/manifest.json` is not a replacement: it omits face embeddings and other local state. To continue without re-analyzing photos:

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
- pick best shots and resolve close calls
- name events and faces, and fix dates, one photo at a time or many at once
- star favorites and delete junk (it goes to a trash you can restore from)
- write blog posts in the Post tray, then Preview, Dry run, or Publish

## Other commands

```sh
uv run psort run --dry-run          # show planned copies/moves without touching the library
uv run psort blog-login             # once: test + save the Turbify FTPS login for publishing
uv run psort export <post>          # "export only" to the outbox, for blogupdate.html
uv run psort reconcile [--apply]    # after moving/renaming/deleting library files by hand
uv run psort empty-trash            # permanently delete trashed photos
uv run psort highlights             # sync highlights/ with ★ favorites (also part of run)
uv run psort close-calls            # list near-tie moments
uv run psort events | events name <id> <name> [--through <id>] | events unname <name>
uv run psort faces list | crops | label <name> --group <id> | unlabel --face <id>
```

psort never writes to the inbox, and every inbox file ends up copied somewhere, except OS cache files. Delete a batch yourself once `psort verify` says it's safe.

## Tests

```sh
uv run pytest -q     # tests use temporary folders only, never your real library
```

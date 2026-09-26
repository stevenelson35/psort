# psort

Local photo curation. psort removes duplicates, groups near-identical bursts into moments, picks the best shot of each, and builds a private, date-organized library. It keeps videos, Live Photo clips and Nokia Rich Capture packages alongside, lets you star favorites into an automatic `highlights/` folder, and can publish posts straight to the Jekyll blog.

**Docs:** [DESIGN.md](DESIGN.md) is the full spec. Its §12 "Status & Handoff" is the place to start when picking the project up. [CLAUDE.md](CLAUDE.md) has notes for coding agents.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (installed in `~/.local/bin`). No sudo or system packages are needed.

```sh
unset VIRTUAL_ENV        # if your shell sets it to another project's venv
uv sync
uv run psort init --inbox <inbox> --library <library> --outbox <outbox>
```

This writes `~/.config/psort/psort.toml`, creates the database in `~/.local/share/psort/`, and downloads the face models. The `videos/`, `unsorted_files/` and `highlights/` folders default to living beside the library.

## Everyday use

```sh
uv run psort run            # new inbox files → library, with step-by-step progress
uv run psort review         # review UI at http://localhost:5000 (Ctrl+C to stop)
uv run psort status         # totals
uv run psort verify         # is the whole inbox safely copied? (or: verify <batch>)
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
uv run pytest -q     # 99 tests; they use temporary folders only, never your real library
```

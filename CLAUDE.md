# psort: notes for coding agents

Local photo curation for one user (Steve). Read `DESIGN.md`, especially **§12 Status & Handoff**, before changing anything.

## Commands
```sh
unset VIRTUAL_ENV                  # the user's shell points it at another repo's venv
export PATH="$HOME/.local/bin:$PATH"
uv sync                            # install
uv run pytest -q                   # 99 tests, ~80 s; must stay green
uv run psort --help
uv run psort run                   # uses ~/.config/psort/psort.toml (the user's REAL library)
```

## Rules
- **The user's real data:** config `~/.config/psort/psort.toml`, DB `~/.local/share/psort/psort.db`, library under OneDrive. Don't run commands that change them unless the user asks.
  - Read the DB with `?mode=ro`.
  - Preview changes on a copy of the DB in `/tmp`.
  - Tests use `tmp_path` configs only; keep it that way. Monkeypatch `blog.SECRETS_PATH` in tests.
- **Never write spaces** into any generated file or folder name. Slugify user-typed names (`events.slugify`).
- **Inbox is read-only.** Every inbox file must end up copied somewhere (library / videos / unsorted_files) or be an OS cache file. `psort verify` must stay truthful.
- **Identity is content (SHA-256).** Library layout is derived from DB state (dates, moments, events, picks). Curate moves files to match; don't hand-place files.
- **Review UI changes:** every POST needs the CSRF token (Jinja global `csrf`). Test by submitting the forms pages actually render (see `tests/test_review.py`).
- **Schema changes:** add `CREATE TABLE IF NOT EXISTS` in `db.py`. For new columns on existing tables, add to `_ADDED_COLUMNS` (the user has a live DB).
- **Match the surrounding style:** type hints, small functions, comments explaining *why*, and user-facing messages in plain language.
- Commit with a descriptive message and push (SSH remote) when the user asks.

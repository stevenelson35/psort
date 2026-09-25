# psort

Local photo curation. psort removes exact duplicates, groups near-identical bursts into moments, picks the best shot of each, and builds a private, date-organized library. See [DESIGN.md](DESIGN.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/). No sudo or system packages are needed.

```sh
uv sync
uv run psort init \
  --inbox /mnt/d/psort-inbox \
  --library /mnt/c/Users/steve/OneDrive/Pictures/psort-library \
  --outbox /mnt/c/Users/steve/Pictures/psort-outbox
```

This writes `~/.config/psort/psort.toml`, creates the state database in `~/.local/share/psort/`, and downloads the face-detection model.

## Use

```sh
uv run psort run --dry-run          # preview what would be copied
uv run psort run                    # ingest → cluster → score → curate → faces
uv run psort review                 # review UI at http://localhost:5000
uv run psort status
uv run psort verify <batch-folder>  # is this inbox batch safe to delete?
uv run psort close-calls            # near-tie best picks worth a look

uv run psort events                               # suggested events
uv run psort events name 20260703_145633 birthday-party
uv run psort events name 20260704_080000 summer-trip --through 20260708_190000

uv run psort faces list                           # people and unnamed face groups
uv run psort faces crops                          # thumbnails to browse in File Explorer
uv run psort faces label Steve --group 4
```

psort never writes to the inbox. Delete a batch yourself once `verify` says it's safe.

## Tests

```sh
uv run pytest
```

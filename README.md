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
uv run psort run                    # ingest → cluster → score → curate
uv run psort status
uv run psort verify <batch-folder>  # is this inbox batch safe to delete?
```

psort never writes to the inbox. Delete a batch yourself once `verify` says it's safe.

## Tests

```sh
uv run pytest
```

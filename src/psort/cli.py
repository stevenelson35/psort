"""Command-line interface (DESIGN.md §9)."""

import os
import sqlite3
import urllib.request
from pathlib import Path
from typing import Annotated

import typer

from . import config as config_mod
from .config import DEFAULT_CONFIG_PATH, DEFAULT_STATE_DIR, FACE_MODEL_URL, Config, ConfigError
from .db import connect
from .ingest import ingest
from .library import curate, verify, write_manifest
from .moments import cluster, score

app = typer.Typer(no_args_is_help=True, help="Local photo curation. See DESIGN.md.")

ConfigOpt = Annotated[Path, typer.Option("--config", "-c", help="Path to psort.toml.")]
_config_path = DEFAULT_CONFIG_PATH


@app.callback()
def main(config: ConfigOpt = DEFAULT_CONFIG_PATH) -> None:
    global _config_path
    _config_path = config


def _open() -> tuple[Config, sqlite3.Connection]:
    try:
        cfg = config_mod.load(_config_path)
    except ConfigError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    return cfg, connect(cfg.db_path)


@app.command()
def init(
    inbox: Annotated[Path, typer.Option(help="Folder you drop photo batches into. psort never writes here.")],
    library: Annotated[Path, typer.Option(help="Where the curated library is built.")],
    outbox: Annotated[Path, typer.Option(help="Where exports for blog posts go.")],
    state_dir: Annotated[Path, typer.Option(help="psort's database and models (keep inside WSL).")] = DEFAULT_STATE_DIR,
    face_model: Annotated[bool, typer.Option(help="Download the face-detection model (~230 KB).")] = True,
    force: Annotated[bool, typer.Option(help="Overwrite an existing config.")] = False,
) -> None:
    """Create psort.toml and the state database."""
    if _config_path.exists() and not force:
        typer.secho(f"{_config_path} already exists (use --force to overwrite).", fg="red", err=True)
        raise typer.Exit(1)
    inbox, library, outbox, state_dir = (p.expanduser().resolve() for p in (inbox, library, outbox, state_dir))
    config_mod.write_default(_config_path, inbox, library, outbox, state_dir)
    cfg = config_mod.load(_config_path)
    connect(cfg.db_path).close()
    library.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Wrote {_config_path}")
    if not inbox.is_dir():
        typer.secho(f"Note: inbox {inbox} doesn't exist yet.", fg="yellow")
    if face_model and not cfg.face_model.exists():
        cfg.face_model.parent.mkdir(parents=True, exist_ok=True)
        partial = cfg.face_model.with_name(cfg.face_model.name + ".partial")
        try:
            urllib.request.urlretrieve(FACE_MODEL_URL, partial)
            os.replace(partial, cfg.face_model)
            typer.echo(f"Downloaded face model to {cfg.face_model}")
        except OSError as e:
            typer.secho(f"Couldn't download face model ({e}); continuing without face scoring.", fg="yellow")


@app.command("ingest")
def ingest_cmd() -> None:
    """Scan the inbox and record every file."""
    cfg, conn = _open()
    _ingest(cfg, conn)


@app.command("cluster")
def cluster_cmd() -> None:
    """Group near-duplicate bursts into moments."""
    cfg, conn = _open()
    typer.echo(f"Moments: {cluster(cfg, conn)}")


@app.command("score")
def score_cmd() -> None:
    """Score photos and pick each moment's best shot."""
    cfg, conn = _open()
    score(cfg, conn)
    typer.echo("Scored.")


@app.command("curate")
def curate_cmd(dry_run: Annotated[bool, typer.Option(help="Show what would change.")] = False) -> None:
    """Copy and arrange photos in the library."""
    cfg, conn = _open()
    _curate(cfg, conn, dry_run)


@app.command()
def run(dry_run: Annotated[bool, typer.Option(help="Don't touch the library; show what would change.")] = False) -> None:
    """Ingest → cluster → score → curate."""
    cfg, conn = _open()
    _ingest(cfg, conn)
    typer.echo(f"Moments: {cluster(cfg, conn)}")
    score(cfg, conn)
    _curate(cfg, conn, dry_run)


@app.command()
def status() -> None:
    """Library totals."""
    cfg, conn = _open()
    counts = {
        "Photos": "SELECT COUNT(*) FROM photos",
        "Moments": "SELECT COUNT(DISTINCT moment_id) FROM photos",
        "Alternates": "SELECT COUNT(*) FROM photos WHERE is_best = 0",
        "Exact duplicates": "SELECT COUNT(*) - COUNT(DISTINCT sha256) FROM sources WHERE status = 'image'",
        "Undated": "SELECT COUNT(*) FROM photos WHERE date_source = 'mtime'",
        "Screenshots": "SELECT COUNT(*) FROM photos WHERE is_screenshot = 1",
        "Not in library": "SELECT COUNT(*) FROM photos WHERE library_path IS NULL",
        "Skipped files": "SELECT COUNT(*) FROM sources WHERE status = 'skipped'",
        "Unreadable": "SELECT COUNT(*) FROM sources WHERE status = 'error'",
    }
    for label, sql in counts.items():
        typer.echo(f"{label + ':':18}{conn.execute(sql).fetchone()[0]}")
    typer.echo(f"{'Face detection:':18}{'on' if cfg.face_model.exists() else 'off (model not downloaded)'}")


@app.command("verify")
def verify_cmd(
    batch: Annotated[str, typer.Argument(help="Batch folder name inside the inbox.")],
    show_ok: Annotated[bool, typer.Option("--all", help="List safe files too.")] = False,
) -> None:
    """Report whether an inbox batch is safe to delete."""
    cfg, conn = _open()
    try:
        results = verify(cfg, conn, batch)
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    problems = [r for r in results if not r.ok]
    for r in results if show_ok else problems:
        typer.echo(f"{'✅' if r.ok else '⚠️ '} {r.path}  —  {r.message}")
    if problems:
        typer.secho(f"{len(problems)} of {len(results)} files are NOT in the library. Don't delete yet.", fg="yellow")
        raise typer.Exit(2)
    typer.secho(f"All {len(results)} files are in the library. {batch!r} is safe to delete.", fg="green")


def _ingest(cfg: Config, conn: sqlite3.Connection) -> None:
    try:
        s = ingest(cfg, conn, log=typer.echo)
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from e
    typer.echo(
        f"Ingest: {s.new_photos} new, {s.duplicates} exact duplicates, {s.unchanged} unchanged, "
        f"{s.skipped} skipped, {s.errors} unreadable"
    )


def _curate(cfg: Config, conn: sqlite3.Connection, dry_run: bool) -> None:
    s = curate(cfg, conn, dry_run=dry_run, log=typer.echo)
    prefix = "Would curate" if dry_run else "Curate"
    typer.echo(f"{prefix}: {s.copied} copied, {s.moved} moved, {s.unchanged} unchanged")
    if s.missing:
        typer.secho(
            f"{len(s.missing)} photos have no library copy and their inbox files are gone: "
            + ", ".join(s.missing[:10]),
            fg="red",
        )
    if not dry_run:
        write_manifest(cfg, conn)

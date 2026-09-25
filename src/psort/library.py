"""Stage 4: build the library from the inbox, plus verify and the manifest (DESIGN.md §5.4–5.5)."""

import json
import os
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .events import named_ranges, slug_for
from .imaging import sha256_file
from .ingest import inbox_files

UNDATED_DIR = "_undated"
ALTERNATES_DIR = "_alternates"
DUPLICATES_DIR = "_duplicates"


@dataclass
class CurateStats:
    copied: int = 0
    moved: int = 0
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)  # names with no library copy and no inbox source


def assign_names(conn: sqlite3.Connection) -> None:
    """Give each new photo a permanent library name, YYYYMMDD_HHMMSS[_n]. Names never change once
    assigned, so a name used on the blog keeps pointing at the same photo."""
    taken = {r["name"] for r in conn.execute("SELECT name FROM photos WHERE name IS NOT NULL")}
    # Same-second photos are numbered in original-filename order, which follows the burst sequence.
    rows = conn.execute(
        """SELECT p.sha256, p.taken_at,
                  (SELECT MIN(s.path) FROM sources s WHERE s.sha256 = p.sha256) AS first_path
           FROM photos p WHERE p.name IS NULL ORDER BY p.taken_at, first_path, p.sha256"""
    ).fetchall()
    for row in rows:
        stem = datetime.fromisoformat(row["taken_at"]).strftime("%Y%m%d_%H%M%S")
        name, n = stem, 0
        while name in taken:
            n += 1
            name = f"{stem}_{n}"
        taken.add(name)
        conn.execute("UPDATE photos SET name = ? WHERE sha256 = ?", (name, row["sha256"]))
    conn.commit()


def desired_paths(conn: sqlite3.Connection) -> dict[str, str]:
    """sha256 → library-relative path, from the current moments and best picks."""
    rows = conn.execute(
        "SELECT sha256, name, ext, taken_at, date_source, moment_id, is_best, duplicate_of FROM photos"
    ).fetchall()
    names = {r["sha256"]: r["name"] for r in rows}
    best = {r["moment_id"]: r for r in rows if r["is_best"]}
    ranges = named_ranges(conn)
    paths = {}
    for r in rows:
        if r["date_source"] == "mtime":
            paths[r["sha256"]] = f"{UNDATED_DIR}/{r['name']}{r['ext']}"
            continue
        b = best[r["moment_id"]]
        day = b["taken_at"][:10]  # alternates live with their best shot, even across midnight
        slug = slug_for(b["taken_at"], ranges)
        folder = f"{day[:4]}/{day}_{slug}" if slug else f"{day[:4]}/{day}"
        if r["is_best"]:
            paths[r["sha256"]] = f"{folder}/{r['name']}{r['ext']}"
        elif r["duplicate_of"]:
            # Grouped under the copy that was kept, e.g. _duplicates/20260703_145634/…
            paths[r["sha256"]] = f"{folder}/{DUPLICATES_DIR}/{names[r['duplicate_of']]}/{r['name']}{r['ext']}"
        else:
            paths[r["sha256"]] = f"{folder}/{ALTERNATES_DIR}/{b['name']}/{r['name']}{r['ext']}"
    return paths


def _remove_empty_parents(path: Path, root: Path) -> None:
    parent = path.parent
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _copy_verified(src: Path, dest: Path, sha: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    shutil.copy2(src, partial)
    if sha256_file(partial) != sha:
        partial.unlink()
        raise OSError(f"Copy of {src} did not verify")
    os.replace(partial, dest)


def curate(
    cfg: Config, conn: sqlite3.Connection, dry_run: bool = False, log: Callable[[str], None] = print
) -> CurateStats:
    assign_names(conn)
    stats = CurateStats()
    lib = cfg.library
    current = {r["sha256"]: r for r in conn.execute("SELECT sha256, name, library_path FROM photos")}

    for sha, target in sorted(desired_paths(conn).items(), key=lambda kv: kv[1]):
        row = current[sha]
        dest = lib / target
        existing = lib / row["library_path"] if row["library_path"] else None

        if row["library_path"] == target and dest.exists():
            stats.unchanged += 1
            continue

        if dest.exists():
            # Something already sits at the target. Adopt it only if it's this exact photo.
            if sha256_file(dest) != sha:
                raise FileExistsError(f"{dest} exists and is a different file; refusing to overwrite")
            action = "adopt"
        elif existing and existing.exists():
            action = "move"
        else:
            action = "copy"

        if action == "move":
            log(f"  move {row['library_path']} → {target}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(existing, dest)
                _remove_empty_parents(existing, lib)
            stats.moved += 1
        elif action == "copy":
            src = _find_source(cfg, conn, sha)
            if src is None:
                stats.missing.append(row["name"])
                continue
            if dry_run:
                log(f"  copy {src.relative_to(cfg.inbox)} → {target}")
            else:
                _copy_verified(src, dest, sha)
            stats.copied += 1

        if not dry_run:
            conn.execute("UPDATE photos SET library_path = ? WHERE sha256 = ?", (target, sha))
            conn.commit()

    return stats


def _find_source(cfg: Config, conn: sqlite3.Connection, sha: str) -> Path | None:
    for r in conn.execute("SELECT path FROM sources WHERE sha256 = ? ORDER BY path", (sha,)):
        path = cfg.inbox / r["path"]
        if path.exists():
            return path
    return None


@dataclass
class VerifyResult:
    path: str
    ok: bool
    message: str


def verify(cfg: Config, conn: sqlite3.Connection, batch: str) -> list[VerifyResult]:
    """Is every file in an inbox batch safe to delete? (DESIGN.md §5.5)"""
    batch_dir = (cfg.inbox / batch).resolve()
    if not batch_dir.is_dir() or not batch_dir.is_relative_to(cfg.inbox.resolve()):
        raise FileNotFoundError(f"No batch folder {batch!r} in {cfg.inbox}")

    results = []
    for path, rel in inbox_files(cfg.inbox.resolve(), batch_dir):
        src = conn.execute("SELECT * FROM sources WHERE path = ?", (str(rel),)).fetchone()
        st = path.stat()
        if src is None:
            results.append(VerifyResult(str(rel), False, "not ingested yet (run `psort run`)"))
        elif src["size"] != st.st_size or src["mtime"] != st.st_mtime:
            results.append(VerifyResult(str(rel), False, "changed since it was ingested (run `psort run`)"))
        elif src["status"] != "image":
            results.append(VerifyResult(str(rel), False, f"not in library: {src['reason']}"))
        else:
            photo = conn.execute("SELECT library_path FROM photos WHERE sha256 = ?", (src["sha256"],)).fetchone()
            if photo["library_path"] and (cfg.library / photo["library_path"]).exists():
                results.append(VerifyResult(str(rel), True, photo["library_path"]))
            else:
                results.append(VerifyResult(str(rel), False, "not copied to the library yet (run `psort run`)"))
    return results


def write_manifest(cfg: Config, conn: sqlite3.Connection) -> Path:
    """A JSON copy of the state inside the library, so the library documents itself (DESIGN.md §2)."""
    people = {}
    for r in conn.execute(
        "SELECT DISTINCT f.sha256, p.name FROM faces f JOIN people p ON p.id = f.person_id ORDER BY p.name"
    ):
        people.setdefault(r["sha256"], []).append(r["name"])
    tags = {}
    for r in conn.execute("SELECT sha256, tag FROM tags ORDER BY tag"):
        tags.setdefault(r["sha256"], []).append(r["tag"])
    photos = [
        {**dict(r), "people": people.get(r["sha256"], []), "tags": tags.get(r["sha256"], [])}
        for r in conn.execute("SELECT * FROM photos ORDER BY taken_at, name")
    ]
    events = [dict(r) for r in named_ranges(conn)]
    sources = [dict(r) for r in conn.execute("SELECT path, sha256 FROM sources WHERE status = 'image' ORDER BY path")]
    out = cfg.library / ".psort" / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    partial.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "photos": photos,
                "events": events,
                "sources": sources,
            },
            indent=1,
        )
    )
    os.replace(partial, out)
    return out

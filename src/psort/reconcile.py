"""Reconcile (DESIGN.md §5.11): make psort's records match files you moved, renamed or deleted by
hand in the library, videos/ or unsorted_files/.

- A file psort expects that turns up somewhere else (same contents) is adopted where you put it;
  psort then files it back into its usual place, with no re-copy and no duplicate.
- A photo whose file is gone everywhere is listed; with --apply it's recorded as deleted, so it's
  never copied back from the inbox.
- Files psort didn't put there are listed and never touched.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .imaging import is_junk, sha256_file
from .trash import forget_missing


@dataclass
class Expected:
    table: str
    sha: str
    root: Path
    path: str
    size: int | None
    label: str


@dataclass
class Report:
    moved: list[tuple[str, str, str]] = field(default_factory=list)  # (label, old path, new path)
    missing: list[tuple[str, str]] = field(default_factory=list)  # (label, path)
    unknown: list[str] = field(default_factory=list)


def _expected(cfg: Config, conn: sqlite3.Connection) -> list[Expected]:
    size = "(SELECT MAX(s.size) FROM sources s WHERE s.sha256 = t.sha256)"
    specs = [
        ("photos", cfg.library, "library_path", "photo"),
        ("live_clips", cfg.library, "library_path", "Live Photo clip"),
        ("deleted_photos", cfg.library, "trash_path", "trashed photo"),
        ("videos", cfg.videos, "library_path", "video"),
        ("other_files", cfg.unsorted, "library_path", "unsorted file"),
    ]
    out = []
    for table, root, col, label in specs:
        for r in conn.execute(f"SELECT t.sha256, t.{col} AS path, {size} AS size FROM {table} t WHERE t.{col} IS NOT NULL"):
            out.append(Expected(table, r["sha256"], root, r["path"], r["size"], label))
    return out


def _files(root: Path) -> list[str]:
    if not root.exists():
        return []
    return [str(p.relative_to(root)) for p in root.rglob("*")
            if p.is_file() and ".psort" not in p.relative_to(root).parts and not is_junk(p)]


def reconcile(cfg: Config, conn: sqlite3.Connection, apply: bool = False) -> Report:
    report = Report()
    expected = _expected(cfg, conn)
    roots = {cfg.library, cfg.videos, cfg.unsorted}
    on_disk = {root: set(_files(root)) for root in roots}
    claimed = {(e.root, e.path) for e in expected}
    unknown = {root: {f for f in files if (root, f) not in claimed} for root, files in on_disk.items()}

    for e in expected:
        if e.path in on_disk[e.root]:
            continue
        # Look for it among files psort doesn't know about: same size first, then same contents.
        match = next((f for f in sorted(unknown[e.root])
                      if (e.size is None or (e.root / f).stat().st_size == e.size) and sha256_file(e.root / f) == e.sha),
                     None)
        if match:
            report.moved.append((e.label, e.path, match))
            unknown[e.root].discard(match)
            if apply:
                col = "trash_path" if e.table == "deleted_photos" else "library_path"
                conn.execute(f"UPDATE {e.table} SET {col} = ? WHERE sha256 = ?", (match, e.sha))
        else:
            report.missing.append((e.label, e.path))
            if apply and e.table == "photos":
                forget_missing(conn, e.sha)  # deleted by hand: don't copy it back
            elif apply and e.table == "deleted_photos":
                conn.execute("UPDATE deleted_photos SET purged = 1, trash_path = NULL WHERE sha256 = ?", (e.sha,))
    conn.commit()
    for root in sorted(roots):
        report.unknown += [str(root / f) for f in sorted(unknown[root])]
    return report

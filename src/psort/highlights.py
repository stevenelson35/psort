"""Favorites and the highlights/ folder (DESIGN.md §5.9).

Favorites live in the database. highlights/ holds a small web-size JPEG of each one, at the same
folder path and name as its original in the library, e.g.

    library/2016/2016-04-17/_alternates/20160417_062036/20160417_062036_1.heic
    highlights/2016/2016-04-17/20160417_062036_1.jpg

Each copy's Windows Title/Subject is the original's library path, and its Tags are the people in it
plus your tags. psort keeps the folder in sync: un-favoriting removes the copy, moving the original
(best pick, event name, date fix) moves it, and people/tag changes re-render it. psort only ever
touches files it wrote; anything else you put in highlights/ is left alone.
"""

import hashlib
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .export import render
from .library import _remove_empty_parents


class HighlightError(Exception):
    pass


def toggle_favorite(conn: sqlite3.Connection, sha: str) -> bool:
    """Returns True if the photo is now a favorite."""
    if not conn.execute("SELECT 1 FROM photos WHERE sha256 = ?", (sha,)).fetchone():
        raise HighlightError(f"No photo {sha[:12]}…")
    if conn.execute("DELETE FROM favorites WHERE sha256 = ?", (sha,)).rowcount:
        conn.commit()
        return False
    conn.execute("INSERT INTO favorites (sha256) VALUES (?)", (sha,))
    conn.commit()
    return True


def highlight_path(library_path: str, name: str) -> str:
    """The library folder (dropping _alternates/…/_duplicates/… levels) + name + .jpg."""
    parts = library_path.split("/")[:-1]
    for i, part in enumerate(parts):
        if part in ("_alternates", "_duplicates"):
            parts = parts[:i]
            break
    return "/".join([*parts, f"{name}.jpg"])


def _wanted(conn: sqlite3.Connection) -> dict[str, tuple[str, str, str, list[str]]]:
    """sha256 → (highlight path, stamp, library path, keywords) for every favorite in the library."""
    wanted = {}
    rows = conn.execute(
        """SELECT p.sha256, p.name, p.library_path,
                  (SELECT group_concat(name, ';') FROM (SELECT DISTINCT pe.name FROM faces f
                      JOIN people pe ON pe.id = f.person_id WHERE f.sha256 = p.sha256 ORDER BY pe.name)) AS people,
                  (SELECT group_concat(tag, ';') FROM (SELECT tag FROM tags WHERE sha256 = p.sha256 ORDER BY tag)) AS tags
           FROM favorites fav JOIN photos p ON p.sha256 = fav.sha256 WHERE p.library_path IS NOT NULL"""
    ).fetchall()
    for r in rows:
        keywords = [k for k in (r["people"] or "").split(";") + (r["tags"] or "").split(";") if k]
        stamp = hashlib.sha1(f"{r['library_path']}|{';'.join(keywords)}".encode()).hexdigest()
        wanted[r["sha256"]] = (highlight_path(r["library_path"], r["name"]), stamp, r["library_path"], keywords)
    return wanted


@dataclass
class SyncStats:
    written: int = 0
    moved: int = 0
    removed: int = 0


def sync(cfg: Config, conn: sqlite3.Connection, log: Callable[[str], None] = lambda _: None) -> SyncStats:
    """Make highlights/ match the favorites exactly (for the files psort wrote)."""
    root = cfg.highlights
    stats = SyncStats()
    wanted = _wanted(conn)
    have = {r["sha256"]: r for r in conn.execute("SELECT * FROM highlights")}

    for sha, row in have.items():  # no longer a favorite (or its original is gone)
        if sha not in wanted:
            old = root / row["path"]
            if old.exists():
                old.unlink()
                _remove_empty_parents(old, root)
            conn.execute("DELETE FROM highlights WHERE sha256 = ?", (sha,))
            log(f"  remove highlight {row['path']}")
            stats.removed += 1

    for sha, (path, stamp, library_path, keywords) in wanted.items():
        row, dest = have.get(sha), root / path
        if row and row["path"] == path and row["stamp"] == stamp and dest.exists():
            continue
        old = root / row["path"] if row else None
        if row and row["stamp"] == stamp and old.exists() and row["path"] != path:
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(old, dest)
            _remove_empty_parents(old, root)
            log(f"  move highlight {row['path']} → {path}")
            stats.moved += 1
        else:
            src = cfg.library / library_path
            if not src.exists():
                continue
            render(src, dest, description=f"psort library: {library_path}", keywords=keywords)
            if old and old != dest and old.exists():
                old.unlink()
                _remove_empty_parents(old, root)
            log(f"  write highlight {path}")
            stats.written += 1
        conn.execute("INSERT OR REPLACE INTO highlights (sha256, path, stamp) VALUES (?, ?, ?)", (sha, path, stamp))
        conn.commit()
    conn.commit()
    return stats

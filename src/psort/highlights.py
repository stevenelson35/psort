"""Favorites and the highlights/ folder (DESIGN.md §5.9).

Favorites live in the database. highlights/ holds a small web-size JPEG of each one, at the same
folder path and name as its original in the library, e.g.

    library/2016/2016-04-17/_alternates/20160417_062036/20160417_062036_1.heic
    highlights/2016/2016-04-17/20160417_062036_1.jpg

Each copy's Windows Title/Subject is the original's library path, and its Tags are the people in it
plus your tags. psort keeps the folder in sync: un-favoriting removes the copy, moving the original
(best pick, event name, date fix) moves it, and people/tag changes re-render it. psort only ever
touches files it wrote; anything else you put in highlights/ is left alone.

A **top pick** (◆) is a favorite you rank higher. Marking one makes it a favorite too, so it has a
highlights/ copy, and it also gets the same web-size copy in top_picks/, which is flat (just
`<name>.jpg`, no folders) so the whole set is easy to copy or show off.
"""

import hashlib
import os
import shutil
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


def toggle_top(conn: sqlite3.Connection, sha: str) -> bool:
    """Returns True if the photo is now a top pick. Marking one also makes it a favorite;
    un-marking leaves it a favorite."""
    if not conn.execute("SELECT 1 FROM photos WHERE sha256 = ?", (sha,)).fetchone():
        raise HighlightError(f"No photo {sha[:12]}…")
    row = conn.execute("SELECT top FROM favorites WHERE sha256 = ?", (sha,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO favorites (sha256, top) VALUES (?, 1)", (sha,))
        now = True
    else:
        now = not row["top"]
        conn.execute("UPDATE favorites SET top = ? WHERE sha256 = ?", (int(now), sha))
    conn.commit()
    return now


def highlight_path(library_path: str, name: str) -> str:
    """The library folder (dropping _alternates/…/_duplicates/… levels) + name + .jpg."""
    parts = library_path.split("/")[:-1]
    for i, part in enumerate(parts):
        if part in ("_alternates", "_duplicates"):
            parts = parts[:i]
            break
    return "/".join([*parts, f"{name}.jpg"])


def _only(shas: set[str] | None, column: str) -> tuple[str, list[str]]:
    """An SQL condition (and parameters) limiting a query to some photos; all when None."""
    if shas is None:
        return "", []
    return f"AND {column} IN ({','.join('?' * len(shas)) or 'NULL'})", sorted(shas)


def _wanted(conn: sqlite3.Connection, top_only: bool = False,
            shas: set[str] | None = None) -> dict[str, tuple[str, str, str, list[str]]]:
    """sha256 → (copy path, stamp, library path, keywords) for every favorite in the library, or
    every top pick (flat: just `<name>.jpg`; names are unique). `shas` limits it to those photos."""
    wanted = {}
    only, params = _only(shas, "p.sha256")
    rows = conn.execute(
        f"""SELECT p.sha256, p.name, p.library_path,
                  (SELECT group_concat(name, ';') FROM (SELECT DISTINCT pe.name FROM faces f
                      JOIN people pe ON pe.id = f.person_id WHERE f.sha256 = p.sha256 ORDER BY pe.name)) AS people,
                  (SELECT group_concat(tag, ';') FROM (SELECT tag FROM tags WHERE sha256 = p.sha256 ORDER BY tag)) AS tags
           FROM favorites fav JOIN photos p ON p.sha256 = fav.sha256 WHERE p.library_path IS NOT NULL
           {"AND fav.top = 1" if top_only else ""} {only} ORDER BY p.name, p.sha256""",
        params,
    ).fetchall()
    for r in rows:
        keywords = [k for k in (r["people"] or "").split(";") + (r["tags"] or "").split(";") if k]
        stamp = hashlib.sha1(f"{r['library_path']}|{';'.join(keywords)}".encode()).hexdigest()
        path = f"{r['name']}.jpg" if top_only else highlight_path(r["library_path"], r["name"])
        wanted[r["sha256"]] = (path, stamp, r["library_path"], keywords)
    return wanted


@dataclass
class SyncStats:
    written: int = 0
    moved: int = 0
    removed: int = 0
    top: "SyncStats | None" = None  # the same counts for top_picks/


def sync(cfg: Config, conn: sqlite3.Connection, log: Callable[[str], None] = lambda _: None,
         shas: set[str] | None = None) -> SyncStats:
    """Make highlights/ match the favorites and top_picks/ match the top picks (for the files psort
    wrote). `shas` limits it to those photos, for a review click: checking every copy on OneDrive
    is `psort run`'s job."""
    stats = _sync_folder(cfg, conn, cfg.highlights, "highlights", _wanted(conn, shas=shas), "highlight", log, shas)
    stats.top = _sync_folder(cfg, conn, cfg.top_picks, "top_picks", _wanted(conn, top_only=True, shas=shas),
                             "top pick", log, shas)
    return stats


def _sync_folder(cfg: Config, conn: sqlite3.Connection, root: Path, table: str, wanted: dict,
                 label: str, log: Callable[[str], None], shas: set[str] | None = None) -> SyncStats:
    stats = SyncStats()
    only, params = _only(shas, "sha256")
    have = {r["sha256"]: r for r in conn.execute(f"SELECT * FROM {table} WHERE 1 {only}", params)}

    for sha, row in have.items():  # no longer wanted (or its original is gone)
        if sha not in wanted:
            old = root / row["path"]
            if old.exists():
                old.unlink()
                _remove_empty_parents(old, root)
            conn.execute(f"DELETE FROM {table} WHERE sha256 = ?", (sha,))
            log(f"  remove {label} {row['path']}")
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
            log(f"  move {label} {row['path']} → {path}")
            stats.moved += 1
        else:
            src = cfg.library / library_path
            if not src.exists():
                continue
            render(src, dest, description=f"psort library: {library_path}", keywords=keywords)
            if old and old != dest and old.exists():
                old.unlink()
                _remove_empty_parents(old, root)
            log(f"  write {label} {path}")
            stats.written += 1
        conn.execute(f"INSERT OR REPLACE INTO {table} (sha256, path, stamp) VALUES (?, ?, ?)", (sha, path, stamp))
        conn.commit()
    conn.commit()
    return stats


def export_top_picks(cfg: Config, conn: sqlite3.Connection, out: Path, originals: bool = False,
                     log: Callable[[str], None] = lambda _: None) -> tuple[int, int]:
    """Copy every top pick, flat, into `out`: web-size JPEGs, or with `originals` the library files
    byte-for-byte. Files already there are left alone. Returns (copied, skipped)."""
    copied = skipped = 0
    for path, _stamp, library_path, keywords in _wanted(conn, top_only=True).values():
        src = cfg.library / library_path
        dest = out / (Path(path).stem + Path(library_path).suffix if originals else path)
        if dest.exists() or not src.exists():
            skipped += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if originals:
            shutil.copy2(src, dest)
        else:
            render(src, dest, description=f"psort library: {library_path}", keywords=keywords)
        log(f"  copy {dest.name}")
        copied += 1
    return copied, skipped

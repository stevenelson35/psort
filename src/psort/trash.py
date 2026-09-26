"""Deleting photos (DESIGN.md §5.10).

Delete moves a photo (and its Live Photo clip) into library/_trash/ and out of every view, and
remembers it so the inbox copy is never copied back. Restore puts it back; Empty trash deletes the
files for good (psort still remembers them, so they stay gone).
"""

import json
import os
import sqlite3
from pathlib import Path

from .config import Config
from .library import _remove_empty_parents

TRASH_DIR = "_trash"


class TrashError(Exception):
    pass


def _move(root: Path, rel: str | None, new_rel: str) -> str | None:
    """Move root/rel → root/new_rel if it exists. Returns new_rel, or None if there was no file."""
    if not rel or not (root / rel).exists():
        return None
    dest = root / new_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(root / rel, dest)
    _remove_empty_parents(root / rel, root)
    return new_rel


def _clips_of(conn: sqlite3.Connection, sha: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT DISTINCT c.sha256, c.library_path FROM live_clips c JOIN sources s ON s.path = c.photo_path "
        "WHERE s.sha256 = ?", (sha,)
    ).fetchall()


def delete(cfg: Config, conn: sqlite3.Connection, shas: list[str]) -> int:
    """Move photos to the trash. Call actions.refresh afterwards to re-pick best shots."""
    if not shas:
        raise TrashError("Tick at least one photo.")
    lib = cfg.library
    for sha in shas:
        row = conn.execute("SELECT * FROM photos WHERE sha256 = ?", (sha,)).fetchone()
        if row is None:
            raise TrashError(f"No photo {sha[:12]}…")
        trash_path = _move(lib, row["library_path"], f"{TRASH_DIR}/{row['library_path']}") if row["library_path"] else None
        for clip in _clips_of(conn, sha):  # the Live Photo clip goes with it
            moved = _move(lib, clip["library_path"], f"{TRASH_DIR}/{clip['library_path']}")
            if moved:
                conn.execute("UPDATE live_clips SET library_path = ? WHERE sha256 = ?", (moved, clip["sha256"]))
        conn.execute(
            "INSERT OR REPLACE INTO deleted_photos (sha256, name, ext, taken_at, data, trash_path) VALUES (?,?,?,?,?,?)",
            (sha, row["name"], row["ext"], row["taken_at"], json.dumps(dict(row)), trash_path),
        )
        conn.execute("DELETE FROM photos WHERE sha256 = ?", (sha,))
        face_ids = [r["id"] for r in conn.execute("SELECT id FROM faces WHERE sha256 = ?", (sha,))]
        conn.executemany("DELETE FROM face_rejections WHERE face_id = ?", [(i,) for i in face_ids])
        conn.execute("DELETE FROM faces WHERE sha256 = ?", (sha,))
        conn.execute("DELETE FROM favorites WHERE sha256 = ?", (sha,))  # highlights sync removes the copy
        conn.execute("DELETE FROM tray WHERE sha256 = ?", (sha,))
    conn.execute("DELETE FROM people WHERE id NOT IN (SELECT person_id FROM faces WHERE person_id IS NOT NULL)")
    conn.commit()
    return len(shas)


def restore(cfg: Config, conn: sqlite3.Connection, sha: str) -> None:
    """Back into the library; call actions.refresh(recluster=True) to put it in its folder."""
    row = conn.execute("SELECT * FROM deleted_photos WHERE sha256 = ?", (sha,)).fetchone()
    if row is None:
        raise TrashError("That photo isn't in the trash.")
    if row["purged"]:
        raise TrashError("That photo was deleted for good and can't be restored.")
    data = json.loads(row["data"])
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
    data.update(library_path=row["trash_path"], moment_id=None, is_best=0, user_best=0, close_call=0,
                duplicate_of=None, faces_scanned=0)  # curate moves it out of the trash; faces are re-found
    data = {k: v for k, v in data.items() if k in columns}
    conn.execute(f"INSERT INTO photos ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})", list(data.values()))
    conn.execute("DELETE FROM deleted_photos WHERE sha256 = ?", (sha,))
    conn.commit()


def empty(cfg: Config, conn: sqlite3.Connection) -> int:
    """Delete trashed files for good. They stay remembered, so they're never copied back."""
    lib = cfg.library
    rows = conn.execute("SELECT sha256, trash_path FROM deleted_photos WHERE purged = 0").fetchall()
    for r in rows:
        if r["trash_path"] and (lib / r["trash_path"]).exists():
            (lib / r["trash_path"]).unlink()
            _remove_empty_parents(lib / r["trash_path"], lib)
        for clip in _clips_of(conn, r["sha256"]):
            if clip["library_path"] and clip["library_path"].startswith(TRASH_DIR + "/"):
                if (lib / clip["library_path"]).exists():
                    (lib / clip["library_path"]).unlink()
                    _remove_empty_parents(lib / clip["library_path"], lib)
                conn.execute("UPDATE live_clips SET library_path = NULL WHERE sha256 = ?", (clip["sha256"],))
        conn.execute("UPDATE deleted_photos SET purged = 1, trash_path = NULL WHERE sha256 = ?", (r["sha256"],))
    conn.commit()
    return len(rows)


def forget_missing(conn: sqlite3.Connection, sha: str) -> None:
    """A photo whose library file was deleted by hand: record it as deleted for good."""
    row = conn.execute("SELECT * FROM photos WHERE sha256 = ?", (sha,)).fetchone()
    if row is None:
        return
    conn.execute(
        "INSERT OR REPLACE INTO deleted_photos (sha256, name, ext, taken_at, data, trash_path, purged) "
        "VALUES (?,?,?,?,?,NULL,1)", (sha, row["name"], row["ext"], row["taken_at"], json.dumps(dict(row))),
    )
    conn.execute("DELETE FROM photos WHERE sha256 = ?", (sha,))
    for table in ("faces", "favorites", "tray"):
        conn.execute(f"DELETE FROM {table} WHERE sha256 = ?", (sha,))
    conn.commit()

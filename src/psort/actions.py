"""Review decisions, shared by the review UI and the CLI. Each one updates the database and then
rearranges the library to match."""

import sqlite3
from datetime import datetime

from .config import Config
from .library import assign_names, curate, write_manifest
from .moments import cluster, score


class ActionError(Exception):
    pass


def refresh(cfg: Config, conn: sqlite3.Connection, recluster: bool = False) -> None:
    """Re-derive moments/best picks and move library files to match."""
    if recluster:
        cluster(cfg, conn)
    score(cfg, conn)
    curate(cfg, conn, log=lambda _: None)
    write_manifest(cfg, conn)


def _photo(conn: sqlite3.Connection, sha: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM photos WHERE sha256 = ?", (sha,)).fetchone()
    if row is None:
        raise ActionError(f"No photo {sha[:12]}…")
    return row


def pick_best(cfg: Config, conn: sqlite3.Connection, sha: str) -> None:
    """Your choice of best shot for its moment. It sticks through every later run."""
    photo = _photo(conn, sha)
    conn.execute("UPDATE photos SET user_best = (sha256 = ?) WHERE moment_id = ?", (sha, photo["moment_id"]))
    conn.commit()
    refresh(cfg, conn)


def clear_pick(cfg: Config, conn: sqlite3.Connection, moment_id: str) -> None:
    """Go back to the automatic best pick."""
    conn.execute("UPDATE photos SET user_best = 0 WHERE moment_id = ?", (moment_id,))
    conn.commit()
    refresh(cfg, conn)


def set_date(cfg: Config, conn: sqlite3.Connection, sha: str, when: datetime) -> None:
    """Correct a photo's capture time. Its library name follows the new date unless it's in the
    post tray (so a name you may be about to publish doesn't change underneath you)."""
    _photo(conn, sha)
    in_tray = conn.execute("SELECT 1 FROM tray WHERE sha256 = ?", (sha,)).fetchone()
    conn.execute(
        "UPDATE photos SET taken_at = ?, date_source = 'user', user_best = 0" + ("" if in_tray else ", name = NULL")
        + " WHERE sha256 = ?",
        (when.replace(microsecond=0).isoformat(), sha),
    )
    conn.commit()
    assign_names(conn)
    refresh(cfg, conn, recluster=True)


def set_tags(cfg: Config, conn: sqlite3.Connection, sha: str, text: str) -> list[str]:
    """Replace a photo's tags with a comma-separated list."""
    _photo(conn, sha)
    tags = sorted({t.strip().lower() for t in text.split(",") if t.strip()})
    conn.execute("DELETE FROM tags WHERE sha256 = ?", (sha,))
    conn.executemany("INSERT INTO tags (sha256, tag) VALUES (?, ?)", [(sha, t) for t in tags])
    conn.commit()
    write_manifest(cfg, conn)
    return tags


def toggle_tray(conn: sqlite3.Connection, sha: str) -> bool:
    """Add to / remove from the post tray. Returns True if it's now in the tray."""
    _photo(conn, sha)
    if conn.execute("DELETE FROM tray WHERE sha256 = ?", (sha,)).rowcount:
        conn.commit()
        return False
    conn.execute("INSERT INTO tray (sha256) VALUES (?)", (sha,))
    conn.commit()
    return True


def toggle_reviewed(conn: sqlite3.Connection, day: str) -> bool:
    """Mark a day reviewed (or not). Returns True if it's now reviewed."""
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as e:
        raise ActionError(f"Not a day: {day!r}") from e
    if conn.execute("DELETE FROM reviewed WHERE day = ?", (day,)).rowcount:
        conn.commit()
        return False
    conn.execute("INSERT INTO reviewed (day) VALUES (?)", (day,))
    conn.commit()
    return True

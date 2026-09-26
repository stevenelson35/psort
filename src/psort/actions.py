"""Review decisions, shared by the review UI and the CLI. Each one updates the database and then
rearranges the library to match."""

import sqlite3
from datetime import date, datetime, time

from . import highlights
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
    highlights.sync(cfg, conn)  # highlight copies follow their originals
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


def set_day(cfg: Config, conn: sqlite3.Connection, shas: list[str], day: date) -> int:
    """Give several photos the same day (time unknown), e.g. a batch of PhotoPass downloads.
    They leave the Undated list and go to that day's folder, but aren't grouped into bursts."""
    if not shas:
        raise ActionError("Tick at least one photo.")
    for sha in shas:
        _photo(conn, sha)
    noon = datetime.combine(day, time(12, 0)).isoformat()
    for sha in shas:
        in_tray = conn.execute("SELECT 1 FROM tray WHERE sha256 = ?", (sha,)).fetchone()
        conn.execute(
            "UPDATE photos SET taken_at = ?, date_source = 'user-day', user_best = 0"
            + ("" if in_tray else ", name = NULL") + " WHERE sha256 = ?",
            (noon, sha),
        )
    conn.commit()
    assign_names(conn)
    refresh(cfg, conn, recluster=True)
    return len(shas)


def set_tags(cfg: Config, conn: sqlite3.Connection, sha: str, text: str) -> list[str]:
    """Replace a photo's tags with a comma-separated list."""
    _photo(conn, sha)
    tags = sorted({t.strip().lower() for t in text.split(",") if t.strip()})
    conn.execute("DELETE FROM tags WHERE sha256 = ?", (sha,))
    conn.executemany("INSERT INTO tags (sha256, tag) VALUES (?, ?)", [(sha, t) for t in tags])
    conn.commit()
    highlights.sync(cfg, conn)  # tags show as Windows Tags on highlight copies
    write_manifest(cfg, conn)
    return tags


def toggle_tray(conn: sqlite3.Connection, sha: str) -> bool:
    """Add to / remove from the post tray. Returns True if it's now in the tray."""
    _photo(conn, sha)
    if conn.execute("DELETE FROM tray WHERE sha256 = ?", (sha,)).rowcount:
        conn.commit()
        return False
    conn.execute("INSERT INTO tray (sha256, position) VALUES (?, (SELECT COALESCE(MAX(position), 0) + 1 FROM tray))",
                 (sha,))
    conn.commit()
    return True


def move_in_tray(conn: sqlite3.Connection, sha: str, step: int) -> None:
    """Move a photo up (-1) or down (+1) in the post."""
    order = [r["sha256"] for r in conn.execute("SELECT t.sha256 FROM tray t JOIN photos p ON p.sha256 = t.sha256 "
                                               "ORDER BY t.position, p.taken_at, p.name")]
    if sha not in order:
        raise ActionError("That photo isn't in the post tray.")
    i = order.index(sha)
    j = max(0, min(len(order) - 1, i + step))
    order.insert(j, order.pop(i))
    conn.executemany("UPDATE tray SET position = ? WHERE sha256 = ?", [(n, s) for n, s in enumerate(order)])
    conn.commit()


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


def toggle_favorite(cfg: Config, conn: sqlite3.Connection, sha: str) -> bool:
    """Star / unstar a photo; its highlights/ copy appears or disappears to match."""
    try:
        now = highlights.toggle_favorite(conn, sha)
    except highlights.HighlightError as e:
        raise ActionError(str(e)) from e
    highlights.sync(cfg, conn)
    write_manifest(cfg, conn)
    return now

"""Review decisions, shared by the review UI and the CLI. Each one updates the database and then
rearranges the library to match."""

import sqlite3
import hashlib
from datetime import date, datetime, time

from . import daystatus, highlights, trash
from .config import Config
from .library import assign_names, curate, day_of, write_manifest
from .moments import cluster, score


class ActionError(Exception):
    pass


# The review UI sets this to write manifest.json in the background a few seconds after the last
# change (it's ~10 MB on OneDrive); the CLI leaves it unset and writes right away.
manifest_later = None


def _manifest(cfg: Config, conn: sqlite3.Connection) -> None:
    if manifest_later:
        manifest_later()
    else:
        write_manifest(cfg, conn)


def refresh(cfg: Config, conn: sqlite3.Connection, recluster: bool = False) -> None:
    """Re-derive moments/best picks and move just the library files that need to move. Files that
    stay put aren't re-checked on disk (that's `psort run`'s job), so a click takes a moment,
    not minutes."""
    if recluster:
        cluster(cfg, conn)
    score(cfg, conn)
    curate(cfg, conn, log=lambda _: None, check_files=False)
    highlights.sync(cfg, conn)  # highlight copies follow their originals
    _manifest(cfg, conn)


def _photo(conn: sqlite3.Connection, sha: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM photos WHERE sha256 = ?", (sha,)).fetchone()
    if row is None:
        raise ActionError(f"No photo {sha[:12]}…")
    return row


def _reviewed_days_of_moments(conn: sqlite3.Connection, moment_ids: set[str]) -> list[str]:
    """Currently-reviewed days holding these moments, to re-mark after an edit you made there."""
    if not moment_ids:
        return []
    marks = ",".join("?" * len(moment_ids))
    days = {day_of(r["library_path"]) for r in conn.execute(
        f"SELECT library_path FROM photos WHERE library_path IS NOT NULL AND moment_id IN ({marks})",
        tuple(moment_ids))}
    return daystatus.keep_reviewed(conn, {d for d in days if d})


def _remark(conn: sqlite3.Connection, days: list[str]) -> None:
    for day in days:
        daystatus.mark(conn, day)


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


def combine_moments(cfg: Config, conn: sqlite3.Connection, photo_shas: list[str]) -> int:
    """Combine the moments represented by selected day-page best shots; return moments combined."""
    shas = sorted(set(photo_shas))
    if len(shas) < 2:
        raise ActionError("Select best shots from at least two moments to combine them.")
    marks = ",".join("?" * len(shas))
    selected = conn.execute(
        f"SELECT sha256, moment_id, is_best FROM photos WHERE sha256 IN ({marks})", shas
    ).fetchall()
    if len(selected) != len(shas) or any(not r["is_best"] for r in selected):
        raise ActionError("Select best-shot cards from the day page.")
    moment_ids = {r["moment_id"] for r in selected}
    if len(moment_ids) < 2:
        raise ActionError("Select best shots from at least two different moments.")
    moment_marks = ",".join("?" * len(moment_ids))
    members = conn.execute(
        f"SELECT sha256, moment_id, taken_at FROM photos WHERE moment_id IN ({moment_marks})", tuple(moment_ids)
    ).fetchall()
    target = min(members, key=lambda r: (r["taken_at"], r["sha256"]))["moment_id"]
    keep = _reviewed_days_of_moments(conn, moment_ids)
    conn.executemany(
        "INSERT OR REPLACE INTO moment_overrides (sha256, moment_id) VALUES (?, ?)",
        [(r["sha256"], target) for r in members],
    )
    conn.execute(f"UPDATE photos SET user_best = 0 WHERE moment_id IN ({moment_marks})", tuple(moment_ids))
    conn.commit()
    refresh(cfg, conn, recluster=True)
    _remark(conn, keep)
    return len(moment_ids)


def split_moment(cfg: Config, conn: sqlite3.Connection, moment_id: str, photo_shas: list[str]) -> int:
    """Move selected photos into their own persistent moment; return photos moved."""
    shas = sorted(set(photo_shas))
    if not shas:
        raise ActionError("Select at least one photo to move into a new moment.")
    members = conn.execute(
        "SELECT sha256, taken_at FROM photos WHERE moment_id = ? ORDER BY taken_at, sha256", (moment_id,)
    ).fetchall()
    if not members:
        raise ActionError("That moment no longer exists.")
    member_shas = {r["sha256"] for r in members}
    if not set(shas) <= member_shas:
        raise ActionError("Select photos from this moment only.")
    if len(shas) == len(members):
        raise ActionError("Leave at least one photo in the current moment.")

    remaining = member_shas - set(shas)
    seed = ":".join(shas)
    split_id = hashlib.sha256(f"psort-split:{moment_id}:{seed}".encode()).hexdigest()
    occupied = {r[0] for r in conn.execute("SELECT DISTINCT moment_id FROM photos")}
    counter = 1
    while split_id in occupied:
        split_id = hashlib.sha256(f"psort-split:{moment_id}:{seed}:{counter}".encode()).hexdigest()
        counter += 1
    keep = _reviewed_days_of_moments(conn, {moment_id})
    conn.executemany(
        "INSERT OR REPLACE INTO moment_overrides (sha256, moment_id) VALUES (?, ?)",
        [(sha, moment_id) for sha in remaining] + [(sha, split_id) for sha in shas],
    )
    conn.commit()
    refresh(cfg, conn, recluster=True)
    _remark(conn, keep)
    return len(shas)


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
    _manifest(cfg, conn)
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
    """Mark a day reviewed as it is now, or un-mark it if it's currently reviewed. A day flagged
    'pics added' or 'moments updated' is re-marked (not un-marked). Returns True if now reviewed."""
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError as e:
        raise ActionError(f"Not a day: {day!r}") from e
    if daystatus.status(conn, day) == daystatus.REVIEWED:
        conn.execute("DELETE FROM reviewed WHERE day = ?", (day,))
        conn.commit()
        return False
    daystatus.mark(conn, day)
    return True


def set_pin(conn: sqlite3.Connection, moment_id: str, pinned: bool) -> None:
    """Show (or stop showing) a moment's best shot beside its day on the Library page."""
    if not conn.execute("SELECT 1 FROM photos WHERE moment_id = ?", (moment_id,)).fetchone():
        raise ActionError("That moment no longer exists.")
    if pinned:
        conn.execute("INSERT OR IGNORE INTO library_pins (moment_id) VALUES (?)", (moment_id,))
    else:
        conn.execute("DELETE FROM library_pins WHERE moment_id = ?", (moment_id,))
    conn.commit()


def toggle_favorite(cfg: Config, conn: sqlite3.Connection, sha: str) -> bool:
    """Star / unstar a photo; its highlights/ copy appears or disappears to match. Starring a shot
    (without an explicit pick already pinned) also makes it its moment's best, if it wasn't."""
    try:
        now = highlights.toggle_favorite(conn, sha)
    except highlights.HighlightError as e:
        raise ActionError(str(e)) from e
    refresh(cfg, conn)
    return now


def delete_photos(cfg: Config, conn: sqlite3.Connection, shas: list[str]) -> int:
    """Move photos to library/_trash (restorable); they're never copied back from the inbox."""
    marks = ",".join("?" * len(shas)) or "''"
    moments = {r[0] for r in conn.execute(f"SELECT moment_id FROM photos WHERE sha256 IN ({marks})", shas)}
    keep = _reviewed_days_of_moments(conn, moments)
    try:
        n = trash.delete(cfg, conn, shas)
    except trash.TrashError as e:
        raise ActionError(str(e)) from e
    refresh(cfg, conn, recluster=True)  # another shot may become the best
    _remark(conn, keep)
    return n


def restore_photo(cfg: Config, conn: sqlite3.Connection, sha: str) -> None:
    try:
        trash.restore(cfg, conn, sha)
    except trash.TrashError as e:
        raise ActionError(str(e)) from e
    refresh(cfg, conn, recluster=True)


def empty_trash(cfg: Config, conn: sqlite3.Connection) -> int:
    n = trash.empty(cfg, conn)
    _manifest(cfg, conn)
    return n

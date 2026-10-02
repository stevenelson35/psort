"""Per-day review status (DESIGN.md §6): new, reviewed, pics added, moments updated.

Marking a day reviewed stores a fingerprint of the day's photos and of how they're grouped into
moments. Status is derived by comparing that fingerprint with the day as it is now, so a
`psort run` that adds photos to a day, or re-clusters it (e.g. after changing [cluster]
settings), flags just that day; days whose photos and grouping didn't change stay reviewed.
"""

import hashlib
import sqlite3
from collections import defaultdict

from .library import day_of

NEW = "new"
REVIEWED = "reviewed"
PICS_ADDED = "pics added"
MOMENTS_UPDATED = "moments updated"

Fingerprint = tuple[str, str, int]  # (photo-set digest, moment-grouping digest, photo count)


def _digest(lines: list[str]) -> str:
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _fingerprint(moments: dict[str, list[str]]) -> Fingerprint:
    shas = sorted(s for members in moments.values() for s in members)
    # Grouping by membership, not moment_id, so a moment keeping its photos but changing its id
    # doesn't count as a change.
    groups = sorted(",".join(sorted(members)) for members in moments.values())
    return _digest(shas), _digest(groups), len(shas)


def fingerprints(conn: sqlite3.Connection) -> dict[str, Fingerprint]:
    by_day: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for r in conn.execute("SELECT sha256, moment_id, library_path FROM photos WHERE library_path IS NOT NULL"):
        if day := day_of(r["library_path"]):
            by_day[day][r["moment_id"]].append(r["sha256"])
    return {day: _fingerprint(moments) for day, moments in by_day.items()}


def fingerprint(conn: sqlite3.Connection, day: str) -> Fingerprint:
    moments: dict[str, list[str]] = defaultdict(list)
    # Library paths are 'YYYY/YYYY-MM-DD[_event]/…', so characters 6–15 are the day.
    for r in conn.execute(
        "SELECT sha256, moment_id FROM photos WHERE library_path IS NOT NULL AND substr(library_path, 6, 10) = ?",
        (day,),
    ):
        moments[r["moment_id"]].append(r["sha256"])
    return _fingerprint(moments)


def _status(mark: sqlite3.Row | None, current: Fingerprint) -> str:
    if mark is None:
        return NEW
    if mark["photos_sig"] is None:  # marked before fingerprints existed and not backfilled yet
        return REVIEWED
    photos_sig, moments_sig, count = current
    if mark["photos_sig"] != photos_sig:
        return PICS_ADDED if count > (mark["photo_count"] or 0) else MOMENTS_UPDATED
    if mark["moments_sig"] != moments_sig:
        return MOMENTS_UPDATED
    return REVIEWED


def _marks(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["day"]: r for r in conn.execute("SELECT day, photos_sig, moments_sig, photo_count FROM reviewed")}


def statuses(conn: sqlite3.Connection) -> dict[str, str]:
    """Status of every day that has photos or a review mark. Days missing here are NEW."""
    current = fingerprints(conn)
    marks = _marks(conn)
    empty = _fingerprint({})
    return {day: _status(marks.get(day), current.get(day, empty)) for day in current.keys() | marks.keys()}


def status(conn: sqlite3.Connection, day: str) -> str:
    mark = conn.execute(
        "SELECT day, photos_sig, moments_sig, photo_count FROM reviewed WHERE day = ?", (day,)
    ).fetchone()
    return _status(mark, fingerprint(conn, day))


def mark(conn: sqlite3.Connection, day: str) -> None:
    """Record the day as reviewed, as it is right now."""
    photos_sig, moments_sig, count = fingerprint(conn, day)
    conn.execute(
        """INSERT INTO reviewed (day, photos_sig, moments_sig, photo_count) VALUES (?, ?, ?, ?)
           ON CONFLICT(day) DO UPDATE SET photos_sig = excluded.photos_sig, moments_sig = excluded.moments_sig,
               photo_count = excluded.photo_count, reviewed_at = datetime('now')""",
        (day, photos_sig, moments_sig, count),
    )
    conn.commit()


def backfill(conn: sqlite3.Connection) -> None:
    """Give days marked reviewed before fingerprints existed a fingerprint of how they are now,
    so later changes to them are detected."""
    days = [r["day"] for r in conn.execute("SELECT day FROM reviewed WHERE photos_sig IS NULL")]
    for day in days:
        mark(conn, day)


def keep_reviewed(conn: sqlite3.Connection, days: set[str]) -> list[str]:
    """The given days that are currently reviewed, so a review-page edit (combine, split,
    delete) can re-mark them afterwards instead of flagging your own change as an update."""
    return [d for d in days if d and status(conn, d) == REVIEWED]

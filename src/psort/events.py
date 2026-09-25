"""Events: suggested from gaps in shooting time, named by you (DESIGN.md §5.6).

A named event is a time range. Photos inside it go in `YYYY-MM-DD_<slug>` day folders, so a
multi-day trip becomes `2026-07-04_summer-trip`, `2026-07-05_summer-trip`, … and a day with
two named events splits into two folders.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .dates import NO_TIME, sql_in


class EventError(Exception):
    pass


def slugify(text: str) -> str:
    """'Birthday Party!' → 'birthday-party'. No spaces in folder names."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not slug:
        raise EventError(f"{text!r} has no letters or digits to make a name from")
    return slug


def event_id(taken_at: str) -> str:
    return datetime.fromisoformat(taken_at).strftime("%Y%m%d_%H%M%S")


@dataclass
class Event:
    id: str  # start time, YYYYMMDD_HHMMSS
    start: str
    end: str
    photos: int
    slug: str | None


def named_ranges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT slug, start, end FROM named_events ORDER BY start").fetchall()


def slug_for(taken_at: str, ranges: list[sqlite3.Row]) -> str | None:
    for r in ranges:
        if r["start"] <= taken_at <= r["end"]:
            return r["slug"]
    return None


def suggest(conn: sqlite3.Connection, gap_hours: float) -> list[Event]:
    """Split dated photos wherever shooting pauses for more than gap_hours."""
    gap = timedelta(hours=gap_hours)
    ranges = named_ranges(conn)
    times = [r["taken_at"] for r in conn.execute(
        f"SELECT taken_at FROM photos WHERE date_source NOT IN {sql_in(NO_TIME)} ORDER BY taken_at"
    )]
    groups: list[list[str]] = []
    for t in times:
        if groups and datetime.fromisoformat(t) - datetime.fromisoformat(groups[-1][-1]) <= gap:
            groups[-1].append(t)
        else:
            groups.append([t])
    return [Event(event_id(g[0]), g[0], g[-1], len(g), slug_for(g[0], ranges)) for g in groups]


def name(conn: sqlite3.Connection, gap_hours: float, first: str, text: str, through: str | None = None) -> str:
    """Name the suggested event `first` (through `through`, for multi-day events). Returns the slug."""
    slug = slugify(text)
    by_id = {e.id: e for e in suggest(conn, gap_hours)}
    for eid in filter(None, (first, through)):
        if eid not in by_id:
            raise EventError(f"No event {eid!r}. Run `psort events` to list them.")
    start, end = by_id[first].start, by_id[through or first].end
    if end < start:
        raise EventError("--through must be the same event or a later one")

    for r in named_ranges(conn):
        if r["slug"] != slug and r["start"] <= end and start <= r["end"]:
            raise EventError(f"That overlaps the event {r['slug']!r}. Unname it first.")
    # Naming again with the same slug replaces its range.
    conn.execute("INSERT OR REPLACE INTO named_events (slug, start, end) VALUES (?, ?, ?)", (slug, start, end))
    conn.commit()
    return slug


def unname(conn: sqlite3.Connection, text: str) -> None:
    slug = slugify(text)
    if not conn.execute("DELETE FROM named_events WHERE slug = ?", (slug,)).rowcount:
        raise EventError(f"No named event {slug!r}")
    conn.commit()

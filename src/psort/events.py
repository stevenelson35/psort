"""Events: suggested from gaps in shooting time, named by you (DESIGN.md §5.6).

A named event is a time range. Photos inside it go in `YYYY-MM-DD_<slug>` day folders, so a
multi-day trip becomes `2026-07-04_summer-trip`, `2026-07-05_summer-trip`, … and a day with
two named events splits into two folders.

One level of nesting: an event within another (a museum trip during a vacation) must fit inside it.
Its slug is '<parent>/<name>', so its photos land in a sub-folder of the parent's day folder:
`2026-04-12_spring-vacation-2026/museum-trip/`. Top-level events never overlap; nor do the events
within one parent.
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
    return conn.execute("SELECT slug, start, end, parent FROM named_events ORDER BY start").fetchall()


def slug_for(taken_at: str, ranges: list[sqlite3.Row]) -> str | None:
    """The most specific named event a time falls in: an event within another wins over its parent."""
    found = None
    for r in ranges:
        if r["start"] <= taken_at <= r["end"]:
            if r["parent"]:
                return r["slug"]
            found = r["slug"]
    return found


def top_slug(slug: str | None) -> str | None:
    """'spring-vacation-2026/museum-trip' → 'spring-vacation-2026' (a top-level slug is itself)."""
    return slug.split("/", 1)[0] if slug else None


def pretty(slug: str) -> str:
    """'spring-vacation-2026/museum-trip' → 'spring vacation 2026 › museum trip'."""
    return " › ".join(part.replace("-", " ") for part in slug.split("/"))


def _slug_of(text: str) -> str:
    """A slug from typed text; 'parent/child' keeps its one slash (naming a nested event by its full name)."""
    parts = text.split("/", 1)
    return "/".join(slugify(p) for p in parts)


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


def name(conn: sqlite3.Connection, gap_hours: float, first: str, text: str, through: str | None = None,
         within: str | None = None) -> str:
    """Name the suggested event `first` (through `through`, for multi-day events), optionally `within` a named
    event. Returns the slug."""
    by_id = {e.id: e for e in suggest(conn, gap_hours)}
    for eid in filter(None, (first, through)):
        if eid not in by_id:
            raise EventError(f"No event {eid!r}. Run `psort events` to list them.")
    start, end = by_id[first].start, by_id[through or first].end
    if end < start:
        raise EventError("--through must be the same event or a later one")
    return name_range(conn, text, start, end, within)


def name_range(conn: sqlite3.Connection, text: str, start: str, end: str, within: str | None = None) -> str:
    """Name the time range [start, end] (e.g. the photos you selected), at the top level or `within` a named
    top-level event it fits inside. Naming again with the same name replaces its range. Returns the slug."""
    if end < start:
        start, end = end, start
    ranges = {r["slug"]: r for r in named_ranges(conn)}
    if within:
        parent = ranges.get(_slug_of(within))
        if not parent:
            raise EventError(f"No named event {within!r} to put it in.")
        if parent["parent"]:
            raise EventError("Events go only one level deep: name it within the top-level event instead.")
        slug = f"{parent['slug']}/{slugify(text.rsplit('/', 1)[-1])}"
        if not (parent["start"] <= start and end <= parent["end"]):
            raise EventError(f"Those photos aren't all within {pretty(parent['slug'])!r} "
                             f"({parent['start'][:16].replace('T', ' ')} to {parent['end'][:16].replace('T', ' ')}).")
        for r in ranges.values():
            if r["parent"] == parent["slug"] and r["slug"] != slug and r["start"] <= end and start <= r["end"]:
                raise EventError(f"That overlaps {pretty(r['slug'])!r}, already within {pretty(parent['slug'])!r}.")
        parent_slug = parent["slug"]
    else:
        slug = slugify(text)
        for r in ranges.values():
            if r["parent"] or r["slug"] == slug or not (r["start"] <= end and start <= r["end"]):
                continue
            if r["start"] <= start and end <= r["end"]:
                raise EventError(f"That's during {pretty(r['slug'])!r}: name it within that event instead.")
            raise EventError(f"That overlaps the event {pretty(r['slug'])!r}. Unname it first.")
        for r in ranges.values():
            if r["parent"] == slug and not (start <= r["start"] and r["end"] <= end):
                raise EventError(f"{pretty(r['slug'])!r} is within it and would fall outside the new dates. "
                                 "Unname that first, or keep the range covering it.")
        parent_slug = None
    conn.execute("INSERT OR REPLACE INTO named_events (slug, start, end, parent) VALUES (?, ?, ?, ?)",
                 (slug, start, end, parent_slug))
    conn.commit()
    return slug


def unname(conn: sqlite3.Connection, text: str) -> None:
    """Remove a name; unnaming an event also removes the events within it (their photos go back to dates)."""
    slug = _slug_of(text)
    if not conn.execute("DELETE FROM named_events WHERE slug = ? OR parent = ?", (slug, slug)).rowcount:
        raise EventError(f"No named event {slug!r}")
    conn.commit()

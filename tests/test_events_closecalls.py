from pathlib import Path

import pytest

from conftest import add_close_call, save, scene
from psort.events import EventError, slugify
from test_pipeline import library_files


def test_slugify():
    assert slugify("Birthday Party!") == "birthday-party"
    assert slugify("  Summer  Trip 2026 ") == "summer-trip-2026"
    with pytest.raises(EventError):
        slugify("  !! ")


def test_suggested_events(psort):
    psort("run")
    out = psort("events").output
    # 3-hour gaps split the sample into: Jul 3 afternoon, Jul 4 morning, Jul 5.
    assert "20260703_145633     7 photos" in out
    assert "20260704_101500     1 photos" in out
    assert "20260705_090000     2 photos" in out
    assert "3 events" in out


def test_naming_events_renames_folders(psort, tmp_path):
    psort("run")
    psort("events", "name", "20260703_145633", "Birthday Party")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-03_birthday-party/20260703_145634.jpg" in files
    assert "2026/2026-07-03_birthday-party/_alternates/20260703_145634/20260703_145633.jpg" in files
    assert not any(f.startswith("2026/2026-07-03/") for f in files)
    assert not any(" " in f for f in files)

    # Multi-day event: each day keeps its own folder, all sharing the name.
    psort("events", "name", "20260704_101500", "summer-trip", "--through", "20260705_090000")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-04_summer-trip/20260704_101500.jpg" in files
    assert "2026/2026-07-05_summer-trip/20260705_120000.heic" in files

    # A range inside another named event is refused at the top level (it points to naming it within instead).
    out = psort("events", "name", "20260705_090000", "other", expect=1).output
    assert "name it within that event instead" in out

    # Unnaming puts the plain dates back and removes the empty event folders.
    psort("events", "unname", "summer-trip")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-04/20260704_101500.jpg" in files
    assert not (tmp_path / "library/2026/2026-07-04_summer-trip").exists()


def test_new_photos_join_named_event(psort, tmp_path, sample_inbox: Path):
    psort("run")
    psort("events", "name", "20260703_145633", "birthday-party")
    save(sample_inbox / "wife-phone/IMG_2000.jpg", scene(50), "2026:07:03 16:00:00", model="Galaxy S24")
    psort("run")
    assert "2026/2026-07-03_birthday-party/20260703_160000.jpg" in library_files(tmp_path / "library")


def test_close_calls(psort, tmp_path, sample_inbox: Path):
    psort("run")
    assert "No close calls." in psort("close-calls").output

    # Two nearly identical sharp shots: a near tie.
    add_close_call(sample_inbox)
    psort("run")
    out = psort("close-calls").output
    assert "1 close call(s)" in out
    assert "20260706_100000" in out and "20260706_100001" in out
    assert "20260703_1456" not in out  # the clear-cut burst isn't flagged
    assert "Close calls:       1" in psort("status").output


def test_events_within_events(psort, tmp_path):
    psort("run")
    psort("events", "name", "20260703_145633", "Spring Vacation 2026", "--through", "20260705_090000")
    # The Jul 4 suggestion as an event within the vacation: a sub-folder of that day's vacation folder.
    psort("events", "name", "20260704_101500", "Museum Trip", "--within", "spring-vacation-2026")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-04_spring-vacation-2026/museum-trip/20260704_101500.jpg" in files
    assert "2026/2026-07-03_spring-vacation-2026/20260703_145634.jpg" in files
    assert "2026/2026-07-05_spring-vacation-2026/20260705_120000.heic" in files
    listing = psort("events").output
    assert "spring vacation 2026 › museum trip" in listing

    # Only one level deep, inside its parent, and not overlapping another event within the same parent.
    assert "one level deep" in psort("events", "name", "20260704_101500", "x", "--within",
                                     "spring-vacation-2026/museum-trip", expect=1).output
    assert "overlaps" in psort("events", "name", "20260704_101500", "gallery", "--within",
                               "spring-vacation-2026", expect=1).output

    # Unnaming the event within puts its photos back in the parent's folder; unnaming the parent removes both.
    psort("events", "unname", "spring-vacation-2026/museum-trip")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-04_spring-vacation-2026/20260704_101500.jpg" in files
    psort("events", "name", "20260704_101500", "museum-trip", "--within", "spring-vacation-2026")
    psort("events", "unname", "spring-vacation-2026")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-04/20260704_101500.jpg" in files
    assert not any("museum-trip" in f or "spring-vacation" in f for f in files)


def test_event_within_must_fit_and_parent_cannot_shrink_past_it(tmp_path):
    from psort.db import connect
    from psort.events import name_range, named_ranges, slug_for

    conn = connect(tmp_path / "events.db")
    name_range(conn, "Spring Vacation", "2026-04-10T08:00:00", "2026-04-15T20:00:00")
    assert name_range(conn, "museum trip", "2026-04-12T10:00:00", "2026-04-12T14:00:00",
                      within="spring-vacation") == "spring-vacation/museum-trip"
    ranges = named_ranges(conn)
    assert slug_for("2026-04-12T11:00:00", ranges) == "spring-vacation/museum-trip"
    assert slug_for("2026-04-12T16:00:00", ranges) == "spring-vacation"
    assert slug_for("2026-04-20T16:00:00", ranges) is None
    with pytest.raises(EventError, match="aren't all within"):
        name_range(conn, "beach", "2026-04-14T10:00:00", "2026-04-17T10:00:00", within="spring-vacation")
    with pytest.raises(EventError, match="would fall outside"):
        name_range(conn, "spring vacation", "2026-04-13T08:00:00", "2026-04-15T20:00:00")
    with pytest.raises(EventError, match="overlaps"):
        name_range(conn, "other trip", "2026-04-15T10:00:00", "2026-04-18T10:00:00")

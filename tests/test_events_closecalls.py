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

    # Overlapping names are refused.
    out = psort("events", "name", "20260705_090000", "other", expect=1).output
    assert "overlaps" in out

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

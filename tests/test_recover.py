"""Recovering unreadable images by re-saving what can be read (DESIGN.md §5.13)."""

import shutil
from pathlib import Path

from PIL import Image

from conftest import save, scene
from psort import recover
from test_pipeline import db, library_files, snapshot

TRUNCATED = "IMG_20260715_120000.jpg"
GARBAGE = "IMG_20260716_120000.jpg"


def add_damaged(inbox: Path) -> None:
    folder = inbox / "damaged"
    good = save(folder / "good.jpg", scene(70), "2026:07:15 12:00:00")
    data = good.read_bytes()
    good.unlink()
    (folder / TRUNCATED).write_bytes(data[: int(len(data) * 0.6)])  # a copy that stopped part-way
    (folder / GARBAGE).write_bytes(b"this was never a picture " * 200)


def photo_count(tmp_path) -> int:
    return db(tmp_path).execute("SELECT COUNT(*) FROM photos").fetchone()[0]


def test_run_reports_unreadable_images_but_only_asks_when_interactive(psort, tmp_path, sample_inbox):
    add_damaged(sample_inbox)
    out = psort("run").output  # not a terminal: mention it, don't prompt
    assert "2 unreadable image(s) found" in out and "psort recover" in out
    assert "Try to recover them" not in out
    assert "unreadable image(s) found" not in psort("run", "--no-recover").output


def test_recover_saves_what_can_be_read_and_leaves_the_originals_alone(psort, tmp_path, sample_inbox):
    add_damaged(sample_inbox)
    psort("run")
    inbox_before, unsorted_before = snapshot(sample_inbox), snapshot(tmp_path / "unsorted_files")
    photos = photo_count(tmp_path)

    out = psort("recover", "--yes").output
    assert "Recovered 1 of 2 (1 new photo(s))" in out
    assert "could not recover" in out and "nothing readable" in out
    assert photo_count(tmp_path) == photos + 1
    assert "2026/2026-07-15/20260715_120000.jpg" in library_files(tmp_path / "library")
    with Image.open(tmp_path / "library/2026/2026-07-15/20260715_120000.jpg") as im:
        assert im.size == (800, 600)

    assert snapshot(sample_inbox) == inbox_before  # inbox untouched
    assert snapshot(tmp_path / "unsorted_files") == unsorted_before  # damaged originals still kept
    conn = db(tmp_path)
    tries = {Path(r["source_path"]).name: r["sha256"] for r in conn.execute("SELECT * FROM recoveries")}
    assert tries[TRUNCATED] and tries[GARBAGE] is None
    assert conn.execute("SELECT date_source FROM photos WHERE taken_at LIKE '2026-07-15%'").fetchone()[0] == "exif"  # EXIF carried over


def test_recovery_is_tried_once_unless_asked_again_and_survives_later_runs(psort, tmp_path, sample_inbox):
    add_damaged(sample_inbox)
    psort("run")
    psort("recover", "--yes")
    photos = photo_count(tmp_path)

    assert "No unreadable images to recover" in psort("recover", "--yes").output
    assert "Recovered 0 of 1" in psort("recover", "--yes", "--retry").output  # only the one that failed
    psort("run")
    assert photo_count(tmp_path) == photos
    assert "unreadable image(s) found" not in psort("run").output  # already dealt with: no nagging
    assert "2026/2026-07-15/20260715_120000.jpg" in library_files(tmp_path / "library")
    assert "safe to delete" in psort("verify").output


def test_recover_works_from_unsorted_files_when_the_inbox_copy_is_gone(psort, tmp_path, sample_inbox):
    add_damaged(sample_inbox)
    psort("run")
    shutil.rmtree(sample_inbox / "damaged")
    assert (tmp_path / "unsorted_files/damaged").exists()
    assert "Recovered 1 of 2" in psort("recover", "--yes").output
    assert "2026/2026-07-15/20260715_120000.jpg" in library_files(tmp_path / "library")


def test_prompt_can_be_declined_or_accepted(psort, tmp_path, sample_inbox):
    add_damaged(sample_inbox)
    psort("run")
    photos = photo_count(tmp_path)
    declined = psort("recover", input="n\n").output
    assert "Try to recover them" in declined and photo_count(tmp_path) == photos
    assert "Recovered 1 of 2" in psort("recover", input="y\n").output


def test_run_recover_flag_recovers_during_the_run(psort, tmp_path, sample_inbox):
    add_damaged(sample_inbox)
    out = psort("run", "--recover").output
    assert "Recovered 1 of 2" in out
    assert "2026/2026-07-15/20260715_120000.jpg" in library_files(tmp_path / "library")


def test_files_too_large_to_be_photos_are_not_loaded(psort, tmp_path, sample_inbox, monkeypatch):
    add_damaged(sample_inbox)
    psort("run")
    monkeypatch.setattr(recover, "MAX_BYTES", 10)
    out = psort("recover", "--yes").output
    assert "too large to be a photo" in out and "Recovered 0 of 2" in out


def test_recovered_copy_can_be_rebuilt_from_the_state_directory(psort, tmp_path, sample_inbox):
    add_damaged(sample_inbox)
    psort("run")
    psort("recover", "--yes")
    (tmp_path / "library/2026/2026-07-15/20260715_120000.jpg").unlink()  # lost from the library
    out = psort("run").output
    assert "2026/2026-07-15/20260715_120000.jpg" in library_files(tmp_path / "library")
    assert "have no library copy" not in out
    assert list((tmp_path / "state/recovered").glob("*.jpg"))

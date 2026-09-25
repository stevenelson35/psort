import json
import sqlite3
from pathlib import Path

from PIL import Image

from psort.imaging import sha256_file


def snapshot(folder: Path) -> dict[str, tuple[str, float]]:
    return {str(p.relative_to(folder)): (sha256_file(p), p.stat().st_mtime) for p in folder.rglob("*") if p.is_file()}


def library_files(library: Path) -> set[str]:
    return {str(p.relative_to(library)) for p in library.rglob("*") if p.is_file() and ".psort" not in p.parts}


def db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state" / "psort.db")
    conn.row_factory = sqlite3.Row
    return conn


def test_run_builds_library(psort, tmp_path, sample_inbox):
    before = snapshot(sample_inbox)
    out = psort("run").output

    assert "Ingest: 11 new, 1 exact duplicates" in out
    assert "3 skipped" not in out  # Thumbs.db is ignored, not skipped
    assert "2 skipped" in out  # .MOV and .txt
    assert library_files(tmp_path / "library") == {
        # Burst: sharp IMG_0002 is best; the other two are its alternates.
        "2026/2026-07-03/20260703_145634.jpg",
        "2026/2026-07-03/_alternates/20260703_145634/20260703_145633.jpg",
        "2026/2026-07-03/_alternates/20260703_145634/20260703_145636.jpg",
        "2026/2026-07-03/20260703_145640.jpg",
        "2026/2026-07-03/20260703_150640.jpg",
        # Same second: numbered in filename order, matching the blog's _1 style.
        "2026/2026-07-03/20260703_180000.jpg",
        "2026/2026-07-03/20260703_180000_1.jpg",
        "2026/2026-07-04/20260704_101500.jpg",
        "2026/2026-07-05/20260705_090000.jpg",
        "2026/2026-07-05/20260705_120000.heic",
        "_undated/20200102_030405.jpg",
    }
    # The inbox is never touched.
    assert snapshot(sample_inbox) == before
    # Library files are byte-identical copies.
    lib_best = tmp_path / "library/2026/2026-07-03/20260703_145634.jpg"
    assert sha256_file(lib_best) == sha256_file(sample_inbox / "2026-phone-dump/IMG_0002.jpg")


def test_metadata(psort, tmp_path):
    psort("run")
    rows = {r["name"]: r for r in db(tmp_path).execute("SELECT * FROM photos")}
    assert rows["20260704_101500"]["date_source"] == "filename"
    assert rows["20200102_030405"]["date_source"] == "mtime"
    assert rows["20260705_120000"]["camera"] == "Apple iPhone 15"
    rotated = rows["20260705_090000"]
    assert (rotated["width"], rotated["height"]) == (600, 800)  # upright portrait dimensions
    manifest = json.loads((tmp_path / "library/.psort/manifest.json").read_text())
    assert len(manifest["photos"]) == 11


def test_rerun_is_idempotent(psort, tmp_path):
    psort("run")
    lib_before = snapshot(tmp_path / "library")
    out = psort("run").output
    assert "Ingest: 0 new, 0 exact duplicates, 14 unchanged" in out
    assert "Curate: 0 copied, 0 moved, 11 unchanged" in out
    assert {k: v for k, v in snapshot(tmp_path / "library").items() if ".psort" not in k} == {
        k: v for k, v in lib_before.items() if ".psort" not in k
    }


def test_user_best_pick_moves_files(psort, tmp_path):
    psort("run")
    conn = db(tmp_path)
    conn.execute("UPDATE photos SET user_best = 1 WHERE name = '20260703_145633'")  # the blurry one
    conn.commit()
    out = psort("run").output
    assert "3 moved" in out
    day = "2026/2026-07-03"
    files = library_files(tmp_path / "library")
    assert f"{day}/20260703_145633.jpg" in files
    assert f"{day}/_alternates/20260703_145633/20260703_145634.jpg" in files
    assert f"{day}/_alternates/20260703_145633/20260703_145636.jpg" in files
    assert not (tmp_path / "library" / day / "_alternates/20260703_145634").exists()  # old folder cleaned up


def test_new_batch_extends_library(psort, tmp_path, sample_inbox):
    psort("run")
    from conftest import save, scene

    save(sample_inbox / "later/IMG_1000.jpg", scene(99), "2026:08:01 10:00:00")
    out = psort("run").output
    assert "Ingest: 1 new" in out
    assert "Curate: 1 copied, 0 moved, 11 unchanged" in out


def test_verify(psort, tmp_path, sample_inbox):
    out = psort("verify", "old-backup", expect=2).output
    assert "not ingested yet" in out

    psort("run")
    out = psort("verify", "old-backup").output
    assert "safe to delete" in out

    out = psort("verify", "2026-phone-dump", expect=2).output
    assert "IMG_0009.MOV" in out and "video (not supported)" in out
    assert "notes.txt" in out
    assert "2 of 13 files are NOT in the library" in out

    psort("verify", "../..", expect=1)


def test_dry_run_leaves_library_empty(psort, tmp_path):
    out = psort("run", "--dry-run").output
    assert "Would curate: 11 copied" in out
    assert library_files(tmp_path / "library") == set()


def test_status(psort):
    psort("run")
    out = psort("status").output
    assert "Photos:            11" in out
    assert "Exact duplicates:  1" in out
    assert "Undated:           1" in out
    assert "Face detection:    off" in out


def test_heic_copy_is_original_format(psort, tmp_path):
    psort("run")
    with Image.open(tmp_path / "library/2026/2026-07-05/20260705_120000.heic") as im:
        assert im.format == "HEIF"

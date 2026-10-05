"""TIFF, GIF and WebP are photos (they used to be filed under unsorted_files/ as "other")."""

import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

from conftest import scene
from psort import imaging
from psort.imaging import to_rgb
from test_pipeline import db, library_files

OLD_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".png"}


def add_odd_formats(inbox: Path) -> None:
    folder = inbox / "scans"
    folder.mkdir(parents=True, exist_ok=True)
    scene(91).save(folder / "20260710_101500.tif")
    scene(92).save(folder / "20260711_101500.tiff")
    scene(93).save(folder / "20260712_101500.gif")
    scene(94).save(folder / "20260713_101500.webp")
    gray = np.asarray(scene(95).convert("L"), dtype=np.uint16) * 257  # 16-bit scanner-style TIFF
    Image.fromarray(gray).save(folder / "20260714_101500.tif")


def test_tiff_gif_and_webp_are_filed_as_photos(psort, tmp_path, sample_inbox):
    add_odd_formats(sample_inbox)
    psort("run")

    conn = db(tmp_path)
    rows = {Path(r["path"]).name: r["status"] for r in conn.execute("SELECT path, status FROM sources WHERE path LIKE 'scans/%'")}
    assert set(rows.values()) == {"image"} and len(rows) == 5
    exts = sorted(r[0] for r in conn.execute("SELECT ext FROM photos WHERE date(taken_at) BETWEEN '2026-07-10' AND '2026-07-14'"))
    assert exts == [".gif", ".tif", ".tif", ".tif", ".webp"]  # .tiff is normalized to .tif
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-12/20260712_101500.gif" in files and "2026/2026-07-13/20260713_101500.webp" in files
    assert not any(f.startswith("scans/") for f in library_files(tmp_path / "unsorted_files"))


def test_sixteen_bit_tiff_is_scaled_not_clipped_to_white():
    gray = np.asarray(scene(95).convert("L"), dtype=np.uint16) * 257
    out = np.asarray(to_rgb(Image.fromarray(gray)))
    assert 20 < out.mean() < 235 and out.std() > 10
    assert np.asarray(Image.fromarray(gray).convert("RGB")).mean() > 250  # what plain convert() does


def test_files_already_in_unsorted_files_move_to_the_library_on_the_next_run(psort, tmp_path, sample_inbox, monkeypatch):
    add_odd_formats(sample_inbox)
    with monkeypatch.context() as m:  # how an older psort saw these files
        m.setattr("psort.ingest.IMAGE_EXTS", OLD_EXTS)
        psort("run")
    unsorted = library_files(tmp_path / "unsorted_files")
    assert {"scans/20260710_101500.tif", "scans/20260712_101500.gif", "scans/20260713_101500.webp"} <= unsorted
    assert {r["status"] for r in db(tmp_path).execute("SELECT status FROM sources WHERE path LIKE 'scans/%'")} == {"other"}

    out = psort("run").output
    assert "scans/20260710_101500.tif" not in library_files(tmp_path / "unsorted_files")
    assert not any(f.startswith("scans/") for f in library_files(tmp_path / "unsorted_files"))
    assert "2026/2026-07-12/20260712_101500.gif" in library_files(tmp_path / "library")
    conn = db(tmp_path)
    assert {r["status"] for r in conn.execute("SELECT status FROM sources WHERE path LIKE 'scans/%'")} == {"image"}
    assert conn.execute("SELECT COUNT(*) FROM other_files WHERE library_path LIKE 'scans/%'").fetchone()[0] == 0
    assert "safe to delete" in psort("verify").output
    assert "Ingest:" in out

    again = psort("run").output  # settled: nothing left to move or copy
    assert "0 new" in again


def test_new_extensions_are_registered():
    assert {".tif", ".tiff", ".gif", ".webp"} <= imaging.IMAGE_EXTS
    assert imaging.normalize_ext(".TIFF") == ".tif"

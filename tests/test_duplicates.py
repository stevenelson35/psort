"""Visual duplicates: the same picture saved twice (like two PhotoPass downloads) vs. real burst frames."""

import sqlite3
from pathlib import Path

from PIL import Image, ImageFilter

from conftest import add_close_call, save, scene
from test_pipeline import library_files


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    conn.row_factory = sqlite3.Row
    return conn


def test_resaved_copies_go_to_duplicates(psort, tmp_path, sample_inbox: Path):
    pic = scene(80)
    # The same picture downloaded twice: identical pixels, different files (metadata/compression).
    save(sample_inbox / "photopass/AK_DINOSAURRIDE_7661046153.jpeg", pic, "2016:04:17 12:18:38", model="GT4907C")
    save(sample_inbox / "photopass/AK_DINOSAURRIDE_7661047395.jpeg", pic, "2016:04:17 12:18:38", model="GT4907C",
         orientation=1)
    # And a half-size "shared" copy of another picture: the full-size one should be kept.
    big = scene(81, size=(1600, 1200))
    save(sample_inbox / "photopass/MK_1.jpeg", big, "2016:04:15 12:56:18", model="GX3300C")
    save(sample_inbox / "shared/MK_1_small.jpeg", big.resize((800, 600)), "2016:04:15 12:56:18", model="GX3300C")

    out = psort("run").output
    assert "(2 visual duplicates set aside in _duplicates)" in out
    files = library_files(tmp_path / "library")
    # Identical copies: either may be kept (larger file wins the tie); the other is filed under it.
    kept17 = [f for f in files if f.startswith("2016/2016-04-17/") and "/_" not in f]
    copies17 = [f for f in files if f.startswith("2016/2016-04-17/_duplicates/")]
    assert len(kept17) == 1 and len(copies17) == 1
    assert copies17[0].split("/")[3] == Path(kept17[0]).stem
    kept = [f for f in files if f.startswith("2016/2016-04-15/") and "/_" not in f]
    assert len(kept) == 1
    with Image.open(tmp_path / "library" / kept[0]) as im:
        assert im.size == (1600, 1200)  # the full-size copy is the keeper
    assert any("/_duplicates/" in f and f.startswith("2016/2016-04-15/") for f in files)

    # Copies aren't close calls, even though they score identically.
    assert "No close calls." in psort("close-calls").output
    status = psort("status").output
    assert "Visual duplicates: 2" in status and "Close calls:       0" in status


def test_burst_frames_are_not_duplicates(psort, tmp_path, sample_inbox: Path):
    """A blurry frame looks identical at hash level; sharpness keeps it a real alternate."""
    pic = scene(82)
    save(sample_inbox / "burst/IMG_1.jpg", pic, "2026:08:01 10:00:00")
    save(sample_inbox / "burst/IMG_2.jpg", pic.filter(ImageFilter.GaussianBlur(1.5)), "2026:08:01 10:00:00")
    add_close_call(sample_inbox)  # shifted frames: similar, not identical
    psort("run")
    conn = db(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE duplicate_of IS NOT NULL").fetchone()[0] == 0
    files = library_files(tmp_path / "library")
    assert "2026/2026-08-01/_alternates/20260801_100000/20260801_100000_1.jpg" in files
    assert "1 close call(s)" in psort("close-calls").output


def test_picking_a_copy_picks_its_keeper(psort, tmp_path, sample_inbox: Path):
    pic = scene(83)
    save(sample_inbox / "dl/a.jpeg", pic, "2016:04:16 11:37:25")
    save(sample_inbox / "dl/b.jpeg", pic, "2016:04:16 11:37:25", orientation=1)
    psort("run")
    conn = db(tmp_path)
    copy = conn.execute("SELECT sha256, duplicate_of FROM photos WHERE duplicate_of IS NOT NULL").fetchone()
    conn.execute("UPDATE photos SET user_best = 1 WHERE sha256 = ?", (copy["sha256"],))
    conn.commit()
    psort("run")
    best = conn.execute("SELECT sha256 FROM photos WHERE is_best = 1 AND taken_at LIKE '2016-04-16%'").fetchone()
    assert best["sha256"] == copy["duplicate_of"]


def test_review_ui_labels_duplicates(psort, tmp_path, sample_inbox: Path):
    from psort.config import load
    from psort.review import create_app

    pic = scene(84)
    save(sample_inbox / "dl/a.jpeg", pic, "2016:04:16 11:37:25")
    save(sample_inbox / "dl/b.jpeg", pic, "2016:04:16 11:37:25", orientation=1)
    psort("run")
    client = create_app(load(tmp_path / "psort.toml")).test_client()
    day = client.get("/folder/2016/2016-04-16").get_data(as_text=True)
    assert "+1 duplicate" in day and "shots" not in day
    moment_id = db(tmp_path).execute("SELECT moment_id FROM photos WHERE taken_at LIKE '2016-04-16%'").fetchone()[0]
    page = client.get(f"/moment/{moment_id}").get_data(as_text=True)
    assert "duplicate of 20160416_113725" in page
    assert "Make this the best" not in page

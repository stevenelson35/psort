import re
import sqlite3

import numpy as np
from PIL import Image

from conftest import EXIF_IFD, save, scene
from psort.config import load
from psort.review import create_app
from psort.winpath import windows_path

GPS_IFD = 0x8825


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    conn.row_factory = sqlite3.Row
    return conn


def add_to_tray(tmp_path, *names):
    conn = db(tmp_path)
    for n in names:
        conn.execute("INSERT INTO tray (sha256) SELECT sha256 FROM photos WHERE name = ?", (n,))
    conn.commit()


def test_export_strips_location_and_rotates(psort, tmp_path, sample_inbox):
    # A big photo with GPS, a serial number, an XMP-ish comment, and a sideways orientation.
    img = scene(70, size=(4000, 3000))
    exif = Image.Exif()
    exif[271], exif[272], exif[274] = "Google", "Pixel 8", 6
    exif.get_ifd(EXIF_IFD).update({36867: "2026:07:10 09:30:00", 36881: "-05:00", 42033: "SERIAL123"})
    exif.get_ifd(GPS_IFD).update({1: "N", 2: (38.0, 37.0, 12.0), 3: "W", 4: (90.0, 11.0, 5.0)})
    path = sample_inbox / "gps/IMG_4000.jpg"
    path.parent.mkdir()
    img.save(path, "JPEG", exif=exif.tobytes(), comment=b"home address in a comment")
    psort("run")
    add_to_tray(tmp_path, "20260710_093000", "20260705_120000")  # plus the HEIC

    out = psort("export", "Go Dogs Go").output
    assert "Exported 2 photo(s)" in out
    folder = tmp_path / "outbox/go-dogs-go"
    assert sorted(p.name for p in folder.iterdir()) == ["20260705_120000.jpg", "20260710_093000.jpg"]

    with Image.open(folder / "20260710_093000.jpg") as im:
        assert im.format == "JPEG"
        assert im.size == (1536, 2048)  # upright portrait, long edge capped
        ex = im.getexif()
        assert 274 not in ex  # orientation baked into pixels
        assert ex.get_ifd(GPS_IFD) == {}
        assert ex.get(272) == "Pixel 8"
        exif_ifd = ex.get_ifd(EXIF_IFD)
        assert exif_ifd.get(36867) == "2026:07:10 09:30:00" and exif_ifd.get(36881) == "-05:00"
        assert 42033 not in exif_ifd  # serial number gone
        assert "comment" not in im.info and "xmp" not in im.info
    assert b"home address" not in (folder / "20260710_093000.jpg").read_bytes()

    with Image.open(folder / "20260705_120000.jpg") as im:
        assert im.format == "JPEG"  # HEIC converted

    conn = db(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM tray").fetchone()[0] == 0  # exported photos leave the tray
    assert {r["post"] for r in conn.execute("SELECT post FROM exports")} == {"go-dogs-go"}


def test_small_photos_are_not_enlarged(psort, tmp_path):
    psort("run")
    psort("export", "small", "20260703_145640")
    with Image.open(tmp_path / "outbox/small/20260703_145640.jpg") as im:
        assert im.size == (800, 600)


def test_transparent_png_gets_white_background(psort, tmp_path, sample_inbox):
    rgba = np.zeros((100, 100, 4), np.uint8)  # fully transparent
    Image.fromarray(rgba, "RGBA").save(sample_inbox / "Screenshot_2026-07-11-10-00-00.png")
    psort("run")
    psort("export", "shots", "20260711_100000")
    with Image.open(tmp_path / "outbox/shots/20260711_100000.jpg") as im:
        assert im.getpixel((50, 50)) == (255, 255, 255)


def test_export_errors_and_keep_tray(psort, tmp_path):
    psort("run")
    assert "tray is empty" in psort("export", "post", expect=1).output
    assert "No photo(s) named nope" in psort("export", "post", "nope", expect=1).output
    add_to_tray(tmp_path, "20260703_145640")
    psort("export", "post", "--keep-tray")
    assert db(tmp_path).execute("SELECT COUNT(*) FROM tray").fetchone()[0] == 1


def test_export_from_review_ui(psort, tmp_path):
    psort("run")
    add_to_tray(tmp_path, "20260703_145640")
    client = create_app(load(tmp_path / "psort.toml")).test_client()
    page = client.get("/tray").get_data(as_text=True)
    assert "Export 1 photo" in page
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    client.post("/tray/export", data={"csrf": token, "post": "Beach Day"})
    page = client.get("/tray").get_data(as_text=True)
    assert "Exported 1 photo(s)" in page and "beach-day" in page
    assert (tmp_path / "outbox/beach-day/20260703_145640.jpg").exists()
    assert "posted in: beach-day" in client.get("/folder/2026/2026-07-03").get_data(as_text=True)


def test_windows_paths():
    from pathlib import Path

    assert windows_path(Path("/mnt/c/Users/steve/Pictures/psort-outbox")) == r"C:\Users\steve\Pictures\psort-outbox"
    assert windows_path(Path("/mnt/d")) == "D:\\"

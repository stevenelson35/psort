"""Nokia Lumia Rich Capture: WP_…_Rich.jpg + WP_…_Rich.nar (a ZIP of the frames)."""

import io
import sqlite3
import zipfile
from pathlib import Path

from PIL import Image, ImageEnhance

from conftest import EXIF_IFD, save, scene
from psort import ingest
from psort.config import load
from psort.dates import from_filename
from psort.review import create_app
from test_pipeline import library_files

CONTENT = """<?xml version="1.0" encoding="utf-8"?>
<content><version>1.2</version><application>BARC</application><usecase>MultiFrame_FnF</usecase>
<image properties="NoFlash">Extra1.jpg</image><image properties="Flash">Reference.jpg</image>
<image properties="FlashNoFlash">FnF.jpg</image><datafile>richsettings.xml</datafile></content>"""


def jpeg_bytes(img: Image.Image, taken: str) -> bytes:
    exif = Image.Exif()
    exif[271], exif[272] = "Microsoft", "Lumia 640 LTE"
    exif.get_ifd(EXIF_IFD)[36867] = taken
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif.tobytes(), quality=90)
    return buf.getvalue()


def make_rich(folder: Path, stem: str, seed: int, taken: str, finished: bool = True) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    base = scene(seed)
    with zipfile.ZipFile(folder / f"{stem}.nar", "w") as z:
        z.writestr("Extra1.jpg", jpeg_bytes(ImageEnhance.Brightness(base).enhance(0.55), taken))  # no flash: darker
        z.writestr("Reference.jpg", jpeg_bytes(base, taken))  # flash
        z.writestr("FnF.jpg", jpeg_bytes(ImageEnhance.Brightness(base).enhance(0.8), taken))
        z.writestr("richsettings.xml", '<?xml version="1.0"?><RichSettings Version="1"/>')
        z.writestr("content.xml", CONTENT)
    if finished:
        (folder / f"{stem}.jpg").write_bytes(jpeg_bytes(ImageEnhance.Contrast(base).enhance(1.1), taken))


def db(tmp_path):
    c = sqlite3.connect(tmp_path / "state/psort.db")
    c.row_factory = sqlite3.Row
    return c


def test_wp_filenames_have_dates():
    from datetime import datetime

    assert from_filename("WP_20151004_06_56_36_Rich.jpg") == datetime(2015, 10, 4, 6, 56, 36)


def test_frames_become_photos_and_package_sits_beside_finished_photo(psort, tmp_path, sample_inbox):
    make_rich(sample_inbox / "lumia", "WP_20151004_06_56_36_Rich", 500, "2015:10:04 06:56:37")
    out = psort("run").output
    assert "Rich Capture: 1 package(s), 3 frame(s) added as photos" in out
    c = db(tmp_path)
    finished = c.execute("""SELECT p.library_path FROM photos p JOIN sources s ON s.sha256 = p.sha256
                            WHERE s.path LIKE '%_Rich.jpg'""").fetchone()["library_path"]
    files = library_files(tmp_path / "library")
    assert finished.rsplit(".", 1)[0] + ".nar" in files  # the package beside its finished photo
    moment = c.execute("SELECT COUNT(DISTINCT moment_id) FROM photos WHERE taken_at LIKE '2015-10-04%'").fetchone()[0]
    assert moment == 1  # finished photo + 3 frames compete in one moment
    assert len([f for f in files if f.startswith("2015/2015-10-04/")]) == 5  # 4 photos + the package
    labels = {r[0] for r in c.execute("SELECT label FROM derived_frames")}
    assert labels == {"no flash", "flash", "flash + no-flash blend"}
    assert "Rich Capture package beside its photo" in psort("verify", "lumia", "--all").output
    assert "Curate: 0 copied" in psort("run").output  # idempotent: nothing re-extracted

    page = create_app(load(tmp_path / "psort.toml")).test_client().get(
        "/moment/" + c.execute("SELECT moment_id FROM photos WHERE taken_at LIKE '2015-10-04%'").fetchone()[0]
    ).get_data(as_text=True)
    assert "◈ Rich" in page and "◈ flash frame" in page and "◈ no flash frame" in page


def test_package_without_finished_photo_still_appears(psort, tmp_path, sample_inbox):
    make_rich(sample_inbox / "lumia", "WP_20160326_19_25_47_Rich", 501, "2016:03:26 19:25:47", finished=False)
    psort("run")
    files = library_files(tmp_path / "library")
    shots = [f for f in files if f.startswith("2016/2016-03-26/")]
    assert any(f.endswith(".nar") for f in shots) and len(shots) == 4  # 3 frames + package
    nar = next(f for f in shots if f.endswith(".nar"))
    ref = db(tmp_path).execute("""SELECT p.library_path FROM photos p JOIN derived_frames d ON d.sha256 = p.sha256
                                  WHERE d.member = 'Reference.jpg'""").fetchone()["library_path"]
    assert nar == ref.rsplit(".", 1)[0] + ".nar"  # beside the flash frame


def test_packages_filed_as_other_before_are_upgraded(psort, tmp_path, sample_inbox, monkeypatch):
    make_rich(sample_inbox / "lumia", "WP_20151004_06_56_36_Rich", 502, "2015:10:04 06:56:37")
    real = ingest.kind_of
    monkeypatch.setattr(ingest, "kind_of", lambda p: "other" if p.suffix.lower() == ".nar" else real(p))
    psort("run")  # what an older psort did
    assert (tmp_path / "unsorted_files/lumia/WP_20151004_06_56_36_Rich.nar").exists()
    monkeypatch.setattr(ingest, "kind_of", real)
    out = psort("run").output
    assert "Rich Capture: 1 package(s), 3 frame(s)" in out
    assert not (tmp_path / "unsorted_files/lumia").exists()  # the old unsorted copy is cleaned up
    assert any(f.endswith(".nar") for f in library_files(tmp_path / "library"))


def test_frames_are_copied_from_the_package_even_after_extraction_cache_is_gone(psort, tmp_path, sample_inbox):
    make_rich(sample_inbox / "lumia", "WP_20151004_06_56_36_Rich", 503, "2015:10:04 06:56:37")
    psort("run")
    assert not (tmp_path / "state/tmp").exists()  # temporary extractions are cleaned up
    frame = db(tmp_path).execute("""SELECT p.library_path FROM photos p JOIN derived_frames d ON d.sha256 = p.sha256
                                    WHERE d.member = 'FnF.jpg'""").fetchone()["library_path"]
    (tmp_path / "library" / frame).unlink()
    assert "Curate: 1 copied" in psort("run").output  # re-extracted from the .nar in the inbox
    assert (tmp_path / "library" / frame).exists()

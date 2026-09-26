"""Every inbox file ends up copied somewhere, and the bulk date tool."""

import re
import sqlite3
from pathlib import Path

from conftest import save, scene
from psort.config import load
from psort.review import create_app
from test_pipeline import library_files


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    conn.row_factory = sqlite3.Row
    return conn


def test_other_files_keep_their_path_without_spaces(psort, tmp_path, sample_inbox: Path):
    odd = sample_inbox / "2016/2016-01-03 - Jacksonville Bank Marathon"
    odd.mkdir(parents=True)
    (odd / "race results.pdf").write_bytes(b"%PDF results")
    (odd / ".picasa.ini").write_text("[x.jpg]\nstar=yes")
    (odd / "broken.jpg").write_bytes(b"\xff\xd8 not really a jpeg")
    out = psort("run").output
    assert "1 unreadable" in out
    unsorted = tmp_path / "unsorted_files/2016/2016-01-03-Jacksonville-Bank-Marathon"
    # Spaces become hyphens in file names too.
    assert sorted(p.name for p in unsorted.iterdir()) == [".picasa.ini", "broken.jpg", "race-results.pdf"]
    assert (unsorted / "race-results.pdf").read_bytes() == b"%PDF results"
    out = psort("verify", "2016", "--all").output
    assert "safe to delete" in out and "unreadable" in out


def test_everything_is_verified_before_the_inbox_can_go(psort, tmp_path, sample_inbox: Path):
    (sample_inbox / "late").mkdir()
    (sample_inbox / "late/new.txt").write_text("arrived after the last run")
    psort("run")
    (sample_inbox / "late/newer.txt").write_text("not ingested yet")
    out = psort("verify", expect=2).output
    assert "late/newer.txt" in out and "not ingested yet" in out
    psort("run")
    assert "The whole inbox is safe to delete" in psort("verify").output


def test_old_databases_copy_previously_skipped_files(psort, tmp_path, sample_inbox: Path):
    psort("run")
    conn = db(tmp_path)
    # Simulate an older version: notes.txt and the Live Photo clip recorded but never copied.
    conn.execute("UPDATE sources SET status = 'skipped', sha256 = NULL WHERE path LIKE '%notes.txt'")
    conn.execute("UPDATE sources SET status = 'livephoto', sha256 = NULL WHERE path LIKE '%.MOV'")
    conn.execute("DELETE FROM other_files")
    conn.execute("DELETE FROM live_clips")
    conn.commit()
    for f in (tmp_path / "unsorted_files").rglob("*"):
        if f.is_file():
            f.unlink()
    (tmp_path / "library/2026/2026-07-05/20260705_120000.mov").unlink()
    psort("run")
    assert (tmp_path / "unsorted_files/2026-phone-dump/notes.txt").exists()
    assert (tmp_path / "library/2026/2026-07-05/20260705_120000.mov").exists()


def test_live_clip_follows_its_photo(psort, tmp_path, sample_inbox: Path):
    psort("run")
    psort("events", "name", "20260705_090000", "zoo-day")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-05_zoo-day/20260705_120000.heic" in files
    assert "2026/2026-07-05_zoo-day/20260705_120000.mov" in files


def test_set_day_for_several_photos(psort, tmp_path, sample_inbox: Path):
    photopass = sample_inbox / "2016/2016-04 - PhotoPass"
    for i in range(3):
        save(photopass / f"AK_{i}.jpeg", scene(200 + i), model=None)
    psort("run")
    client = create_app(load(tmp_path / "psort.toml")).test_client()
    page = client.get("/undated").get_data(as_text=True)
    assert "Set this date for all ticked" in page
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    shas = [r["sha256"] for r in db(tmp_path).execute("SELECT sha256 FROM photos WHERE date_source = 'folder-month'")]
    assert len(shas) == 3

    client.post("/undated/set-day", data={"csrf": token, "day": "2016-04-16", "photo": shas[:2]})
    assert "Set 2 photo(s) to Sat 16 Apr 2016" in client.get("/undated").get_data(as_text=True)
    files = library_files(tmp_path / "library")
    day = sorted(f for f in files if f.startswith("2016/2016-04-16/"))
    assert day == ["2016/2016-04-16/20160416_120000.jpg", "2016/2016-04-16/20160416_120000_1.jpg"]
    assert len([f for f in files if "unknown-day" in f]) == 1  # the unticked one stays
    rows = db(tmp_path).execute("SELECT DISTINCT moment_id FROM photos WHERE date_source = 'user-day'").fetchall()
    assert len(rows) == 2  # same made-up time, but never grouped as a burst

    client.post("/undated/set-day", data={"csrf": token, "day": "2016-04-16"})
    assert "Tick at least one photo" in client.get("/undated").get_data(as_text=True)

import sqlite3
from datetime import datetime
from pathlib import Path

from conftest import save, scene
from psort.dates import from_folders
from test_pipeline import library_files


def test_from_folders():
    day = datetime(2016, 1, 3, 12)
    assert from_folders("2016/2016-01-03 - Jacksonville Bank Marathon/635877163566200462.jpg") == (day, "folder")
    assert from_folders("2016_01_03/x.jpg") == (day, "folder")
    assert from_folders("2016/2016-04 - PhotoPass - Disney Weekend/AK_1.jpeg") == (datetime(2016, 4, 1, 12), "folder-month")
    # Nearest folder wins.
    assert from_folders("2015-05-fargo/2016-01-03 trip/x.jpg") == (day, "folder")
    assert from_folders("2015-05-fargo/x.jpg") == (datetime(2015, 5, 1, 12), "folder-month")
    for undated in ["2016/x.jpg", "misc/x.jpg", "2016-13-40/x.jpg", "x.jpg", "1850-01-01/x.jpg"]:
        assert from_folders(undated) is None, undated


def test_folder_dates_in_library(psort, tmp_path, sample_inbox: Path):
    marathon = sample_inbox / "2016/2016-01-03 - Jacksonville Bank Marathon"
    save(marathon / "635877163566200462.jpg", scene(90), model=None)
    save(marathon / "635877163645576478.jpg", scene(91), model=None)
    save(sample_inbox / "2016/2016-04 - PhotoPass - Disney Weekend/AK_DINOSAURRIDE_383157749007.jpeg", scene(92), model=None)
    psort("run")
    files = library_files(tmp_path / "library")
    assert "2016/2016-01-03/20160103_120000.jpg" in files
    assert "2016/2016-01-03/20160103_120000_1.jpg" in files  # separate photos, never grouped as a burst
    assert "2016/2016-04_unknown-day/20160401_120000.jpg" in files
    # Month-only dates still need a look; day dates don't.
    assert "Undated:           2" in psort("status").output  # the month one + the sample's mystery.jpg


def test_existing_undated_photos_get_folder_dates(psort, tmp_path, sample_inbox: Path):
    folder = sample_inbox / "2016/2016-01-03 - Jacksonville Bank Marathon"
    save(folder / "635877163566200462.jpg", scene(93), model=None)
    psort("run")
    # Simulate a library built before folder dates existed.
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    conn.execute("UPDATE photos SET date_source = 'mtime', taken_at = '2020-01-01T00:00:00', name = '20200101_000000' "
                 "WHERE taken_at LIKE '2016-01-03%'")
    conn.commit()
    out = psort("run").output
    assert "Dated 1 earlier undated photo(s) from their folder names" in out
    files = library_files(tmp_path / "library")
    assert "2016/2016-01-03/20160103_120000.jpg" in files
    assert not any("20200101_000000" in f for f in files)


def test_undated_page_explains_sources(psort, tmp_path, sample_inbox: Path):
    from psort.config import load
    from psort.review import create_app

    save(sample_inbox / "2016/2016-04 - PhotoPass/AK_1.jpeg", scene(94), model=None)
    psort("run")
    client = create_app(load(tmp_path / "psort.toml")).test_client()
    page = client.get("/undated").get_data(as_text=True)
    assert "month from folder name" in page and "guessed from file time" in page
    home = client.get("/").get_data(as_text=True)
    assert "April 2016" in home and "day unknown" in home
    assert client.get("/folder/2016/2016-04_unknown-day").status_code == 200

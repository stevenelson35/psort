"""Deleting photos (trash, restore, empty) and reconciling manual changes to the library."""

import os
import re
import shutil
import sqlite3

from psort.config import load
from psort.review import create_app
from test_pipeline import library_files


def db(tmp_path):
    c = sqlite3.connect(tmp_path / "state/psort.db")
    c.row_factory = sqlite3.Row
    return c


def sha_of(tmp_path, name):
    return db(tmp_path).execute("SELECT sha256, moment_id FROM photos WHERE name = ?", (name,)).fetchone()


def ui(tmp_path):
    client = create_app(load(tmp_path / "psort.toml"), tmp_path / "psort.toml").test_client()
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/events").get_data(as_text=True)).group(1)
    return client, lambda url, **form: client.post(url, data={"csrf": token, **form})


def test_delete_restore_and_empty(psort, tmp_path):
    psort("run")
    client, post = ui(tmp_path)
    best = sha_of(tmp_path, "20260703_145634")
    post(f"/photo/{best['sha256']}/favorite")
    post(f"/photo/{best['sha256']}/tray")

    post("/photos/delete", photo=best["sha256"])
    files = library_files(tmp_path / "library")
    assert "_trash/2026/2026-07-03/20260703_145634.jpg" in files
    # The next best shot of the burst takes over the moment's place.
    assert "2026/2026-07-03/20260703_145636.jpg" in files
    assert not (tmp_path / "highlights/2026").exists()  # its highlight went too
    assert "20260703_145634" not in client.get("/folder/2026/2026-07-03").get_data(as_text=True)
    assert "20260703_145634" in client.get("/trash").get_data(as_text=True)
    assert client.get(f"/trash/thumb/{best['sha256']}.jpg").mimetype == "image/jpeg"

    # A run doesn't copy it back from the inbox; verify knows it was deleted on purpose.
    out = psort("run").output
    assert "Curate: 0 copied" in out
    assert "you deleted this photo (it's in library/_trash)" in psort("verify", "2026-phone-dump", "--all").output
    assert "In trash:          1" in psort("status").output

    post(f"/trash/{best['sha256']}/restore")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-03/20260703_145634.jpg" in files and not any(f.startswith("_trash/") for f in files)

    post("/photos/delete", photo=best["sha256"])
    post("/trash/empty")
    assert not any(f.startswith("_trash/") for f in library_files(tmp_path / "library"))
    assert "Curate: 0 copied" in psort("run").output  # still never copied back
    assert "(gone for good)" in psort("verify", "2026-phone-dump", "--all").output
    assert "The whole inbox is safe to delete" in psort("verify").output


def test_live_clip_goes_to_trash_with_its_photo(psort, tmp_path):
    psort("run")
    _, post = ui(tmp_path)
    heic = sha_of(tmp_path, "20260705_120000")["sha256"]
    post("/photos/delete", photo=heic)
    files = library_files(tmp_path / "library")
    assert "_trash/2026/2026-07-05/20260705_120000.heic" in files
    assert "_trash/2026/2026-07-05/20260705_120000.mov" in files
    assert "Live Photo clip of a photo you deleted" in psort("verify", "2026-phone-dump", "--all").output
    post(f"/trash/{heic}/restore")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-05/20260705_120000.mov" in files


def test_bulk_delete_from_day_page(psort, tmp_path):
    psort("run")
    client, post = ui(tmp_path)
    page = client.get("/folder/2026/2026-07-03").get_data(as_text=True)
    assert "Delete ticked" in page
    shas = [sha_of(tmp_path, n)["sha256"] for n in ("20260703_180000", "20260703_180000_1")]
    post("/photos/delete", photo=shas)
    assert db(tmp_path).execute("SELECT COUNT(*) FROM deleted_photos").fetchone()[0] == 2


def test_reconcile_moved_and_renamed(psort, tmp_path):
    psort("run")
    lib = tmp_path / "library"
    # Move one photo into another folder by hand, and rename a whole day folder.
    (lib / "misc").mkdir()
    shutil.move(lib / "2026/2026-07-04/20260704_101500.jpg", lib / "misc/moved.jpg")
    os.rename(lib / "2026/2026-07-05", lib / "2026/zoo")
    out = psort("reconcile").output
    assert "moved   photo: 2026/2026-07-04/20260704_101500.jpg → misc/moved.jpg" in out
    assert "2026/zoo/20260705_120000.heic" in out  # found in the renamed folder
    assert "Run `psort reconcile --apply`" in out
    assert (lib / "misc/moved.jpg").exists()  # report only: nothing changed yet

    out = psort("reconcile", "--apply").output
    assert "Library is back in order" in out
    files = library_files(lib)
    assert "2026/2026-07-04/20260704_101500.jpg" in files and "2026/2026-07-05/20260705_120000.heic" in files
    assert not (lib / "misc").exists() and not (lib / "2026/zoo").exists()
    assert "Curate: 0 copied" in psort("run").output  # adopted, never re-copied
    assert "Everything is where psort expects it" in psort("reconcile").output


def test_reconcile_deleted_by_hand_and_unknown_files(psort, tmp_path):
    psort("run")
    lib = tmp_path / "library"
    (lib / "2026/2026-07-04/20260704_101500.jpg").unlink()  # deleted in File Explorer
    (lib / "2026/my-own-notes.txt").write_text("mine")
    out = psort("reconcile").output
    assert "missing photo: 2026/2026-07-04/20260704_101500.jpg" in out
    assert "my-own-notes.txt" in out and "left alone" in out
    psort("reconcile", "--apply")
    out = psort("run").output
    assert "Curate: 0 copied" in out  # recorded as deleted, not copied back
    assert (lib / "2026/my-own-notes.txt").read_text() == "mine"
    assert "Deleted for good:  1" in psort("status").output

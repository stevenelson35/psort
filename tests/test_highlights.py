"""Favorites and the self-maintaining highlights/ folder."""

import re
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image

from psort.config import load
from psort.faces import assign, label
from psort.review import create_app


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    conn.row_factory = sqlite3.Row
    return conn


def sha_of(tmp_path, name):
    return db(tmp_path).execute("SELECT sha256, moment_id FROM photos WHERE name = ?", (name,)).fetchone()


def files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def windows_props(path: Path) -> tuple[str, str]:
    with Image.open(path) as im:
        ex = im.getexif()
        tags = ex.get(0x9C9E, b"")
        return ex.get(270, ""), tags.decode("utf-16-le").rstrip("\0") if isinstance(tags, bytes) else ""


def ui(tmp_path):
    client = create_app(load(tmp_path / "psort.toml")).test_client()
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/events").get_data(as_text=True)).group(1)
    return client, lambda url, **form: client.post(url, data={"csrf": token, **form})


def test_favorite_creates_linked_highlight_and_unfavorite_removes_it(psort, tmp_path):
    psort("run")
    client, post = ui(tmp_path)
    best = sha_of(tmp_path, "20260703_145634")["sha256"]
    post(f"/photo/{best}/favorite")
    hl = tmp_path / "highlights/2026/2026-07-03/20260703_145634.jpg"
    assert hl.exists()
    title, _ = windows_props(hl)
    assert title == "psort library: 2026/2026-07-03/20260703_145634.jpg"  # the way back to the original
    page = client.get("/favorites").get_data(as_text=True)
    assert "20260703_145634" in page and "2026-07-03" in page

    post(f"/photo/{best}/favorite")  # un-favorite
    assert not hl.exists() and not (tmp_path / "highlights/2026").exists()  # empty folders cleaned up
    assert "No favorites yet" in client.get("/favorites").get_data(as_text=True)


def test_highlights_follow_moves_and_alternates_go_in_day_folder(psort, tmp_path):
    psort("run")
    _, post = ui(tmp_path)
    alternate = sha_of(tmp_path, "20260703_145633")["sha256"]  # the blurry one, in _alternates
    post(f"/photo/{alternate}/favorite")
    assert files(tmp_path / "highlights") == {"2026/2026-07-03/20260703_145633.jpg"}

    post("/events/name", event="20260703_145633", name="Birthday Party")
    assert files(tmp_path / "highlights") == {"2026/2026-07-03_birthday-party/20260703_145633.jpg"}
    title, _ = windows_props(tmp_path / "highlights/2026/2026-07-03_birthday-party/20260703_145633.jpg")
    assert title.startswith("psort library: 2026/2026-07-03_birthday-party/_alternates/")


def test_tags_and_people_become_windows_tags(psort, tmp_path):
    psort("run")
    _, post = ui(tmp_path)
    sha = sha_of(tmp_path, "20260703_145640")["sha256"]
    post(f"/photo/{sha}/favorite")
    post(f"/photo/{sha}/tags", tags="beach, dogs")
    hl = tmp_path / "highlights/2026/2026-07-03/20260703_145640.jpg"
    assert windows_props(hl)[1] == "beach;dogs"

    # Name a (synthetic) face in it: the person's name joins the tags.
    conn = db(tmp_path)
    vec = np.ones(128, np.float32) / np.sqrt(128)
    conn.execute("INSERT INTO faces (sha256, x, y, w, h, confidence, embedding) VALUES (?, .3, .3, .3, .3, .9, ?)",
                 (sha, vec.tobytes()))
    conn.commit()
    cfg = load(tmp_path / "psort.toml")
    assign(cfg, conn)
    label(cfg, conn, "Daughter", face_ids=[1])
    assert windows_props(hl)[1] == "Daughter;beach;dogs"
    page = create_app(cfg).test_client().get("/favorites?person=Daughter").get_data(as_text=True)
    assert "20260703_145640" in page
    assert "No favorites match" in create_app(cfg).test_client().get("/favorites?year=1999").get_data(as_text=True)


def test_sync_never_touches_other_files_and_restores_missing_copies(psort, tmp_path):
    psort("run")
    _, post = ui(tmp_path)
    post(f"/photo/{sha_of(tmp_path, '20260703_145640')['sha256']}/favorite")
    mine = tmp_path / "highlights/2026/my-notes.txt"
    mine.write_text("mine")
    hl = tmp_path / "highlights/2026/2026-07-03/20260703_145640.jpg"
    hl.unlink()  # deleted by accident
    out = psort("run").output
    assert "Highlights: 1 written" in out and hl.exists() and mine.read_text() == "mine"
    assert "Highlights:" not in psort("run").output  # nothing to do the second time
    assert "Favorites:         1" in psort("status").output

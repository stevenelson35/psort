import re
import sqlite3

import numpy as np
import pytest

from psort.config import load
from psort.faces import assign
from psort.review import create_app
from test_pipeline import library_files


@pytest.fixture
def ui(psort, tmp_path):
    psort("run")
    cfg = load(tmp_path / "psort.toml")
    client = create_app(cfg).test_client()
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/events").get_data(as_text=True)).group(1)

    def post(url, **form):
        return client.post(url, data={"csrf": token, **form})

    client.post_ok = post
    client.cfg = cfg
    return client


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    conn.row_factory = sqlite3.Row
    return conn


def sha_of(tmp_path, name):
    return db(tmp_path).execute("SELECT sha256, moment_id FROM photos WHERE name = ?", (name,)).fetchone()


def text(resp):
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def test_pages_render(ui, tmp_path):
    home = text(ui.get("/"))
    assert "11 photos" in home and "Fri 3 Jul 2026" in home and "Undated" in home
    day = text(ui.get("/folder/2026/2026-07-03"))
    assert "20260703_145634" in day and "3 shots" in day
    assert "20260703_145633" not in day  # alternates only show inside their moment
    burst = sha_of(tmp_path, "20260703_145634")
    moment = text(ui.get(f"/moment/{burst['moment_id']}"))
    assert "Moment: 3 shots" in moment and "★ best" in moment and moment.count("Make this the best") == 2
    for page in ["/close-calls", "/events", "/faces", "/undated", "/tray"]:
        text(ui.get(page))
    assert ui.get("/folder/2026/nope").status_code == 404


def test_thumbnails_including_heic(ui, tmp_path):
    heic = sha_of(tmp_path, "20260705_120000")["sha256"]
    resp = ui.get(f"/thumb/{heic}/320.jpg")
    assert resp.status_code == 200 and resp.mimetype == "image/jpeg" and resp.data[:2] == b"\xff\xd8"
    assert ui.get(f"/thumb/{heic}/999.jpg").status_code == 404
    assert ui.get("/thumb/nope/320.jpg").status_code == 404


def test_requests_from_elsewhere_are_refused(ui, tmp_path):
    sha = sha_of(tmp_path, "20260703_145633")["sha256"]
    assert ui.post(f"/photo/{sha}/best", data={}).status_code == 403  # no token
    assert ui.post(f"/photo/{sha}/best", data={"csrf": "guess"}).status_code == 403
    assert ui.get("/", headers={"Host": "evil.example.com"}).status_code == 403


def test_pick_best(ui, tmp_path):
    blurry = sha_of(tmp_path, "20260703_145633")
    resp = ui.post_ok(f"/photo/{blurry['sha256']}/best", next=f"/moment/{blurry['moment_id']}")
    assert resp.status_code == 302 and resp.location.endswith(f"/moment/{blurry['moment_id']}")
    assert "2026/2026-07-03/20260703_145633.jpg" in library_files(tmp_path / "library")
    assert "You picked the best shot" in text(ui.get(f"/moment/{blurry['moment_id']}"))

    ui.post_ok(f"/moment/{blurry['moment_id']}/auto")
    assert "2026/2026-07-03/20260703_145634.jpg" in library_files(tmp_path / "library")


def test_open_redirect_is_ignored(ui, tmp_path):
    sha = sha_of(tmp_path, "20260703_145633")["sha256"]
    resp = ui.post_ok(f"/photo/{sha}/tray", next="//evil.example.com/")
    assert resp.location == "/"


def test_name_and_unname_event(ui, tmp_path):
    ui.post_ok("/events/name", event="20260704_101500", name="Summer Trip", through="20260705_090000")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-05_summer-trip/20260705_120000.heic" in files
    home = text(ui.get("/"))
    assert "summer trip" in home
    assert "summer-trip" in text(ui.get("/events"))

    resp = ui.post_ok("/events/name", event="20260705_090000", name="other")
    assert resp.status_code == 302
    assert "overlaps" in text(ui.get("/events"))  # flashed error

    ui.post_ok("/events/unname", name="summer-trip")
    assert "2026/2026-07-05/20260705_120000.heic" in library_files(tmp_path / "library")


def test_fix_undated_date(ui, tmp_path):
    undated = sha_of(tmp_path, "20200102_030405")["sha256"]
    ui.post_ok(f"/photo/{undated}/date", when="2026-07-04T08:00")
    files = library_files(tmp_path / "library")
    assert "2026/2026-07-04/20260704_080000.jpg" in files
    assert not any(f.startswith("_undated/") for f in files)
    assert "Nothing undated" in text(ui.get("/undated"))

    ui.post_ok(f"/photo/{undated}/date", when="not a date")
    assert "Enter a date" in text(ui.get("/undated"))


def test_tray_tags_reviewed(ui, tmp_path):
    sha = sha_of(tmp_path, "20260703_145640")["sha256"]
    ui.post_ok(f"/photo/{sha}/tray")
    assert "20260703_145640" in text(ui.get("/tray"))
    ui.post_ok(f"/photo/{sha}/tags", tags="Beach, dogs ,  beach")
    assert "#beach #dogs" in text(ui.get("/folder/2026/2026-07-03"))
    ui.post_ok("/day/2026-07-03/reviewed")
    assert "✓ Reviewed" in text(ui.get("/folder/2026/2026-07-03"))
    ui.post_ok(f"/photo/{sha}/tray")
    assert "Empty" in text(ui.get("/tray"))
    ui.post_ok("/day/bogus/reviewed")
    assert "Not a day" in text(ui.get("/"))


def test_name_faces(ui, tmp_path):
    """Synthetic faces on real library photos: two look-alike faces and a stranger."""
    conn = db(tmp_path)
    rng = np.random.default_rng(1)
    center = rng.normal(size=128)
    stranger = rng.normal(size=128)
    photos = [r["sha256"] for r in conn.execute("SELECT sha256 FROM photos ORDER BY name LIMIT 3")]
    for sha, vec in zip(photos, [center + rng.normal(scale=.2, size=128), center, stranger]):
        vec = (vec / np.linalg.norm(vec)).astype(np.float32)
        conn.execute("INSERT INTO faces (sha256, x, y, w, h, confidence, embedding) VALUES (?, .3, .3, .3, .3, .9, ?)",
                     (sha, vec.tobytes()))
    conn.commit()
    assign(ui.cfg, conn)
    groups = [r[0] for r in conn.execute("SELECT DISTINCT cluster FROM faces ORDER BY cluster")]
    assert len(groups) == 2

    page = text(ui.get("/faces"))
    assert "Group 1 · 2 faces" in page
    assert ui.get("/face/1.jpg").mimetype == "image/jpeg"

    # Name group 1 but untick face 2: only face 1 gets the name.
    ui.post_ok("/faces/label", name="Alice", group="1", shown=["1", "2"], face=["1"])
    rows = {r["id"]: r for r in conn.execute("SELECT id, person_id, label_source FROM faces")}
    assert rows[1]["label_source"] == "user"
    assert rows[2]["person_id"] is None  # unticked = "not Alice": not auto-matched despite looking alike
    person_id = rows[1]["person_id"]
    assert "Alice" in text(ui.get("/faces"))
    assert "Not Alice" in text(ui.get(f"/faces/person/{person_id}"))

    ui.post_ok("/faces/unlabel", face="1")
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0

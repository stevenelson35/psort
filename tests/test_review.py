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
    assert 'class="brand">Library</a>' in home
    day = text(ui.get("/folder/2026/2026-07-03"))
    assert 'data-photo-mode="fit"' in day and 'data-photo-mode="fill"' in day
    assert 'data-image-zoom-target="day-photos"' in day
    assert "20260703_145634" in day and "3 shots" in day
    assert "20260703_145633" not in day  # alternates only show inside their moment
    burst = sha_of(tmp_path, "20260703_145634")
    moment = text(ui.get(f"/moment/{burst['moment_id']}"))
    assert 'data-image-zoom-target="moment-photos"' in moment
    assert "Moment: 3 shots" in moment and "★ best" in moment and moment.count("Make this the best") == 2
    assert "20260703_145633" in moment and "20260703_145636" in moment
    assert 'data-image-zoom-target="undated-photos"' in text(ui.get("/undated"))
    ui.post_ok(f"/photo/{burst['sha256']}/favorite")
    assert 'data-image-zoom-target="favorite-photos"' in text(ui.get("/favorites"))
    for page in ["/close-calls", "/events", "/faces", "/tray"]:
        text(ui.get(page))
    assert ui.get("/folder/2026/nope").status_code == 404


def test_unexpected_errors_get_a_useful_message_instead_of_a_crash(ui, tmp_path, monkeypatch):
    import psort.actions as actions_mod

    def boom(cfg, conn, sha):
        raise RuntimeError("synthetic failure for this test")

    monkeypatch.setattr(actions_mod, "pick_best", boom)
    sha = sha_of(tmp_path, "20260703_145633")["sha256"]
    resp = ui.post_ok(f"/photo/{sha}/best", next="/")
    assert resp.status_code == 302  # not a bare 500
    assert "synthetic failure for this test" in text(ui.get("/"))


def test_combine_and_split_moments_persist(ui, psort, tmp_path):
    conn = db(tmp_path)
    picks = conn.execute(
        "SELECT sha256, moment_id FROM photos WHERE library_path LIKE '2026/2026-07-03/%' "
        "AND is_best = 1 ORDER BY taken_at LIMIT 2"
    ).fetchall()
    assert len(picks) == 2 and picks[0]["moment_id"] != picks[1]["moment_id"]
    conn.close()

    day_url = "/folder/2026/2026-07-03"
    response = ui.post_ok("/folder/2026/2026-07-03/combine", photo=[p["sha256"] for p in picks], next=day_url)
    assert response.location == day_url
    conn = db(tmp_path)
    combined = {r["moment_id"] for r in conn.execute(
        "SELECT moment_id FROM photos WHERE sha256 IN (?, ?)", (picks[0]["sha256"], picks[1]["sha256"]))}
    assert len(combined) == 1
    combined_id = next(iter(combined))
    conn.close()

    moment_url = f"/moment/{combined_id}"
    response = ui.post_ok(f"/moment/{combined_id}/split", photo=picks[1]["sha256"], next=moment_url)
    assert response.location == moment_url
    conn = db(tmp_path)
    split = {r["moment_id"] for r in conn.execute(
        "SELECT moment_id FROM photos WHERE sha256 IN (?, ?)", (picks[0]["sha256"], picks[1]["sha256"]))}
    assert len(split) == 2
    conn.close()

    psort("run")
    conn = db(tmp_path)
    persisted = {r["moment_id"] for r in conn.execute(
        "SELECT moment_id FROM photos WHERE sha256 IN (?, ?)", (picks[0]["sha256"], picks[1]["sha256"]))}
    assert len(persisted) == 2


def test_day_filters_include_visible_people_and_close_calls(ui, tmp_path):
    conn = db(tmp_path)
    photo = conn.execute(
        "SELECT sha256 FROM photos WHERE library_path LIKE '2026/2026-07-03/%' AND is_best = 1 LIMIT 1"
    ).fetchone()
    conn.execute("INSERT INTO people (name) VALUES ('Alice')")
    person_id = conn.execute("SELECT id FROM people WHERE name = 'Alice'").fetchone()["id"]
    vector = np.random.default_rng(12).normal(size=128).astype(np.float32)
    vector /= np.linalg.norm(vector)
    conn.execute(
        "INSERT INTO faces (sha256, x, y, w, h, confidence, embedding, person_id, label_source) "
        "VALUES (?, .3, .3, .3, .3, .9, ?, ?, 'user')",
        (photo["sha256"], vector.tobytes(), person_id),
    )
    conn.execute("UPDATE photos SET close_call = 1 WHERE sha256 = ?", (photo["sha256"],))
    conn.commit()

    page = text(ui.get("/folder/2026/2026-07-03"))
    assert 'data-filter-value="Alice"' in page
    assert 'data-day-filter="close"' in page
    assert 'data-day-filter="unidentified"' in page
    assert 'data-people=' in page and 'data-close-call="true"' in page


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


def test_mark_reviewed_and_return(ui, tmp_path):
    day = text(ui.get("/folder/2026/2026-07-03"))
    assert "Mark reviewed &amp; return to Library" in day

    response = ui.post_ok("/day/2026-07-03/reviewed", next="/")
    assert response.location == "/"
    assert db(tmp_path).execute("SELECT 1 FROM reviewed WHERE day = '2026-07-03'").fetchone()


def test_close_call_can_confirm_current_pick(ui, tmp_path):
    conn = db(tmp_path)
    chosen = conn.execute("SELECT sha256, moment_id FROM photos WHERE is_best = 1 LIMIT 1").fetchone()
    conn.execute("UPDATE photos SET close_call = 1 WHERE sha256 = ?", (chosen["sha256"],))
    conn.commit()

    close_calls = text(ui.get("/close-calls"))
    moment = text(ui.get(f"/moment/{chosen['moment_id']}"))
    assert "Keep this as best" in close_calls
    assert "Keep this as best" in moment

    ui.post_ok(f"/photo/{chosen['sha256']}/best")
    result = conn.execute("SELECT user_best, close_call FROM photos WHERE sha256 = ?", (chosen["sha256"],)).fetchone()
    assert (result["user_best"], result["close_call"]) == (1, 0)
    assert "No close calls." in text(ui.get("/close-calls"))


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

    # The stranger (face 3) is dropped from review for good, and stays that way after re-assigning.
    ui.post_ok("/faces/ignore", face="3")
    assert conn.execute("SELECT cluster, ignored FROM faces WHERE id = 3").fetchone()[:] == (None, 1)
    assert "Group 3" not in text(ui.get("/faces"))
    ignored_page = text(ui.get("/faces/ignored"))
    assert "Un-ignore" in ignored_page

    ui.post_ok("/faces/unignore", face="3")
    assert conn.execute("SELECT ignored FROM faces WHERE id = 3").fetchone()["ignored"] == 0
    assert "No ignored faces" in text(ui.get("/faces/ignored"))


def test_face_identity_can_be_set_and_removed_from_moment(ui, tmp_path):
    conn = db(tmp_path)
    photo = conn.execute("SELECT sha256, moment_id FROM photos ORDER BY name LIMIT 1").fetchone()
    vector = np.random.default_rng(9).normal(size=128).astype(np.float32)
    vector /= np.linalg.norm(vector)
    face_id = conn.execute(
        "INSERT INTO faces (sha256, x, y, w, h, confidence, embedding) VALUES (?, .3, .3, .3, .3, .9, ?)",
        (photo["sha256"], vector.tobytes()),
    ).lastrowid
    conn.commit()
    assign(ui.cfg, conn)

    moment_url = f"/moment/{photo['moment_id']}"
    page = text(ui.get(moment_url))
    assert "Unidentified" in page and "Ignore face" in page and "Identify face" in page

    ui.post_ok(f"/faces/{face_id}/identify", name="Alice", next=moment_url)
    row = conn.execute("SELECT person_id, label_source FROM faces WHERE id = ?", (face_id,)).fetchone()
    assert row["label_source"] == "user"
    assert conn.execute("SELECT name FROM people WHERE id = ?", (row["person_id"],)).fetchone()["name"] == "Alice"
    assert "Not Alice" in text(ui.get(moment_url))

    ui.post_ok("/faces/unlabel", face=str(face_id), next=moment_url)
    row = conn.execute("SELECT person_id, cluster FROM faces WHERE id = ?", (face_id,)).fetchone()
    assert row["person_id"] is None and row["cluster"] is not None


def test_reviewed_day_resets_when_a_new_photo_arrives(ui, psort, sample_inbox):
    ui.post_ok("/day/2026-07-03/reviewed")
    assert "✓ Reviewed" in text(ui.get("/folder/2026/2026-07-03"))

    from conftest import save, scene
    save(sample_inbox / "late/IMG_9999.jpg", scene(320), "2026:07:03 18:30:00")
    psort("run")

    assert "Mark day reviewed" in text(ui.get("/folder/2026/2026-07-03"))  # back to to-do


def test_library_progress_summary(ui, tmp_path):
    home = text(ui.get("/"))
    assert "0 / " in home and "days reviewed" in home
    ui.post_ok("/day/2026-07-03/reviewed")
    home = text(ui.get("/"))
    assert "1 / " in home and "days reviewed" in home


# ---- Submitting the forms the pages actually render (not hand-built requests) ----

FORM = re.compile(r'<form method="post" action="([^"]+)".*?</form>', re.S)


def forms(client, url):
    page = text(client.get(url))
    return [(m.group(1), dict(re.findall(r'name="(\w+)" value="([^"]*)"', m.group(0)))) for m in FORM.finditer(page)]


def submit(client, url, action_contains, **extra):
    """Submit the first form on `url` whose action contains `action_contains`, using its own fields."""
    for action, fields in forms(client, url):
        if action_contains in action:
            return client.post(action, data={**fields, **extra})
    raise AssertionError(f"no form matching {action_contains!r} on {url}")


def test_every_rendered_form_carries_the_token(ui, tmp_path, sample_inbox):
    from conftest import add_close_call, save, scene

    add_close_call(sample_inbox)
    from typer.testing import CliRunner

    from psort.cli import app

    CliRunner().invoke(app, ["--config", str(tmp_path / "psort.toml"), "run"])
    burst = sha_of(tmp_path, "20260703_145634")["moment_id"]
    sha = sha_of(tmp_path, "20260703_145640")["sha256"]
    ui.post_ok(f"/photo/{sha}/tray")

    pages = ["/", "/folder/2026/2026-07-03", f"/moment/{burst}", "/close-calls", "/events", "/faces",
             "/undated", "/tray"]
    tokens = {fields.get("csrf") for url in pages for _, fields in forms(ui, url)}
    assert len(tokens) == 1 and "" not in tokens and None not in tokens


def test_rendered_buttons_work(ui, tmp_path, sample_inbox):
    from conftest import add_close_call
    from typer.testing import CliRunner

    from psort.cli import app

    add_close_call(sample_inbox)
    CliRunner().invoke(app, ["--config", str(tmp_path / "psort.toml"), "run"])

    # Close calls: "Pick this" on the runner-up.
    runner_up = sha_of(tmp_path, "20260706_100001")
    pick = f"/photo/{runner_up['sha256']}/best"
    assert submit(ui, "/close-calls", pick).status_code == 302
    assert db(tmp_path).execute("SELECT user_best FROM photos WHERE name = '20260706_100001'").fetchone()[0] == 1
    assert "No close calls" in text(ui.get("/close-calls"))

    # Moment page: "Make this the best", then "Let psort pick again".
    burst = sha_of(tmp_path, "20260703_145634")["moment_id"]
    blurry = sha_of(tmp_path, "20260703_145633")["sha256"]
    assert submit(ui, f"/moment/{burst}", f"/photo/{blurry}/best").status_code == 302
    assert "2026/2026-07-03/20260703_145633.jpg" in library_files(tmp_path / "library")
    assert submit(ui, f"/moment/{burst}", f"/moment/{burst}/auto").status_code == 302
    assert "2026/2026-07-03/20260703_145634.jpg" in library_files(tmp_path / "library")

    # Tray add/remove, mark reviewed, tags, date, event name/unname.
    best = sha_of(tmp_path, "20260703_145634")["sha256"]
    assert submit(ui, f"/moment/{burst}", f"/photo/{best}/tray").status_code == 302
    assert "Export 1 photo" in text(ui.get("/tray"))
    assert submit(ui, "/tray", f"/photo/{best}/tray").status_code == 302  # the tray page's "Remove" button
    assert "Empty" in text(ui.get("/tray"))
    assert submit(ui, "/folder/2026/2026-07-03", "/reviewed").status_code == 302
    assert "✓ Reviewed" in text(ui.get("/folder/2026/2026-07-03"))
    assert submit(ui, f"/moment/{burst}", "/tags", tags="party").status_code == 302
    assert submit(ui, "/events", "/events/name", name="Party Time").status_code == 302
    assert "party-time" in text(ui.get("/events"))
    assert submit(ui, "/events", "/events/unname").status_code == 302
    undated = sha_of(tmp_path, "20200102_030405")["sha256"]
    assert submit(ui, "/undated", f"/photo/{undated}/date", when="2026-07-04T08:00").status_code == 302

    # Export from the tray form.
    ui.post_ok(f"/photo/{blurry}/tray")
    assert submit(ui, "/tray", "/tray/export", post="party").status_code == 302
    assert (tmp_path / "outbox/party/20260703_145633.jpg").exists()


def test_stale_page_gets_a_helpful_message(ui, tmp_path):
    sha = sha_of(tmp_path, "20260703_145633")["sha256"]
    resp = ui.post(f"/photo/{sha}/best", data={"csrf": "token-from-an-old-launch"})
    assert resp.status_code == 403 and "reload the page" in resp.get_data(as_text=True)


def test_dark_mode_is_default(ui):
    page = text(ui.get("/"))
    assert '<html lang="en" data-theme="dark">' in page
    assert 'localStorage.getItem("psort-theme") || "dark"' in page and "toggleTheme()" in page
    css = ui.get("/static/style.css").get_data(as_text=True)
    assert '[data-theme="light"]' in css


def test_clicks_only_touch_files_that_move(ui, tmp_path):
    """A pick shouldn't re-check the whole library on disk (minutes on OneDrive); `psort run` does that."""
    lib = tmp_path / "library"
    unrelated = lib / "2026/2026-07-04/20260704_101500.jpg"
    unrelated.unlink()  # something unrelated is missing; a click shouldn't go looking
    blurry = sha_of(tmp_path, "20260703_145633")
    ui.post_ok(f"/photo/{blurry['sha256']}/best")
    assert (lib / "2026/2026-07-03/20260703_145633.jpg").exists()  # the files that moved did move
    assert not unrelated.exists()


def test_manifest_is_written_after_clicks_settle(ui, tmp_path, monkeypatch):
    import json
    import time

    import psort.review as review_mod

    manifest = tmp_path / "library/.psort/manifest.json"
    before = manifest.stat().st_mtime
    sha = sha_of(tmp_path, "20260703_145640")["sha256"]
    ui.post_ok(f"/photo/{sha}/tags", tags="later")
    assert manifest.stat().st_mtime == before  # not during the click
    ui.application.flush_manifest()  # what the timer (or quitting) does
    photos = {p["name"]: p for p in json.loads(manifest.read_text())["photos"]}
    assert photos["20260703_145640"]["tags"] == ["later"]


def test_big_face_group_page_names_only_ticked_faces(ui, tmp_path):
    conn = db(tmp_path)
    rng = np.random.default_rng(3)
    center = rng.normal(size=128)
    photos = [r["sha256"] for r in conn.execute("SELECT sha256 FROM photos ORDER BY name")]
    for i in range(10):  # a group bigger than the 8 shown on the Faces page
        vec = center + rng.normal(scale=.15, size=128)
        vec = (vec / np.linalg.norm(vec)).astype(np.float32)
        conn.execute("INSERT INTO faces (sha256, x, y, w, h, confidence, embedding) VALUES (?, .3, .3, .3, .3, .9, ?)",
                     (photos[i % len(photos)], vec.tobytes()))
    conn.commit()
    assign(ui.cfg, conn)
    cluster = conn.execute("SELECT cluster FROM faces LIMIT 1").fetchone()[0]

    page = text(ui.get("/faces"))
    assert "See all 10 faces" in page
    assert f'name="group" value="{cluster}"' not in page  # can't name faces you haven't seen

    group = text(ui.get(f"/faces/group/{cluster}"))
    assert group.count('name="face"') == 10 and "Unticked faces are left undecided" in group
    ticked = re.findall(r'name="face" value="(\d+)"', group)[:3]
    ui.post_ok("/faces/label", name="Alice", face=ticked, next=f"/faces/group/{cluster}")
    rows = {r["id"]: r for r in conn.execute("SELECT id, label_source FROM faces")}
    assert all(rows[int(i)]["label_source"] == "user" for i in ticked)
    assert conn.execute("SELECT COUNT(*) FROM face_rejections").fetchone()[0] == 0  # unticked: undecided

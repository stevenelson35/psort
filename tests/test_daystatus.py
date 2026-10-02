"""Day review status (new / reviewed / pics added / moments updated), day navigation, and pinned
Library thumbnails."""

import re
import sqlite3

import pytest

from conftest import save, scene
from psort import daystatus
from psort.config import load
from psort.review import create_app

DAY = "2026-07-03"
DAY_URL = "/folder/2026/2026-07-03"


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def ui(psort, tmp_path):
    psort("run")
    client = create_app(load(tmp_path / "psort.toml")).test_client()
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/events").get_data(as_text=True)).group(1)
    client.post_ok = lambda url, **form: client.post(url, data={"csrf": token, **form})
    return client


def text(resp):
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def test_new_day_then_reviewed(ui, tmp_path):
    assert daystatus.status(db(tmp_path), DAY) == daystatus.NEW
    ui.post_ok(f"/day/{DAY}/reviewed")
    assert daystatus.status(db(tmp_path), DAY) == daystatus.REVIEWED
    ui.post_ok(f"/day/{DAY}/reviewed")  # un-mark
    assert daystatus.status(db(tmp_path), DAY) == daystatus.NEW


def test_unchanged_run_keeps_reviewed(ui, psort, tmp_path):
    ui.post_ok(f"/day/{DAY}/reviewed")
    psort("run")
    assert daystatus.status(db(tmp_path), DAY) == daystatus.REVIEWED


def test_new_photo_flags_pics_added_and_remarking_clears_it(ui, psort, tmp_path, sample_inbox):
    ui.post_ok(f"/day/{DAY}/reviewed")
    save(sample_inbox / "late/IMG_9999.jpg", scene(321), "2026:07:03 19:30:00")
    psort("run")
    assert daystatus.status(db(tmp_path), DAY) == daystatus.PICS_ADDED
    home = text(ui.get("/"))
    assert "pics added" in home
    ui.post_ok(f"/day/{DAY}/reviewed")  # re-marks rather than un-marks
    assert daystatus.status(db(tmp_path), DAY) == daystatus.REVIEWED


def test_reclustering_flags_only_days_whose_moments_changed(ui, psort, tmp_path):
    ui.post_ok(f"/day/{DAY}/reviewed")
    ui.post_ok("/day/2026-07-05/reviewed")
    # A much looser burst gap merges Jul 3's separate same-scene moments; Jul 5's two photos
    # are different scenes hours apart, so they stay as they were.
    config = tmp_path / "psort.toml"
    config.write_text(config.read_text().replace("burst_gap_seconds = 10", "burst_gap_seconds = 3600")
                      .replace("phash_threshold = 10", "phash_threshold = 64"))
    before = db(tmp_path).execute(
        "SELECT COUNT(DISTINCT moment_id) FROM photos WHERE substr(library_path, 6, 10) = ?", (DAY,)).fetchone()[0]
    psort("run")
    after = db(tmp_path).execute(
        "SELECT COUNT(DISTINCT moment_id) FROM photos WHERE substr(library_path, 6, 10) = ?", (DAY,)).fetchone()[0]
    assert after < before
    statuses = daystatus.statuses(db(tmp_path))
    assert statuses[DAY] == daystatus.MOMENTS_UPDATED
    assert statuses["2026-07-05"] == daystatus.REVIEWED


def test_your_own_combine_keeps_a_reviewed_day_reviewed(ui, tmp_path):
    ui.post_ok(f"/day/{DAY}/reviewed")
    picks = [r["sha256"] for r in db(tmp_path).execute(
        "SELECT sha256 FROM photos WHERE substr(library_path, 6, 10) = ? AND is_best = 1 ORDER BY taken_at LIMIT 2",
        (DAY,))]
    ui.post_ok(f"{DAY_URL}/combine", photo=picks, next=DAY_URL)
    assert daystatus.status(db(tmp_path), DAY) == daystatus.REVIEWED


def test_legacy_reviewed_rows_are_backfilled_before_reclustering(psort, tmp_path):
    psort("run")
    conn = db(tmp_path)
    conn.execute("INSERT INTO reviewed (day) VALUES (?)", (DAY,))  # as an older psort wrote it
    conn.commit()
    psort("status")  # any command backfills the fingerprint first
    row = db(tmp_path).execute("SELECT photos_sig, moments_sig FROM reviewed WHERE day = ?", (DAY,)).fetchone()
    assert row["photos_sig"] and row["moments_sig"]
    assert daystatus.status(db(tmp_path), DAY) == daystatus.REVIEWED


def test_day_navigation_links(ui, tmp_path):
    page = text(ui.get(DAY_URL))
    assert "Back to Library" in page
    assert "Next day" in page and 'href="/folder/2026/2026-07-04"' in page
    assert "Next unreviewed" in page and "Mark reviewed &amp; next unreviewed" in page
    middle = text(ui.get("/folder/2026/2026-07-04"))
    assert "Previous day" in middle and 'href="/folder/2026/2026-07-03"' in middle

    ui.post_ok("/day/2026-07-04/reviewed")
    page = text(ui.get(DAY_URL))
    # Jul 4 is reviewed now, so "next unreviewed" skips ahead to Jul 5.
    assert re.search(r'href="/folder/2026/2026-07-05">Next unreviewed', page)


def test_pinned_moment_thumbnail_shows_on_library(ui, tmp_path):
    best = db(tmp_path).execute(
        "SELECT sha256, moment_id FROM photos WHERE substr(library_path, 6, 10) = ? AND is_best = 1 LIMIT 1",
        (DAY,)).fetchone()
    assert "show on Library" in text(ui.get(DAY_URL))
    assert f"/thumb/{best['sha256']}/320.jpg" not in text(ui.get("/"))

    ui.post_ok(f"/moment/{best['moment_id']}/pin", pinned="1", next=DAY_URL)
    home = text(ui.get("/"))
    assert f"/thumb/{best['sha256']}/320.jpg" in home and 'class="day-thumbs"' in home
    assert "checked" in text(ui.get(DAY_URL)).split(f'id="m-{best["moment_id"]}"')[1].split("</form>")[1]

    ui.post_ok(f"/moment/{best['moment_id']}/pin", next=DAY_URL)  # unchecked: no "pinned" field
    assert f"/thumb/{best['sha256']}/320.jpg" not in text(ui.get("/"))


def test_combining_pinned_favorite_moments_keeps_one_pin_and_labels_the_alternate(ui, tmp_path):
    picks = db(tmp_path).execute(
        "SELECT sha256, moment_id FROM photos WHERE substr(library_path, 6, 10) = ? AND is_best = 1 "
        "ORDER BY taken_at LIMIT 2", (DAY,)).fetchall()
    for p in picks:
        ui.post_ok(f"/photo/{p['sha256']}/favorite")
        ui.post_ok(f"/moment/{p['moment_id']}/pin", pinned="1", next=DAY_URL)
    ui.post_ok(f"{DAY_URL}/combine", photo=[p["sha256"] for p in picks], next=DAY_URL)

    conn = db(tmp_path)
    merged = {r[0] for r in conn.execute(
        "SELECT moment_id FROM photos WHERE sha256 IN (?, ?)", (picks[0]["sha256"], picks[1]["sha256"]))}
    assert len(merged) == 1
    pins = {r[0] for r in conn.execute("SELECT moment_id FROM library_pins")}
    assert pins == merged  # carried to the merged moment; the absorbed moment's pin is gone

    page = text(ui.get(DAY_URL))
    assert page.count('data-alternate="true"') == 1  # the other favorite, shown only with the Favorites filter
    assert "favorite alternate" in page

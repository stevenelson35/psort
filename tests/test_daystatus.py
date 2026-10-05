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
    assert "← Library" in page and 'href="/#m2026-07"' in page
    assert "Next day" in page and 'href="/folder/2026/2026-07-04"' in page
    assert "Next unreviewed" in page and "✓ &amp; next unreviewed" in page
    assert 'class="breadcrumb"' in page and 'href="/#y2026"' in page
    middle = text(ui.get("/folder/2026/2026-07-04"))
    assert "Previous day" in middle and 'href="/folder/2026/2026-07-03"' in middle

    ui.post_ok("/day/2026-07-04/reviewed")
    page = text(ui.get(DAY_URL))
    # Jul 4 is reviewed now, so "next unreviewed" skips ahead to Jul 5.
    assert re.search(r'href="/folder/2026/2026-07-05"[^>]*>Next unreviewed', page)


def test_pinned_moment_thumbnail_shows_on_library(ui, tmp_path):
    best = db(tmp_path).execute(
        "SELECT sha256, moment_id FROM photos WHERE substr(library_path, 6, 10) = ? AND is_best = 1 LIMIT 1",
        (DAY,)).fetchone()
    assert "show on Library" in text(ui.get(DAY_URL))
    link = f'href="/moment/{best["moment_id"]}"'
    assert link not in text(ui.get("/"))

    ui.post_ok(f"/moment/{best['moment_id']}/pin", pinned="1", next=DAY_URL)
    home = text(ui.get("/"))
    assert link in home and 'class="day-thumbs"' in home
    assert "checked" in text(ui.get(DAY_URL)).split(f'/moment/{best["moment_id"]}/pin"')[1].split("</form>")[0]

    ui.post_ok(f"/moment/{best['moment_id']}/pin", next=DAY_URL)  # unchecked: no "pinned" field
    assert link not in text(ui.get("/"))


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


def test_starring_a_photo_pins_its_moment_and_unstarring_keeps_the_pin(ui, tmp_path):
    best = db(tmp_path).execute(
        "SELECT sha256, moment_id FROM photos WHERE substr(library_path, 6, 10) = ? AND is_best = 1 LIMIT 1",
        (DAY,)).fetchone()
    pinned = lambda: {r[0] for r in db(tmp_path).execute("SELECT moment_id FROM library_pins")}  # noqa: E731
    assert best["moment_id"] not in pinned()
    ui.post_ok(f"/photo/{best['sha256']}/favorite")
    assert best["moment_id"] in pinned()
    ui.post_ok(f"/photo/{best['sha256']}/favorite")  # un-star
    assert best["moment_id"] in pinned()


def _html(resp):
    assert resp.status_code == 200, resp.status_code
    return resp.get_data(as_text=True)


def test_library_grouped_view_has_collapsible_years_months_and_jump_bar(ui):
    home = _html(ui.get("/"))
    assert '<details class="year-group" id="y2026"' in home
    assert 'id="m2026-07"' in home and "July 2026" in home
    assert 'href="#y2026"' in home and 'href="#undated"' in home  # sticky year jump bar
    assert 'class="collage' in home  # month/year cover photos
    assert 'data-todo="yes"' in home  # unreviewed days are tagged for the "only days needing review" filter
    assert "Only days needing review" in home


def test_library_views_switch_and_are_remembered_in_a_cookie(ui):
    listing = ui.get("/?view=list")
    assert 'class="month-head"' in _html(listing) and "<details" not in _html(listing)
    assert "psort-library-view=list" in listing.headers["Set-Cookie"]
    assert 'class="month-head"' in _html(ui.get("/"))  # no ?view: the cookie decides

    calendar = _html(ui.get("/?view=calendar"))
    assert 'class="calendar"' in calendar and "cal-new" in calendar
    assert 'href="/folder/2026/2026-07-03"' in calendar
    assert 'href="/?view=grouped"' in calendar
    assert "<details" in _html(ui.get("/?view=grouped"))
    assert "<details" in _html(ui.get("/?view=bogus"))  # unknown views fall back to the default


def test_calendar_and_todo_markers_follow_review_status(ui):
    ui.post_ok(f"/day/{DAY}/reviewed")
    calendar = _html(ui.get("/?view=calendar"))
    assert re.search(r'class="cal-day cal-reviewed" href="/folder/2026/2026-07-03"', calendar)
    home = _html(ui.get("/?view=grouped"))
    row = home.split('href="/folder/2026/2026-07-03"')[0].rsplit("<tr", 1)[1]
    assert 'data-todo="no"' in row


def test_day_toolbar_has_matching_buttons_with_destination_dates(ui):
    page = _html(ui.get("/folder/2026/2026-07-05"))
    assert 'class="btn-group"' in page and 'class="btn" href="/folder/2026/2026-07-04"' in page
    assert re.search(r'href="/folder/2026/2026-07-0[34]"[^>]*>« Previous unreviewed \(', page)
    assert "✓ &amp; « previous unreviewed" in page and "✓ &amp; back to Library" in page
    assert "shots" not in page.split("</nav>")[0]
    first = _html(ui.get(DAY_URL))
    assert '<span class="btn disabled"' in first  # nothing earlier to go back to

    before = re.search(r'name="next" value="([^"]+)">\s*<button class="ok" title="Mark reviewed, then go back', page)
    assert before and before.group(1).startswith("/folder/2026/2026-07-0")


def test_toolbar_marks_reviewed_and_goes_to_previous_unreviewed(ui, tmp_path):
    page = _html(ui.get("/folder/2026/2026-07-05"))
    target = re.search(r'name="next" value="([^"]+)">\s*<button class="ok" title="Mark reviewed, then go back', page).group(1)
    response = ui.post_ok("/day/2026-07-05/reviewed", next=target)
    assert response.location == target
    assert db(tmp_path).execute("SELECT 1 FROM reviewed WHERE day = '2026-07-05'").fetchone()

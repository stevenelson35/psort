"""Review clicks redo only the moments they change (DESIGN.md §6 "Clicks stay fast"). Whatever a
scoped click leaves behind must be exactly what a full re-cluster and re-score would give."""

from psort.library import desired_paths
from psort.moments import cluster, score
from test_review import db, sha_of, ui  # noqa: F401  (ui is a fixture)

DAY = "2026/2026-07-03"


def snapshot(conn) -> list[tuple]:
    return [tuple(r) for r in conn.execute(
        "SELECT sha256, moment_id, is_best, close_call, duplicate_of, score, library_path FROM photos ORDER BY sha256")]


def assert_same_as_full_refresh(ui, tmp_path):
    conn = db(tmp_path)
    before = snapshot(conn)
    cluster(ui.cfg, conn)
    score(ui.cfg, conn)
    assert snapshot(conn) == before
    assert desired_paths(conn) == {r[0]: r[6] for r in before}  # every file is where a full curate wants it
    pins = {r[0] for r in conn.execute("SELECT moment_id FROM library_pins")}
    assert pins <= {r[1] for r in before}  # no pin left on a moment that no longer exists


def bests(tmp_path, day=DAY):
    return db(tmp_path).execute(
        "SELECT sha256, moment_id FROM photos WHERE substr(library_path, 1, ?) = ? AND is_best = 1 ORDER BY taken_at",
        (len(day) + 1, day + "/")).fetchall()


def test_scoped_clicks_match_a_full_refresh(ui, tmp_path):
    burst_best = sha_of(tmp_path, "20260703_145634")
    alternate = sha_of(tmp_path, "20260703_145636")["sha256"]

    ui.post_ok(f"/photo/{alternate}/favorite")  # a favorite becomes its moment's best
    assert_same_as_full_refresh(ui, tmp_path)
    ui.post_ok(f"/photo/{burst_best['sha256']}/best")  # an explicit pick wins again
    assert_same_as_full_refresh(ui, tmp_path)
    ui.post_ok(f"/photo/{alternate}/top")
    assert_same_as_full_refresh(ui, tmp_path)

    picks = bests(tmp_path)
    ui.post_ok(f"/moment/{picks[0]['moment_id']}/pin", pinned="1")
    ui.post_ok(f"/folder/{DAY}/combine", photo=[p["sha256"] for p in picks[:3]])
    assert_same_as_full_refresh(ui, tmp_path)
    combined = sha_of(tmp_path, "20260703_145634")["moment_id"]
    assert len({sha_of(tmp_path, n)["moment_id"] for n in ("20260703_145634", "20260703_145640")}) == 1

    ui.post_ok(f"/moment/{combined}/split", photo=[picks[1]["sha256"], picks[2]["sha256"]])
    assert_same_as_full_refresh(ui, tmp_path)

    ui.post_ok(f"/moment/{combined}/auto")
    ui.post_ok(f"/photo/{alternate}/favorite")  # un-star: both favorite and top pick go
    assert_same_as_full_refresh(ui, tmp_path)


def test_scoped_favorite_keeps_highlights_in_sync(ui, tmp_path):
    sha = sha_of(tmp_path, "20260703_145640")["sha256"]
    ui.post_ok(f"/photo/{sha}/top")
    assert (ui.cfg.highlights / DAY.split("/")[0] / "2026-07-03" / "20260703_145640.jpg").exists()
    assert (ui.cfg.top_picks / "20260703_145640.jpg").exists()
    ui.post_ok(f"/photo/{sha}/favorite")
    assert not list(ui.cfg.highlights.rglob("20260703_145640.jpg"))
    assert not (ui.cfg.top_picks / "20260703_145640.jpg").exists()


def fetch(ui, url, **form):
    """A click sent by the day page's script."""
    return ui.post(url, data={"csrf": csrf(ui), **form}, headers={"X-Requested-With": "fetch"})


def csrf(ui):
    import re

    return re.search(r'name="csrf" value="([^"]+)"', ui.get("/events").get_data(as_text=True)).group(1)


def test_fetch_clicks_answer_with_json_and_cards_can_be_reloaded(ui, tmp_path):
    photo = sha_of(tmp_path, "20260703_145640")
    resp = fetch(ui, f"/photo/{photo['sha256']}/favorite", next=f"/folder/{DAY}")
    assert resp.status_code == 200 and resp.get_json() == {"ok": True}

    cards = ui.get(f"/folder/{DAY}/cards", query_string={"m": photo["moment_id"]}).get_json()
    assert f'data-sha="{photo["sha256"]}"' in cards["html"] and cards["html"].count('class="day-photo"') == 1
    assert 'class="star on"' in cards["html"]  # now starred
    assert f'value="/folder/{DAY}#m-{photo["moment_id"]}"' in cards["html"]  # no-JS posts return to the card
    assert (cards["moments"], cards["photos"]) == (5, 7)

    resp = fetch(ui, "/photo/nope/favorite")
    assert resp.status_code == 400 and resp.get_json()["ok"] is False and "No photo" in resp.get_json()["error"]


def test_fetch_combine_and_reload_drops_the_combined_away_moment(ui, tmp_path):
    picks = bests(tmp_path)
    ids = [p["moment_id"] for p in picks[:2]]
    resp = fetch(ui, f"/folder/{DAY}/combine", photo=[p["sha256"] for p in picks[:2]])
    assert resp.get_json() == {"ok": True}
    cards = ui.get(f"/folder/{DAY}/cards", query_string=[("m", i) for i in ids]).get_json()
    assert cards["html"].count('class="day-photo"') == 1 and cards["moments"] == 4

    resp = fetch(ui, f"/folder/{DAY}/combine", photo=[picks[2]["sha256"]])
    assert resp.status_code == 400 and "at least two" in resp.get_json()["error"]


def test_day_page_uses_in_place_updates(ui, tmp_path):
    page = ui.get(f"/folder/{DAY}").get_data(as_text=True)
    assert f'"/folder/{DAY}/cards"' in page and 'id="day-toast"' in page and 'id="day-moment-count"' in page
    assert page.count('class="day-photo"') == 5 and "this.form.requestSubmit()" in page

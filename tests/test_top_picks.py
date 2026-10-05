"""Top picks (◆): a ranked-higher favorite, kept flat in top_picks/, browsable from the Library."""

import json

from psort import browse
from psort.config import load
from test_highlights import db, files, sha_of, ui, windows_props

BEST = "20260703_145634"
OTHER = "20260703_150640"


def pick(tmp_path, name, what):
    client, post = ui(tmp_path)
    sha = sha_of(tmp_path, name)["sha256"]
    post(f"/photo/{sha}/{what}")
    return client, post, sha


def test_top_pick_is_also_a_favorite_and_gets_a_flat_copy(psort, tmp_path):
    psort("run")
    _, _, sha = pick(tmp_path, BEST, "top")
    row = db(tmp_path).execute("SELECT top FROM favorites WHERE sha256 = ?", (sha,)).fetchone()
    assert row and row["top"] == 1  # one click: favorite and top pick
    assert (tmp_path / "highlights/2026/2026-07-03/20260703_145634.jpg").exists()  # nested highlights copy
    assert files(tmp_path / "top_picks") == {"20260703_145634.jpg"}  # flat: no folders
    title, _ = windows_props(tmp_path / "top_picks/20260703_145634.jpg")
    assert title == "psort library: 2026/2026-07-03/20260703_145634.jpg"
    assert db(tmp_path).execute("SELECT 1 FROM library_pins").fetchone()  # its moment shows on the Library


def test_a_plain_favorite_is_not_a_top_pick(psort, tmp_path):
    psort("run")
    pick(tmp_path, BEST, "favorite")
    assert (tmp_path / "highlights/2026/2026-07-03/20260703_145634.jpg").exists()
    assert not files(tmp_path / "top_picks")


def test_unmarking_keeps_the_favorite_and_unstarring_removes_both(psort, tmp_path):
    psort("run")
    client, post, sha = pick(tmp_path, BEST, "top")
    post(f"/photo/{sha}/top")  # un-mark
    assert not files(tmp_path / "top_picks")
    assert (tmp_path / "highlights/2026/2026-07-03/20260703_145634.jpg").exists()
    assert db(tmp_path).execute("SELECT top FROM favorites WHERE sha256 = ?", (sha,)).fetchone()["top"] == 0

    post(f"/photo/{sha}/top")  # back to a top pick
    post(f"/photo/{sha}/favorite")  # un-star
    assert not files(tmp_path / "top_picks") and not (tmp_path / "highlights/2026").exists()
    assert not db(tmp_path).execute("SELECT 1 FROM favorites WHERE sha256 = ?", (sha,)).fetchone()
    assert not db(tmp_path).execute("SELECT 1 FROM top_picks").fetchone()


def test_buttons_show_state_on_day_and_moment_pages(psort, tmp_path):
    psort("run")
    client, post, sha = pick(tmp_path, BEST, "top")
    day = client.get("/folder/2026/2026-07-03").get_data(as_text=True)
    assert f'/photo/{sha}/top' in day and "◆" in day and "◇" in day  # marked and unmarked cards
    moment = client.get(f"/moment/{sha_of(tmp_path, BEST)['moment_id']}").get_data(as_text=True)
    assert "◆ Top pick" in moment and "◇ Make a top pick" not in moment.split(sha)[0] + ""


def test_favorites_page_can_show_only_top_picks(psort, tmp_path):
    psort("run")
    client, post, _ = pick(tmp_path, BEST, "top")
    post(f"/photo/{sha_of(tmp_path, OTHER)['sha256']}/favorite")
    both = client.get("/favorites").get_data(as_text=True)
    only = client.get("/favorites?top=1").get_data(as_text=True)
    assert BEST in both and OTHER in both
    assert BEST in only and OTHER not in only and "checked" in only


def test_library_views_for_favorites_and_top_picks_link_to_moments(psort, tmp_path):
    psort("run")
    client, post, _ = pick(tmp_path, BEST, "top")
    post(f"/photo/{sha_of(tmp_path, OTHER)['sha256']}/favorite")
    best_link = f'href="/moment/{sha_of(tmp_path, BEST)["moment_id"]}"'
    other_link = f'href="/moment/{sha_of(tmp_path, OTHER)["moment_id"]}"'

    favorites = client.get("/?view=favorites")
    page = favorites.get_data(as_text=True)
    assert best_link in page and other_link in page
    assert "★ Favorites (2)" in page and "◆ Top picks (1)" in page
    assert 'id="y2026"' in page and 'href="#y2026"' in page and 'class="gallery"' in page
    assert "psort-library-view=favorites" in favorites.headers["Set-Cookie"]
    assert "Fri 3 Jul 2026" in page and "◆ Fri 3 Jul 2026" in page  # top picks are marked in the favorites view

    top = client.get("/?view=top").get_data(as_text=True)
    assert best_link in top and other_link not in top
    assert 'id="todo-only"' not in top  # review-progress controls don't apply here
    assert "Only days needing review" in client.get("/?view=grouped").get_data(as_text=True)


def test_library_gallery_views_say_so_when_empty(psort, tmp_path):
    psort("run")
    client, _ = ui(tmp_path)
    assert "No top picks yet" in client.get("/?view=top").get_data(as_text=True)
    assert "No favorites yet" in client.get("/?view=favorites").get_data(as_text=True)


def test_top_picks_command_syncs_and_exports_flat(psort, tmp_path):
    psort("run")
    pick(tmp_path, BEST, "top")
    pick(tmp_path, OTHER, "top")
    out = tmp_path / "share"
    result = psort("top-picks", "--out", str(out)).output
    assert "2 top pick(s)" in result and "Copied 2 web-size file(s)" in result
    assert files(out) == {"20260703_145634.jpg", "20260703_150640.jpg"}
    assert "already there" in psort("top-picks", "--out", str(out)).output  # nothing overwritten

    originals = tmp_path / "originals"
    assert "Copied 2 original file(s)" in psort("top-picks", "--out", str(originals), "--originals").output
    assert (originals / "20260703_145634.jpg").read_bytes() == (tmp_path / "library/2026/2026-07-03/20260703_145634.jpg").read_bytes()


def test_top_picks_command_without_any_says_so(psort, tmp_path):
    psort("run")
    out = psort("top-picks", "--out", str(tmp_path / "share")).output
    assert "0 top pick(s)" in out and "No top picks yet" in out and not (tmp_path / "share").exists()


def test_run_keeps_top_picks_in_sync_and_rebuilds_a_deleted_copy(psort, tmp_path):
    psort("run")
    pick(tmp_path, BEST, "top")
    (tmp_path / "top_picks/20260703_145634.jpg").unlink()
    out = psort("run").output
    assert (tmp_path / "top_picks/20260703_145634.jpg").exists() and "Top picks: 1 written" in out


def test_top_picks_folder_is_configurable_and_defaults_beside_the_library(psort, tmp_path):
    cfg = load(tmp_path / "psort.toml")
    assert cfg.top_picks == tmp_path / "top_picks"
    config = tmp_path / "psort.toml"
    config.write_text(config.read_text().replace(f'top_picks = "{tmp_path}/top_picks"', f'top_picks = "{tmp_path}/best"'))
    assert load(config).top_picks == tmp_path / "best"


def test_manifest_flags_only_top_picks(psort, tmp_path):
    psort("run")
    pick(tmp_path, BEST, "top")
    pick(tmp_path, OTHER, "favorite")
    manifest = browse.build_manifest(db(tmp_path))
    by_id = {p["id"]: p for p in manifest["photos"]}
    assert by_id[BEST]["top"] is True and "top" not in by_id[OTHER]
    assert json.loads(json.dumps(manifest))["photos"]

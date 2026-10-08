"""Private photos (🔒): kept in the library but never published, and easy to find for review."""

from test_review import db, sha_of, text, ui  # noqa: F401  (ui is a fixture)

BEST = "20260703_145634"
ALTERNATE = "20260703_145636"  # another shot in BEST's moment


def test_toggle_private_from_a_day_card(ui, tmp_path):
    sha = sha_of(tmp_path, BEST)["sha256"]
    day = text(ui.get("/folder/2026/2026-07-03"))
    assert f'action="/photo/{sha}/private"' in day and "🔒 private</span>" not in day
    ui.post_ok(f"/photo/{sha}/private", next="/folder/2026/2026-07-03")
    assert db(tmp_path).execute("SELECT private FROM photos WHERE sha256 = ?", (sha,)).fetchone()[0] == 1
    day = text(ui.get("/folder/2026/2026-07-03"))
    assert "🔒 private</span>" in day and 'data-private="true"' in day
    ui.post_ok(f"/photo/{sha}/private")
    assert db(tmp_path).execute("SELECT private FROM photos WHERE sha256 = ?", (sha,)).fetchone()[0] == 0


def test_private_alternates_show_on_the_day_page_for_the_private_filter(ui, tmp_path):
    ui.post_ok(f"/photo/{sha_of(tmp_path, BEST)['sha256']}/best")  # keep the best where it is
    alternate = sha_of(tmp_path, ALTERNATE)
    assert ALTERNATE not in text(ui.get("/folder/2026/2026-07-03"))
    ui.post_ok(f"/photo/{alternate['sha256']}/private")
    day = text(ui.get("/folder/2026/2026-07-03"))
    assert 'data-day-filter="private"' in day
    assert ALTERNATE in day and "🔒 private alternate" in day
    moment = text(ui.get(f"/moment/{alternate['moment_id']}"))
    assert "🔒 Private" in moment and "🔒 Mark private" in moment  # one private shot, the others not


def test_library_private_view_lists_every_private_photo(ui, tmp_path):
    assert "No private photos" in text(ui.get("/?view=private"))
    ui.post_ok(f"/photo/{sha_of(tmp_path, BEST)['sha256']}/best")
    alternate = sha_of(tmp_path, ALTERNATE)
    ui.post_ok(f"/photo/{alternate['sha256']}/private")
    page = text(ui.get("/?view=private"))
    assert "🔒 Private (1)" in page and f'href="/moment/{alternate["moment_id"]}"' in page
    assert 'id="todo-only"' not in page
    ui.post_ok(f"/photo/{alternate['sha256']}/favorite")
    assert "🔒 Fri 3 Jul 2026" in text(ui.get("/?view=favorites"))  # private favorites are marked


def test_library_page_keeps_the_nav_counts(ui, tmp_path):
    ui.post_ok(f"/photo/{sha_of(tmp_path, BEST)['sha256']}/tray")
    assert 'Post tray <span class="pill ok">1</span>' in text(ui.get("/"))


def test_private_survives_trash_and_restore_and_is_in_the_manifest(ui, tmp_path):
    import json

    sha = sha_of(tmp_path, BEST)["sha256"]
    ui.post_ok(f"/photo/{sha}/private")
    ui.post_ok("/photos/delete", photo=sha)
    ui.post_ok(f"/trash/{sha}/restore")
    assert db(tmp_path).execute("SELECT private FROM photos WHERE sha256 = ?", (sha,)).fetchone()[0] == 1
    ui.application.flush_manifest()
    manifest = json.loads((ui.cfg.library / ".psort/manifest.json").read_text())
    assert next(p for p in manifest["photos"] if p["sha256"] == sha)["private"] == 1

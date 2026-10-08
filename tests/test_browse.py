"""Publishing favorites for the browse page: the manifest shape and the upload."""

import json
import sqlite3
import threading

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from psort import blog, browse
from psort.config import load

@pytest.fixture
def ftp_site(tmp_path):
    root = tmp_path / "turbify"
    (root / "pics/browse").mkdir(parents=True)
    auth = DummyAuthorizer()
    auth.add_user("sjnelson@itsallonesong.com", "secret", str(root), perm="elradfmw")
    handler = type("H", (FTPHandler,), {"authorizer": auth})
    server = FTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.2}, daemon=True)
    thread.start()
    yield root, server.address[1]
    server.close_all()


def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state/psort.db")
    c.row_factory = sqlite3.Row
    return c


def favorite(tmp_path, name):
    c = conn(tmp_path)
    c.execute("INSERT INTO favorites (sha256) SELECT sha256 FROM photos WHERE name = ?", (name,))
    c.commit()


def test_no_favorites_refuses(psort, tmp_path):
    psort("run")
    cfg = load(tmp_path / "psort.toml")
    with pytest.raises(browse.BrowseError):
        browse.publish(cfg, conn(tmp_path), blog.BlogSettings(repo=tmp_path, ftp_host="h", ftp_user="u"), None,
                       dry_run=True)


def test_manifest_has_people_and_events_but_no_embeddings(psort, tmp_path):
    psort("run")
    c = conn(tmp_path)
    favorite(tmp_path, "20260703_145634")
    favorite(tmp_path, "20260705_090000")
    c.execute("INSERT INTO people (name) VALUES ('Steve')")
    person = c.execute("SELECT id FROM people WHERE name = 'Steve'").fetchone()["id"]
    face = c.execute("SELECT id, sha256 FROM faces LIMIT 1").fetchone()
    if face:
        c.execute("UPDATE faces SET person_id = ? WHERE id = ?", (person, face["id"]))
    c.execute("INSERT INTO named_events (slug, start, end) VALUES ('day-out', '2026-07-03T00:00:00', "
              "'2026-07-03T23:59:59')")
    c.commit()

    manifest = browse.build_manifest(c)
    assert manifest["version"] == 1
    ids = {p["id"] for p in manifest["photos"]}
    assert ids == {"20260703_145634", "20260705_090000"}
    by_id = {p["id"]: p for p in manifest["photos"]}
    assert by_id["20260703_145634"]["event"] == "day-out"
    assert by_id["20260705_090000"]["event"] is None
    assert "embedding" not in json.dumps(manifest)
    if face:
        assert by_id[c.execute("SELECT name FROM photos WHERE sha256 = ?", (face["sha256"],)).fetchone()["name"]
                     ]["people"] == ["Steve"]


def test_publish_uploads_images_and_replaces_the_manifest(psort, tmp_path, ftp_site):
    psort("run")
    favorite(tmp_path, "20260703_145634")
    root, port = ftp_site

    def run_publish():
        cfg = load(tmp_path / "psort.toml")
        s = blog.BlogSettings(repo=tmp_path, ftp_host="127.0.0.1", ftp_user="sjnelson@itsallonesong.com",
                              remote_dir="pics/browse", ftp_tls=False, ftp_port=port)
        return browse.publish(cfg, conn(tmp_path), s, "secret")

    run_publish()
    manifest_path = root / "pics/browse/browse-manifest.json"
    assert manifest_path.exists()
    cors = (root / "pics/browse/.htaccess").read_text()
    assert 'Access-Control-Allow-Origin "https://blog.itsallonesong.com"' in cors
    assert not (root / "pics/browse/20260703_145634.jpg").exists()  # the page only uses the sizes
    assert (root / "pics/browse/640/20260703_145634-640.jpg").exists()
    assert (root / "pics/browse/2048/20260703_145634-2048.jpg").exists()
    assert (root / "pics/browse/index.html").exists()  # no folder listing
    assert not list(root.glob("pics/browse/*.uploading"))

    favorite(tmp_path, "20260705_090000")  # a second favorite changes the manifest
    run_publish()
    manifest = json.loads(manifest_path.read_text())
    assert {p["id"] for p in manifest["photos"]} == {"20260703_145634", "20260705_090000"}


def test_private_favorites_stay_off_the_browse_page_and_leave_the_server(psort, tmp_path, ftp_site):
    """Marking an already-published favorite private (or un-starring one) removes its copies on the next publish."""
    psort("run")
    favorite(tmp_path, "20260703_145634")
    favorite(tmp_path, "20260705_090000")
    root, port = ftp_site
    cfg = load(tmp_path / "psort.toml")
    s = blog.BlogSettings(repo=tmp_path, ftp_host="127.0.0.1", ftp_user="sjnelson@itsallonesong.com",
                          remote_dir="pics/browse", ftp_tls=False, ftp_port=port)
    browse.publish(cfg, conn(tmp_path), s, "secret")
    assert (root / "pics/browse/640/20260705_090000-640.jpg").exists()
    (root / "pics/browse/notes.txt").write_text("not psort's")

    c = conn(tmp_path)
    c.execute("UPDATE photos SET private = 1 WHERE name = '20260705_090000'")
    c.commit()
    assert {p["id"] for p in browse.build_manifest(c)["photos"]} == {"20260703_145634"}
    log = []
    browse.publish(cfg, conn(tmp_path), s, "secret", log=log.append)
    assert not list(root.glob("pics/browse/**/20260705_090000*"))
    assert (root / "pics/browse/640/20260703_145634-640.jpg").exists()
    assert (root / "pics/browse/notes.txt").exists()  # only psort's photo files are removed
    assert "removed 1," in log[-1]
    manifest = json.loads((root / "pics/browse/browse-manifest.json").read_text())
    assert {p["id"] for p in manifest["photos"]} == {"20260703_145634"}

    c.execute("DELETE FROM favorites WHERE sha256 = (SELECT sha256 FROM photos WHERE name = '20260703_145634')")
    c.execute("UPDATE photos SET private = 0 WHERE name = '20260705_090000'")
    c.commit()
    browse.publish(cfg, conn(tmp_path), s, "secret")  # un-starred: removed; un-privated favorite: back
    assert not list(root.glob("pics/browse/**/20260703_145634*"))
    assert (root / "pics/browse/640/20260705_090000-640.jpg").exists()


def test_only_private_favorites_means_nothing_to_publish(psort, tmp_path):
    psort("run")
    favorite(tmp_path, "20260703_145634")
    c = conn(tmp_path)
    c.execute("UPDATE photos SET private = 1")
    c.commit()
    with pytest.raises(browse.BrowseError, match="private photos are left out"):
        browse.publish(load(tmp_path / "psort.toml"), c, blog.BlogSettings(repo=tmp_path, ftp_host="h", ftp_user="u"),
                       None, dry_run=True)


def settings_for(tmp_path, port):
    return blog.BlogSettings(repo=tmp_path, ftp_host="127.0.0.1", ftp_user="sjnelson@itsallonesong.com",
                             remote_dir="pics/browse", ftp_tls=False, ftp_port=port)


def publish(tmp_path, port, **kw):
    log = []
    browse.publish(load(tmp_path / "psort.toml"), conn(tmp_path), settings_for(tmp_path, port), "secret",
                   log=log.append, **kw)
    return log


def test_second_publish_with_nothing_new_does_not_even_connect(psort, tmp_path, ftp_site, monkeypatch):
    psort("run")
    favorite(tmp_path, "20260703_145634")
    root, port = ftp_site
    publish(tmp_path, port)

    def no_connection(*_a, **_k):
        raise AssertionError("connected to the server with nothing to do")

    monkeypatch.setattr(browse, "Uploader", no_connection)
    assert "Already up to date (1 photo(s))" in publish(tmp_path, port)[-1]


def test_only_new_photos_are_rendered_and_uploaded(psort, tmp_path, ftp_site, monkeypatch):
    psort("run")
    favorite(tmp_path, "20260703_145634")
    root, port = ftp_site
    publish(tmp_path, port)
    first = root / "pics/browse/640/20260703_145634-640.jpg"
    first.write_bytes(b"marker: not re-uploaded")

    rendered = []
    real = browse.stage_images
    monkeypatch.setattr(browse, "stage_images", lambda cfg, rows, out, **kw: rendered.extend(r["name"] for r in rows)
                        or real(cfg, rows, out, **kw))
    favorite(tmp_path, "20260705_090000")
    log = publish(tmp_path, port)
    assert rendered == ["20260705_090000"]
    assert "Uploaded 1 new photo(s)" in log[-1] and "1 unchanged" in log[-1] and "browse-manifest.json" in log[-1]
    assert first.read_bytes() == b"marker: not re-uploaded"
    manifest = json.loads((root / "pics/browse/browse-manifest.json").read_text())
    assert {p["id"] for p in manifest["photos"]} == {"20260703_145634", "20260705_090000"}


def test_first_incremental_publish_adopts_what_is_already_there(psort, tmp_path, ftp_site, monkeypatch):
    """A server published by the old psort (every size plus an unused full-size copy, no records)."""
    psort("run")
    favorite(tmp_path, "20260703_145634")
    root, port = ftp_site
    publish(tmp_path, port)
    c = conn(tmp_path)
    c.execute("DELETE FROM browse_published")
    c.execute("DELETE FROM browse_files")
    c.commit()
    (root / "pics/browse/20260703_145634.jpg").write_bytes(b"old full-size copy")
    (root / "pics/browse/640/20260101_000000-640.jpg").write_bytes(b"a stray old favorite")

    rendered = []
    monkeypatch.setattr(browse, "stage_images", lambda cfg, rows, out, **kw: rendered.extend(rows) or [])
    log = publish(tmp_path, port)
    assert rendered == []  # adopted, not re-uploaded
    assert any("1 adopted without re-uploading" in line and "1 unused full-size" in line for line in log)
    assert not (root / "pics/browse/20260703_145634.jpg").exists()
    assert not (root / "pics/browse/640/20260101_000000-640.jpg").exists()
    assert conn(tmp_path).execute("SELECT COUNT(*) FROM browse_published").fetchone()[0] == 1


def test_verify_reuploads_a_photo_missing_on_the_server(psort, tmp_path, ftp_site):
    psort("run")
    favorite(tmp_path, "20260703_145634")
    root, port = ftp_site
    publish(tmp_path, port)
    (root / "pics/browse/1024/20260703_145634-1024.jpg").unlink()
    assert "Already up to date" in publish(tmp_path, port)[-1]  # the records can't know
    log = publish(tmp_path, port, verify=True)
    assert "Uploaded 1 new photo(s)" in log[-1]
    assert (root / "pics/browse/1024/20260703_145634-1024.jpg").exists()


def test_a_new_render_version_reuploads_everything(psort, tmp_path, ftp_site, monkeypatch):
    psort("run")
    favorite(tmp_path, "20260703_145634")
    root, port = ftp_site
    publish(tmp_path, port)
    monkeypatch.setattr(browse, "RENDER_VERSION", browse.RENDER_VERSION + 1)
    assert "Uploaded 1 new photo(s)" in publish(tmp_path, port)[-1]


def test_dry_run_reports_the_plan_without_connecting(psort, tmp_path, ftp_site):
    psort("run")
    favorite(tmp_path, "20260703_145634")
    root, port = ftp_site
    publish(tmp_path, port)
    favorite(tmp_path, "20260705_090000")
    log = []
    staged = browse.publish(load(tmp_path / "psort.toml"), conn(tmp_path), settings_for(tmp_path, port), None,
                            dry_run=True, log=log.append)
    assert "1 photo(s) to upload, 0 to remove, 1 unchanged" in log[-1]
    assert (staged / "640/20260705_090000-640.jpg").exists() and not (staged / "640/20260703_145634-640.jpg").exists()
    assert not (root / "pics/browse/640/20260705_090000-640.jpg").exists()

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


def test_publish_uploads_images_and_always_replaces_the_manifest(psort, tmp_path, ftp_site):
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
    assert (root / "pics/browse/20260703_145634.jpg").exists()
    assert (root / "pics/browse/640/20260703_145634-640.jpg").exists()

    favorite(tmp_path, "20260705_090000")  # a second favorite changes the manifest
    run_publish()
    manifest = json.loads(manifest_path.read_text())
    assert {p["id"] for p in manifest["photos"]} == {"20260703_145634", "20260705_090000"}

"""Publishing posts: the markdown format, image sizes, FTP upload, and git commit/push."""

import re
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest
from PIL import Image
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import FTPServer

from psort import blog
from psort.config import load
from psort.review import create_app


# ---- Fixtures: a local FTP server standing in for Turbify, and a blog repo with a GitHub-like remote ----

@pytest.fixture
def ftp_site(tmp_path):
    root = tmp_path / "turbify"
    (root / "pics/blog/1024").mkdir(parents=True)
    (root / "pics/blog/1024/20260101_000000-1024.jpg").write_bytes(b"existing")
    auth = DummyAuthorizer()
    auth.add_user("sjnelson@itsallonesong.com", "secret", str(root), perm="elradfmw")
    handler = type("H", (FTPHandler,), {"authorizer": auth})
    server = FTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.2}, daemon=True)
    thread.start()
    yield root, server.address[1]
    server.close_all()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def blog_repo(tmp_path):
    remote = tmp_path / "github.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "blog"
    subprocess.run(["git", "clone", "-q", str(remote), str(repo)], check=True, capture_output=True)
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "all_collections/_authors").mkdir(parents=True)
    for a in ("steve", "linda", "camilla"):
        (repo / f"all_collections/_authors/{a}.md").write_text(f"---\nshort_name: {a}\n---\n")
    (repo / "all_collections/_posts").mkdir()
    (repo / "all_collections/_posts/2026-07-03-go-dogs-go.md").write_text(
        '---\ntitle: "Go, Dogs. Go!"\nauthor: steve\ncategories: ["summer vacation 2026"]\n'
        'tags: ["2026", "driving"]\n---\n\nFirst part.\n')
    (repo / "notes-not-for-commit.txt").write_text("untracked; must never be committed")
    git(repo, "add", "all_collections")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "push", "-q", "-u", "origin", "main")
    return repo, remote


@pytest.fixture
def setup(psort, tmp_path, ftp_site, blog_repo, monkeypatch):
    root, port = ftp_site
    repo, remote = blog_repo
    monkeypatch.setattr(blog, "SECRETS_PATH", tmp_path / "secrets.toml")
    psort("run")
    out = psort("blog-login", "--host", "127.0.0.1", "--port", str(port), "--no-tls", "--repo", str(repo),
                input="secret\n").output
    assert "Login OK" in out
    return root, repo, remote


def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "state/psort.db")
    c.row_factory = sqlite3.Row
    return c


def add_to_tray(tmp_path, *names):
    c = conn(tmp_path)
    for i, n in enumerate(names, start=1):
        c.execute("INSERT INTO tray (sha256, position) SELECT sha256, ? FROM photos WHERE name = ?", (i, n))
    c.commit()


def ui(tmp_path):
    client = create_app(load(tmp_path / "psort.toml"), tmp_path / "psort.toml").test_client()
    token = re.search(r'name="csrf" value="([^"]+)"', client.get("/events").get_data(as_text=True)).group(1)
    return client, token


# ---- The post format ----

def test_post_matches_the_actions_format():
    draft = blog.Draft(title='Go, "Dogs". Go!', author="steve", categories="summer vacation 2026",
                       tags="2026, driving,summer", top_text="Intro.", youtube_id="abc123", bottom_text="The end.",
                       quote="Get up!\nIt is day.", quote_attribution="P.D. Eastman",
                       captions={"a": {"text_before": "Pups.", "alt": "Two pups"}, "b": {"alt": ""}})
    photos = [{"sha256": "a", "name": "20260703_091654_1"}, {"sha256": "b", "name": "20260703_145633"}]
    assert blog.render_post(draft, photos) == (
        '---\n'
        'title: "Go, \\"Dogs\\". Go!"\n'
        'author: steve\n'
        'categories: ["summer vacation 2026"]\n'
        'tags: ["2026", "driving", "summer"]\n'
        'youtubeId: abc123\n'
        '---\n\n'
        'Intro.\n\n'
        'Pups.\n\n'
        '![Two pups]({{ site.pics_url }}20260703_091654_1.jpg)\n\n'
        '![]({{ site.pics_url }}20260703_145633.jpg)\n\n'
        '{% include youtubePlayer.html id="abc123" %}\n\n'
        'The end.\n'
        '\n'
        '> Get up!  \n  \n> It is day.  \n  \n\n'
        '\n- P.D. Eastman\n'
    )


def test_appending_to_an_existing_post():
    draft = blog.Draft(existing_post="2026-07-03-go-dogs-go.md", captions={"a": {"text_before": "More."}})
    photos = [{"sha256": "a", "name": "20260704_100000"}]
    assert blog.render_post(draft, photos, "---\ntitle: x\n---\n\nFirst part.\n") == (
        "---\ntitle: x\n---\n\nFirst part.\n\n\nMore.\n\n![]({{ site.pics_url }}20260704_100000.jpg)\n")


def test_post_url_uses_jekyll_permalinks():
    s = blog.BlogSettings(repo=Path("."), ftp_host="h", ftp_user="u")
    text = '---\ncategories: ["summer vacation 2026"]\n---\n'
    assert blog.post_url(s, "2026-07-04-gothic-again.md", text) == \
        "https://blog.itsallonesong.com/summer%20vacation%202026/2026/07/04/gothic-again.html"


# ---- Images ----

def test_stage_images_makes_the_blogs_sizes(psort, tmp_path):
    psort("run")
    cfg = load(tmp_path / "psort.toml")
    rows = conn(tmp_path).execute("SELECT * FROM photos WHERE name IN ('20260703_145640', '20260705_090000')").fetchall()
    files = blog.stage_images(cfg, rows, tmp_path / "stage")
    remotes = {r for _, r in files}
    assert "20260703_145640.jpg" in remotes and "640/20260703_145640-640.jpg" in remotes
    assert len(files) == 2 * 8
    with Image.open(tmp_path / "stage/20260705_090000.jpg") as im:
        assert im.size == (600, 800)  # the portrait photo, upright
    with Image.open(tmp_path / "stage/640/20260705_090000-640.jpg") as im:
        assert im.size == (600, 800)  # narrower than 640: copied, not enlarged
    with Image.open(tmp_path / "stage/1024/20260703_145640-1024.jpg") as im:
        assert im.size == (800, 600)  # smaller than 1024: copied, never enlarged


# ---- Login, upload, publish ----

def test_blog_login_rejects_a_wrong_password(psort, tmp_path, ftp_site, blog_repo, monkeypatch):
    monkeypatch.setattr(blog, "SECRETS_PATH", tmp_path / "secrets.toml")
    out = psort("blog-login", "--host", "127.0.0.1", "--port", str(ftp_site[1]), "--no-tls",
                "--repo", str(blog_repo[0]), input="wrong\n", expect=1).output
    assert "login was rejected" in out
    assert not (tmp_path / "secrets.toml").exists()


def test_blog_login_saves_private_settings(setup, tmp_path):
    secrets = tmp_path / "secrets.toml"
    assert oct(secrets.stat().st_mode)[-3:] == "600"
    assert "secret" not in (tmp_path / "psort.toml").read_text()  # the password never goes in the config
    assert 'ftp_user = "sjnelson@itsallonesong.com"' in (tmp_path / "psort.toml").read_text()


def test_publish_new_post_end_to_end(setup, tmp_path):
    root, repo, remote = setup
    add_to_tray(tmp_path, "20260703_145640", "20260705_090000")
    client, token = ui(tmp_path)
    page = client.get("/tray").get_data(as_text=True)
    assert 'value="2026-07-03"' in page and "summer vacation 2026" in page  # first photo's date; known categories
    sha = conn(tmp_path).execute("SELECT sha256 FROM photos WHERE name = '20260703_145640'").fetchone()[0]
    form = {"csrf": token, "title": "Beach Day", "author": "linda", "categories": "summer vacation 2026",
            "tags": "2026, beach", f"text_before_{sha}": "Sand everywhere.", f"alt_{sha}": "The beach",
            "post_date": "2026-07-03"}

    preview = client.post("/tray/compose", data={**form, "action": "preview"}).get_data(as_text=True)
    assert "2026-07-03-beach-day.md" in preview and "![The beach]({{ site.pics_url }}20260703_145640.jpg)" in preview

    dry = client.post("/tray/compose", data={**form, "action": "dryrun"}).get_data(as_text=True)
    assert "Dry run" in dry
    assert not (root / "pics/blog/20260703_145640.jpg").exists()  # nothing uploaded yet

    done = client.post("/tray/compose", data={**form, "action": "publish"}).get_data(as_text=True)
    assert "Published" in done
    assert "https://blog.itsallonesong.com/summer%20vacation%202026/2026/07/03/beach-day.html" in done
    assert (root / "pics/blog/20260703_145640.jpg").exists()
    assert (root / "pics/blog/2048/20260705_090000-2048.jpg").exists()
    assert (root / "pics/blog/1024/20260101_000000-1024.jpg").read_bytes() == b"existing"  # untouched

    pushed = subprocess.run(["git", "--git-dir", str(remote), "show", "main:all_collections/_posts/2026-07-03-beach-day.md"],
                            check=True, capture_output=True, text=True).stdout
    assert pushed.startswith('---\ntitle: "Beach Day"\nauthor: linda\n') and "Sand everywhere." in pushed
    assert "notes-not-for-commit.txt" not in git(repo, "show", "--stat", "HEAD")  # only the post was committed
    assert "?? notes-not-for-commit.txt" in git(repo, "status", "--porcelain")
    c = conn(tmp_path)
    assert c.execute("SELECT COUNT(*) FROM tray").fetchone()[0] == 0
    assert {r[0] for r in c.execute("SELECT post FROM exports")} == {"2026-07-03-beach-day"}


def test_publish_appends_and_skips_already_uploaded(setup, tmp_path):
    root, repo, remote = setup
    add_to_tray(tmp_path, "20260703_145640")
    client, token = ui(tmp_path)
    form = {"csrf": token, "existing_post": "2026-07-03-go-dogs-go.md", "bottom_text": "Later that day."}
    client.post("/tray/compose", data={**form, "action": "publish"})
    text = (repo / "all_collections/_posts/2026-07-03-go-dogs-go.md").read_text()
    assert text.startswith('---\ntitle: "Go, Dogs. Go!"') and text.endswith("Later that day.\n")

    # Publishing the same photo again uploads nothing new.
    add_to_tray(tmp_path, "20260703_145640")
    c = conn(tmp_path)
    blog.save_draft(c, blog.Draft(existing_post="2026-07-03-go-dogs-go.md"))  # publishing cleared the last draft
    result = blog.publish(load(tmp_path / "psort.toml"), c, blog.settings(tmp_path / "psort.toml"))
    assert result.uploaded == 0 and result.skipped == 8


def test_publish_refuses_to_overwrite_a_different_remote_file(setup, tmp_path):
    root, repo, remote = setup
    (root / "pics/blog/20260703_145640.jpg").write_bytes(b"someone else's photo")
    add_to_tray(tmp_path, "20260703_145640")
    client, token = ui(tmp_path)
    client.post("/tray/compose", data={"csrf": token, "title": "Clash", "action": "publish"})
    page = client.get("/tray").get_data(as_text=True)
    assert "already exists on the server and is a different file" in page
    assert (root / "pics/blog/20260703_145640.jpg").read_bytes() == b"someone else's photo"
    assert not (repo / "all_collections/_posts/2026-07-03-clash.md").exists()  # no post without its photos


def test_title_needed_and_duplicate_filename_refused(setup, tmp_path):
    add_to_tray(tmp_path, "20260703_145640")
    client, token = ui(tmp_path)
    client.post("/tray/compose", data={"csrf": token, "title": "", "action": "preview"})
    assert "Give the post a title" in client.get("/tray").get_data(as_text=True)
    client.post("/tray/compose", data={"csrf": token, "title": "Go Dogs Go", "action": "dryrun"})
    assert "already exists" in client.get("/tray").get_data(as_text=True)


def test_reorder_tray(setup, tmp_path):
    add_to_tray(tmp_path, "20260703_145640", "20260705_090000")
    client, token = ui(tmp_path)
    sha = conn(tmp_path).execute("SELECT sha256 FROM photos WHERE name = '20260705_090000'").fetchone()[0]
    client.post(f"/tray/{sha}/move", data={"csrf": token, "step": "-1"})
    order = [r[0] for r in conn(tmp_path).execute(
        "SELECT p.name FROM tray t JOIN photos p ON p.sha256 = t.sha256 ORDER BY t.position")]
    assert order == ["20260705_090000", "20260703_145640"]

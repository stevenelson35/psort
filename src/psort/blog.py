"""Publishing posts to the Jekyll blog (DESIGN.md §8, Phase 2).

Reproduces what blogupdate.html + process-blog-post.yml do, without Cloudinary or the Action:
  1. web copies of the tray photos plus the blog's responsive sizes
     (pics/blog/<name>.jpg and pics/blog/<size>/<name>-<size>.jpg),
  2. uploaded to Turbify over FTP with TLS (explicit FTPS),
  3. the post written in the same markdown format, committed to the blog repo and pushed.
"""

import ftplib
import json
import os
import re
import shutil
import sqlite3
import ssl
import stat
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import quote

from PIL import Image

from .config import DEFAULT_CONFIG_PATH, Config
from .events import slugify
from .export import render

SIZES = [2048, 1920, 1600, 1366, 1024, 768, 640]  # same list as the blog's create_responsive_images.py
QUALITY = 85
SECRETS_PATH = DEFAULT_CONFIG_PATH.parent / "secrets.toml"


class BlogError(Exception):
    pass


# ---- Settings and secrets ----

@dataclass
class BlogSettings:
    repo: Path
    ftp_host: str
    ftp_user: str
    remote_dir: str = "pics/blog"
    site_url: str = "https://blog.itsallonesong.com"
    default_author: str = "steve"
    ftp_tls: bool = True  # tests use a plain local FTP server
    ftp_port: int = 21

    @property
    def posts_dir(self) -> Path:
        return self.repo / "all_collections" / "_posts"


def settings(cfg_path: Path) -> BlogSettings:
    data = tomllib.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    b = data.get("blog")
    if not b:
        raise BlogError("Blog publishing isn't set up yet. Run `psort blog-login` first.")
    return BlogSettings(
        repo=Path(b["repo"]).expanduser(), ftp_host=b["ftp_host"], ftp_user=b["ftp_user"],
        remote_dir=b.get("remote_dir", "pics/blog"), site_url=b.get("site_url", "https://blog.itsallonesong.com"),
        default_author=b.get("default_author", "steve"), ftp_tls=b.get("ftp_tls", True),
        ftp_port=int(b.get("ftp_port", 21)),
    )


def write_settings(cfg_path: Path, s: BlogSettings) -> None:
    """Add or replace the [blog] section of psort.toml, leaving everything else as it is."""
    text = cfg_path.read_text()
    text = re.sub(r"\n\[blog\]\n(?:(?!\n\[).)*", "", text, flags=re.S).rstrip() + "\n"
    q = json.dumps
    text += f"""
[blog]
# Publishing posts: photos go to Turbify over FTP with TLS; the post is committed to the blog repo.
repo = {q(str(s.repo))}
ftp_host = {q(s.ftp_host)}
ftp_user = {q(s.ftp_user)}
remote_dir = {q(s.remote_dir)}
site_url = {q(s.site_url)}
default_author = {q(s.default_author)}
"""
    if not s.ftp_tls:
        text += "ftp_tls = false\n"
    if s.ftp_port != 21:
        text += f"ftp_port = {s.ftp_port}\n"
    cfg_path.write_text(text)


def save_password(password: str, path: Path | None = None) -> None:
    """Stored only on this PC, readable only by you (never in a repo)."""
    path = path or SECRETS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"# psort secrets. Keep this file private.\nftp_password = {json.dumps(password)}\n")
    os.chmod(path, 0o600)


def load_password(path: Path | None = None) -> str:
    path = path or SECRETS_PATH
    if not path.exists():
        raise BlogError("No FTP password saved. Run `psort blog-login`.")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise BlogError(f"{path} is readable by other users. Run: chmod 600 {path}")
    return tomllib.loads(path.read_text())["ftp_password"]


# ---- FTP ----

class Uploader:
    """FTP with TLS to the blog photos folder. Never overwrites a different file."""

    def __init__(self, s: BlogSettings, password: str):
        self.s = s
        if s.ftp_tls:
            self.ftp = ftplib.FTP_TLS(context=ssl.create_default_context(), timeout=60)
        else:
            self.ftp = ftplib.FTP(timeout=60)
        try:
            self.ftp.connect(s.ftp_host, s.ftp_port)
            self.ftp.login(s.ftp_user, password)
            if s.ftp_tls:
                self.ftp.prot_p()  # encrypt file transfers too, not just the login
            self.ftp.cwd(s.remote_dir)
        except ftplib.error_perm as e:
            self.close()
            if str(e).startswith("530"):
                raise BlogError("FTP login was rejected: check the username and password.") from e
            raise BlogError(f"Can't open the folder {s.remote_dir!r} on the server ({e}).") from e
        except (OSError, ftplib.Error) as e:
            self.close()
            raise BlogError(f"Couldn't connect to {s.ftp_host} ({e}).") from e
        self.ftp.voidcmd("TYPE I")

    def close(self) -> None:
        try:
            self.ftp.quit()
        except Exception:
            self.ftp.close()

    def check(self) -> list[str]:
        """A few existing names from the 1024 folder, proving this is the blog photos folder."""
        try:
            return sorted(self.ftp.nlst("1024"))[:3]
        except ftplib.error_perm:
            return []

    def _size(self, remote: str) -> int | None:
        try:
            return self.ftp.size(remote)
        except ftplib.error_perm:
            return None

    def upload(self, local: Path, remote: str) -> str:
        """'uploaded', or 'skipped' if the same file is already there."""
        existing = self._size(remote)
        if existing is not None:
            if existing == local.stat().st_size:
                return "skipped"
            raise BlogError(f"{self.s.remote_dir}/{remote} already exists on the server and is a different file. "
                            "Not overwriting it.")
        if "/" in remote:
            folder = remote.rsplit("/", 1)[0]
            try:
                self.ftp.mkd(folder)
            except ftplib.error_perm:
                pass  # already there
        with local.open("rb") as f:
            self.ftp.storbinary(f"STOR {remote}", f)
        return "uploaded"


# ---- Images ----

def stage_images(cfg: Config, photos: list[sqlite3.Row], out: Path) -> list[tuple[Path, str]]:
    """Web copy + responsive sizes for each photo. Returns (local file, remote path) pairs."""
    files = []
    for p in photos:
        base = out / f"{p['name']}.jpg"
        render(cfg.library / p["library_path"], base)  # upright, ≤2048px, no GPS
        files.append((base, base.name))
        with Image.open(base) as im:
            width = im.width
            for size in SIZES:
                dest = out / str(size) / f"{p['name']}-{size}.jpg"
                dest.parent.mkdir(parents=True, exist_ok=True)
                if size >= width:
                    shutil.copyfile(base, dest)  # never enlarge (like the blog's script)
                else:
                    small = im.resize((size, round(im.height * size / width)), Image.LANCZOS)
                    small.save(dest, "JPEG", quality=QUALITY, optimize=True, progressive=True)
                files.append((dest, f"{size}/{dest.name}"))
    return files


# ---- The post ----

@dataclass
class Draft:
    title: str = ""
    author: str = ""
    categories: str = ""  # comma-separated, as blogupdate.html sends them
    tags: str = ""
    top_text: str = ""
    youtube_id: str = ""
    bottom_text: str = ""
    quote: str = ""
    quote_attribution: str = ""
    existing_post: str = ""  # filename to append to; empty = new post
    post_date: str = ""  # YYYY-MM-DD for a new post; empty = first photo's date
    captions: dict[str, dict[str, str]] = field(default_factory=dict)  # sha → {text_before, alt}


def load_draft(conn: sqlite3.Connection) -> Draft:
    row = conn.execute("SELECT data FROM post_draft WHERE id = 1").fetchone()
    return Draft(**json.loads(row["data"])) if row else Draft()


def save_draft(conn: sqlite3.Connection, draft: Draft) -> None:
    conn.execute("INSERT OR REPLACE INTO post_draft (id, data) VALUES (1, ?)", (json.dumps(draft.__dict__),))
    conn.commit()


def tray_photos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.sha256, p.name, p.library_path, p.taken_at, t.position FROM tray t
           JOIN photos p ON p.sha256 = t.sha256 ORDER BY t.position, p.taken_at, p.name"""
    ).fetchall()


def _yaml_list(items: str) -> str:
    values = [v.strip() for v in items.split(",") if v.strip()]
    return "[" + ", ".join(f'"{v}"' for v in values) + "]" if values else "[]"


def _blockquote(text: str) -> str:
    lines = text.splitlines() or [""]
    out = []
    for line in lines:
        out.append(f"> {line}  " if line else ">  ")
        out.append("  ")  # keeps each line its own blockquote, as the Action writes it
    return "\n".join(out)


def post_filename(draft: Draft, photos: list[sqlite3.Row]) -> str:
    if draft.existing_post:
        return os.path.basename(draft.existing_post)
    if not draft.title.strip():
        raise BlogError("Give the post a title.")
    day = draft.post_date or (photos[0]["taken_at"][:10] if photos else date.today().isoformat())
    return f"{day}-{slugify(draft.title)}.md"


def render_post(draft: Draft, photos: list[sqlite3.Row], existing_text: str | None = None) -> str:
    """The post file's full contents, in exactly the format process-blog-post.yml writes."""
    parts = []
    if draft.top_text.strip():
        parts.append(draft.top_text.strip())
    for p in photos:
        cap = draft.captions.get(p["sha256"], {})
        if cap.get("text_before", "").strip():
            parts.append(cap["text_before"].strip())
        parts.append(f"![{cap.get('alt', '').strip()}]({{{{ site.pics_url }}}}{p['name']}.jpg)")
    if draft.youtube_id.strip():
        parts.append(f'{{% include youtubePlayer.html id="{draft.youtube_id.strip()}" %}}')
    if draft.bottom_text.strip():
        parts.append(draft.bottom_text.strip())
    body = "\n\n".join(parts)

    if existing_text is not None:
        text = existing_text + (f"\n\n{body}\n" if body else "")
    else:
        title = draft.title.strip().replace('"', '\\"')
        fm = ["---", f'title: "{title}"', f"author: {draft.author.strip() or 'steve'}"]
        if draft.categories.strip():
            fm.append(f"categories: {_yaml_list(draft.categories)}")
        if draft.tags.strip():
            fm.append(f"tags: {_yaml_list(draft.tags)}")
        if draft.youtube_id.strip():
            fm.append(f"youtubeId: {draft.youtube_id.strip()}")
        fm.append("---")
        text = "\n".join(fm) + "\n\n" + (body + "\n" if body else "")

    if draft.quote.strip() or draft.quote_attribution.strip():
        text += "\n"
        if draft.quote.strip():
            text += _blockquote(draft.quote.strip()) + "\n\n"
        if draft.quote_attribution.strip():
            text += f"\n- {draft.quote_attribution.strip()}\n"
    return text


def post_url(s: BlogSettings, filename: str, text: str) -> str:
    """Jekyll's default permalink: /<categories>/YYYY/MM/DD/<slug>.html"""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})-(.+)\.md$", filename)
    if not m:
        return s.site_url
    cats = re.search(r'^categories:\s*\[(.*)\]\s*$', text, re.M)
    names = [c.strip().strip('"') for c in cats.group(1).split(",")] if cats and cats.group(1).strip() else []
    path = "/".join([*(quote(c) for c in names), m[1], m[2], m[3], f"{m[4]}.html"])
    return f"{s.site_url.rstrip('/')}/{path}"


# ---- What the blog already has (for the composer's pick lists) ----

def blog_choices(s: BlogSettings) -> dict:
    authors = sorted(p.stem for p in (s.repo / "all_collections" / "_authors").glob("*.md"))
    categories, tags, posts = set(), set(), []
    for f in sorted(s.posts_dir.glob("*.md"), reverse=True):
        text = f.read_text(errors="replace")
        title = re.search(r'^title:\s*"?(.*?)"?\s*$', text, re.M)
        posts.append({"file": f.name, "title": title.group(1) if title else f.stem})
        for key, bucket in (("categories", categories), ("tags", tags)):
            m = re.search(rf"^{key}:\s*\[(.*)\]\s*$", text, re.M)
            if m:
                bucket.update(v.strip().strip('"') for v in m.group(1).split(",") if v.strip())
    return {"authors": authors, "categories": sorted(categories), "tags": sorted(tags), "posts": posts}


# ---- Publishing ----

@dataclass
class PublishResult:
    filename: str
    url: str
    text: str
    staged: Path
    uploaded: int = 0
    skipped: int = 0
    log: list[str] = field(default_factory=list)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise BlogError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def publish(cfg: Config, conn: sqlite3.Connection, s: BlogSettings, dry_run: bool = False,
            password: str | None = None, log: Callable[[str], None] = lambda _: None) -> PublishResult:
    """Dry run: build everything locally (images + post) without uploading or committing."""
    draft = load_draft(conn)
    photos = tray_photos(conn)
    if not photos and not (draft.top_text or draft.bottom_text or draft.youtube_id):
        raise BlogError("The post tray is empty.")
    filename = post_filename(draft, photos)
    target = s.posts_dir / filename
    if draft.existing_post and not target.exists():
        raise BlogError(f"No post {filename} in the blog repo.")
    if not draft.existing_post and target.exists():
        raise BlogError(f"{filename} already exists. Pick it under 'Add to existing post', or change the title.")

    staged = cfg.state_dir / "publish" / filename.removesuffix(".md")
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    result = PublishResult(filename, "", "", staged)

    def note(msg):
        result.log.append(msg)
        log(msg)

    files = stage_images(cfg, photos, staged)
    note(f"Prepared {len(photos)} photo(s) and their sizes ({len(files)} files).")

    if not dry_run:
        if not (s.repo / ".git").exists():
            raise BlogError(f"{s.repo} isn't a git repo.")
        _git(s.repo, "pull", "--ff-only", "--quiet")  # pick up posts made from the phone first
        note("Pulled the latest blog from GitHub.")
    existing = target.read_text() if draft.existing_post else None
    result.text = render_post(draft, photos, existing)
    result.url = post_url(s, filename, result.text)
    (staged / filename).write_text(result.text)

    if dry_run:
        note(f"Dry run: nothing uploaded or committed. Files are in {staged}.")
        return result

    up = Uploader(s, password or load_password())
    try:
        for local, remote in files:
            if up.upload(local, remote) == "uploaded":
                result.uploaded += 1
            else:
                result.skipped += 1
    finally:
        up.close()
    note(f"Uploaded {result.uploaded} file(s) to {s.remote_dir}/ ({result.skipped} already there).")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(result.text)
    rel = str(target.relative_to(s.repo))
    _git(s.repo, "add", "--", rel)
    verb = "Add to post" if draft.existing_post else "Add post"
    _git(s.repo, "commit", "--quiet", "-m", f"{verb}: {draft.title or filename} (psort)", "--", rel)
    _git(s.repo, "push", "--quiet")
    note(f"Committed {rel} and pushed. The site rebuilds in a couple of minutes.")

    slug = filename.removesuffix(".md")
    conn.executemany("INSERT OR REPLACE INTO exports (sha256, post) VALUES (?, ?)", [(p["sha256"], slug) for p in photos])
    conn.execute("DELETE FROM tray")
    conn.execute("DELETE FROM post_draft")
    conn.commit()
    return result

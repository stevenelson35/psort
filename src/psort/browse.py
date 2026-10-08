"""Publish favorites for the photo browser page (DESIGN.md §12 backlog: "an improved image
browsing system").

Renders web-size copies of every favorite (reusing the blog's rendering and sizes) plus a slim
public JSON manifest — names, dates, who's in each photo, and which named event it falls in.
Photos marked private are left out, and anything published earlier that's no longer wanted
(un-starred, or marked private since) is removed from the server.

Incremental: `browse_published` records what's already up there, so a run renders and uploads only
new photos, deletes only removed ones, and replaces the manifest only when it changed. With nothing
to do it doesn't even connect. `verify` (automatic the first time) lists the server instead and
repairs any difference.
Face *embeddings* never leave the WSL database; only people's names are published, same as
highlights/ (DESIGN.md §5.7, §5.9). Everything is uploaded to Turbify beside the blog's photos,
so the browse page (browse.html in the blog repo) is pure static JSON + JS: no server code.
"""

import ftplib
import hashlib
import io
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .blog import SIZES, BlogSettings, Uploader, stage_images
from .config import Config
from .events import named_ranges, slug_for

MANIFEST_NAME = "browse-manifest.json"
# Bump when the published images change (sizes, quality, rendering), so every photo is re-uploaded.
RENDER_VERSION = 1
# An empty page so the folder can't be listed in a browser (safer than "Options -Indexes" in
# .htaccess, which breaks the whole folder if the host doesn't allow that override).
INDEX_HTML = "<!doctype html><title></title>\n"


class BrowseError(Exception):
    pass


def favorite_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT p.sha256, p.name, p.library_path, p.taken_at, fav.top FROM photos p
           JOIN favorites fav ON fav.sha256 = p.sha256
           WHERE p.library_path IS NOT NULL AND p.private = 0
           ORDER BY p.taken_at, p.name"""
    ).fetchall()


def build_manifest(conn: sqlite3.Connection) -> dict:
    """The public data behind the browse page: favorites that aren't private, by name (not sha256, and never
    an embedding). `events` lists only named events that actually contain a favorite."""
    rows = favorite_rows(conn)
    people: dict[str, list[str]] = {}
    for r in conn.execute(
        """SELECT DISTINCT f.sha256, p.name FROM faces f JOIN people p ON p.id = f.person_id
           JOIN favorites fav ON fav.sha256 = f.sha256 ORDER BY p.name"""
    ):
        people.setdefault(r["sha256"], []).append(r["name"])
    ranges = named_ranges(conn)
    by_slug = {r["slug"]: r for r in ranges}
    used_events: set[str] = set()
    photos = []
    for r in rows:
        slug = slug_for(r["taken_at"], ranges)
        if slug:
            used_events.add(slug)
        photo = {"id": r["name"], "taken_at": r["taken_at"], "people": people.get(r["sha256"], []), "event": slug}
        if r["top"]:
            photo["top"] = True  # only present on top picks, so older data and manifests are unchanged
        photos.append(photo)
    events = [{"slug": s, "name": s.replace("-", " "), "start": by_slug[s]["start"], "end": by_slug[s]["end"]}
              for s in sorted(used_events)]
    return {"version": 1, "photos": photos, "events": events}


def _size_paths(name: str) -> list[str]:
    return [f"{size}/{name}-{size}.jpg" for size in SIZES]


def _cors(settings: BlogSettings) -> str:
    origin = urlsplit(settings.site_url)
    if not origin.scheme or not origin.netloc:
        raise BrowseError(f"Invalid site URL for CORS: {settings.site_url!r}")
    return ("<IfModule mod_headers.c>\n"
            f'  Header always set Access-Control-Allow-Origin "{origin.scheme}://{origin.netloc}"\n'
            "</IfModule>\n")


@dataclass
class Plan:
    """What a publish will do, worked out from the database alone (no server needed)."""
    new: list[sqlite3.Row] = field(default_factory=list)      # photos to render and upload
    gone: list[str] = field(default_factory=list)             # names to delete from the server
    unchanged: int = 0
    files: dict[str, bytes] = field(default_factory=dict)     # manifest / .htaccess / index.html that changed

    def empty(self) -> bool:
        return not (self.new or self.gone or self.files)


def plan(conn: sqlite3.Connection, settings: BlogSettings, verify: bool = False) -> Plan:
    """Compare the favorites with what `browse_published` says is on the server. With `verify`,
    every small file is treated as changed (the photos are checked against the server listing later)."""
    rows = favorite_rows(conn)
    if not rows:
        raise BrowseError("No favorites to publish yet (private photos are left out). "
                          "Star some photos (☆) in the review UI first.")
    have = {r["name"]: r for r in conn.execute(
        "SELECT name, sha256, version FROM browse_published WHERE remote_dir = ?", (settings.remote_dir,))}
    wanted = {r["name"]: r for r in rows}
    result = Plan()
    for name, r in wanted.items():
        old = have.get(name)
        if old and old["sha256"] == r["sha256"] and old["version"] == RENDER_VERSION:
            result.unchanged += 1
        else:
            result.new.append(r)
    result.gone = sorted(set(have) - set(wanted))
    digests = {r["path"]: r["digest"] for r in conn.execute(
        "SELECT path, digest FROM browse_files WHERE remote_dir = ?", (settings.remote_dir,))}
    small = {
        MANIFEST_NAME: json.dumps(build_manifest(conn), indent=1).encode(),
        ".htaccess": _cors(settings).encode(),
        "index.html": INDEX_HTML.encode(),
    }
    for path, data in small.items():
        if verify or digests.get(path) != hashlib.sha256(data).hexdigest():
            result.files[path] = data
    return result


class _Server:
    """The browse folder on Turbify. psort owns this folder, so it replaces files freely."""

    def __init__(self, up: Uploader):
        self.ftp = up.ftp
        self.folders: set[str] = set()

    def put(self, local: Path | None, remote: str, data: bytes | None = None) -> None:
        if "/" in remote and (folder := remote.rsplit("/", 1)[0]) not in self.folders:
            try:
                self.ftp.mkd(folder)
            except ftplib.error_perm:
                pass  # already there
            self.folders.add(folder)
        if data is not None:
            self.ftp.storbinary(f"STOR {remote}", io.BytesIO(data))
        else:
            with local.open("rb") as f:
                self.ftp.storbinary(f"STOR {remote}", f)

    def replace(self, remote: str, data: bytes) -> None:
        """Upload beside, then rename over the old one, so the page never sees a missing or half file."""
        temp = remote + ".uploading"
        self.put(None, temp, data)
        try:
            self.ftp.rename(temp, remote)
        except (ftplib.error_perm, ftplib.error_temp):  # a server that won't rename over an existing file
            self.delete(remote)
            self.ftp.rename(temp, remote)

    def delete(self, remote: str) -> None:
        try:
            self.ftp.delete(remote)
        except (ftplib.error_perm, ftplib.error_temp):
            pass  # already gone

    def listing(self, folder: str) -> set[str]:
        try:
            return {n.rsplit("/", 1)[-1] for n in self.ftp.nlst(folder)}
        except (ftplib.error_perm, ftplib.error_temp):
            return set()  # no such folder yet, or empty (some servers answer 450/550 for that)


def _record(conn: sqlite3.Connection, remote_dir: str, name: str, sha: str) -> None:
    conn.execute("INSERT OR REPLACE INTO browse_published (remote_dir, name, sha256, version) VALUES (?, ?, ?, ?)",
                 (remote_dir, name, sha, RENDER_VERSION))
    conn.commit()


def _verify(conn: sqlite3.Connection, server: _Server, remote_dir: str, todo: Plan,
            log: Callable[[str], None]) -> None:
    """Check the plan against the server's listing: adopt complete photos already up there (no
    re-upload), re-upload incomplete ones, and delete stray psort photos and the old full-size
    <name>.jpg copies the browse page never used."""
    listed = {size: server.listing(str(size)) for size in SIZES}
    complete = set.intersection(*({f.removesuffix(f"-{size}.jpg") for f in files if f.endswith(f"-{size}.jpg")}
                                  for size, files in listed.items()))
    on_server = set().union(*({f.removesuffix(f"-{size}.jpg") for f in files if f.endswith(f"-{size}.jpg")}
                              for size, files in listed.items()))
    wanted = {r["name"]: r for r in favorite_rows(conn)}
    recorded = {r["name"] for r in conn.execute(
        "SELECT name FROM browse_published WHERE remote_dir = ?", (remote_dir,))}
    adopted = 0
    new = {r["name"] for r in todo.new}
    for name in recorded - complete:  # recorded, but not (all) there: upload again if still wanted
        conn.execute("DELETE FROM browse_published WHERE remote_dir = ? AND name = ?", (remote_dir, name))
        if name in wanted and name not in new:
            todo.new.append(wanted[name])
            todo.unchanged -= 1
    for r in list(todo.new):
        if r["name"] in complete and r["name"] not in recorded:
            _record(conn, remote_dir, r["name"], r["sha256"])  # published before psort kept records
            todo.new.remove(r)
            adopted += 1
            todo.unchanged += 1
    todo.gone = sorted(set(todo.gone) | (on_server - set(wanted)))
    conn.commit()
    old_copies = [f for f in server.listing(".") if f.endswith(".jpg")]
    for f in old_copies:
        server.delete(f)
    log(f"Checked the server: {len(complete)} photo(s) complete there"
        + (f", {adopted} adopted without re-uploading" if adopted else "")
        + (f", {len(old_copies)} unused full-size copies deleted" if old_copies else "") + ".")


def publish(cfg: Config, conn: sqlite3.Connection, settings: BlogSettings, password: str | None,
            dry_run: bool = False, log: Callable[[str], None] = lambda _: None, verify: bool = False) -> Path:
    """Bring `settings.remote_dir` on Turbify up to date with the favorites (not private ones):
    upload new photos at the blog's responsive sizes (upright, GPS-free), delete removed ones,
    and replace the manifest if it changed. `verify` checks the server listing too; it's
    automatic when nothing has been recorded for this folder yet."""
    verify = verify or not conn.execute("SELECT 1 FROM browse_published WHERE remote_dir = ? LIMIT 1",
                                        (settings.remote_dir,)).fetchone()
    todo = plan(conn, settings, verify)
    staged = cfg.state_dir / "publish" / "browse"
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    if dry_run:
        stage_images(cfg, todo.new, staged, include_base=False)
        for path, data in todo.files.items():
            (staged / path).write_bytes(data)
        log(f"Dry run: {len(todo.new)} photo(s) to upload, {len(todo.gone)} to remove, {todo.unchanged} unchanged"
            + (f", and {', '.join(sorted(todo.files))}" if todo.files else "") + "."
            + (" The server will be checked too, since nothing is recorded for it yet." if verify else "")
            + f" Nothing uploaded. Files are in {staged}.")
        return staged
    if todo.empty() and not verify:
        log(f"Already up to date ({todo.unchanged} photo(s)); nothing uploaded.")
        return staged

    up = Uploader(settings, password or "")
    try:
        server = _Server(up)
        if verify:
            _verify(conn, server, settings.remote_dir, todo, log)
        for i, r in enumerate(todo.new, start=1):
            folder = staged / r["name"]
            folder.mkdir()
            for local, remote in stage_images(cfg, [r], folder, include_base=False):
                server.put(local, remote)
            _record(conn, settings.remote_dir, r["name"], r["sha256"])  # only once all its sizes are up
            shutil.rmtree(folder)
            log(f"  [{i}/{len(todo.new)}] uploaded {r['name']}")
        for path, data in todo.files.items():  # the manifest after the photos it lists, before removals
            server.replace(path, data)
            conn.execute("INSERT OR REPLACE INTO browse_files (remote_dir, path, digest) VALUES (?, ?, ?)",
                         (settings.remote_dir, path, hashlib.sha256(data).hexdigest()))
            conn.commit()
        for name in todo.gone:
            for remote in [*_size_paths(name), f"{name}.jpg"]:
                server.delete(remote)
            conn.execute("DELETE FROM browse_published WHERE remote_dir = ? AND name = ?", (settings.remote_dir, name))
            conn.commit()
            log(f"  removed {name} from the server (no longer a favorite, or marked private)")
    finally:
        up.close()
    log(f"Uploaded {len(todo.new)} new photo(s) to {settings.remote_dir}/, removed {len(todo.gone)}, "
        f"{todo.unchanged} unchanged" + (f"; updated {', '.join(sorted(todo.files))}" if todo.files else "") + ".")
    return staged

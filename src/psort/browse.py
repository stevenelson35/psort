"""Publish favorites for the photo browser page (DESIGN.md §12 backlog: "an improved image
browsing system").

Renders web-size copies of every favorite (reusing the blog's rendering and sizes) plus a slim
public JSON manifest — names, dates, who's in each photo, and which named event it falls in.
Photos marked private are left out, and anything published earlier that's no longer wanted
(un-starred, or marked private since) is removed from the server.
Face *embeddings* never leave the WSL database; only people's names are published, same as
highlights/ (DESIGN.md §5.7, §5.9). Everything is uploaded to Turbify beside the blog's photos,
so the browse page (browse.html in the blog repo) is pure static JSON + JS: no server code.
"""

import ftplib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .blog import SIZES, BlogSettings, Uploader, stage_images
from .config import Config
from .events import named_ranges, slug_for

MANIFEST_NAME = "browse-manifest.json"


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


def _upload_manifest(up: Uploader, local: Path, remote: str) -> None:
    """Unlike a favorite's images (content-addressed by name, never overwritten), the manifest
    changes every time this runs, so it always replaces whatever's already there."""
    try:
        up.ftp.delete(remote)
    except ftplib.error_perm:
        pass  # nothing there yet
    with local.open("rb") as f:
        up.ftp.storbinary(f"STOR {remote}", f)


def _remove_unwanted(up: Uploader, wanted: set[str], log: Callable[[str], None]) -> int:
    """Delete the published copies of photos no longer wanted (un-starred, or marked private).
    Every published photo has a 640 copy, so that folder's listing says what's on the server.
    Only psort's own file names are deleted."""
    try:
        listed = up.ftp.nlst(str(SIZES[-1]))
    except ftplib.error_perm:
        return 0  # nothing published yet
    suffix = f"-{SIZES[-1]}.jpg"
    gone = sorted({n.rsplit("/", 1)[-1].removesuffix(suffix) for n in listed if n.endswith(suffix)} - wanted)
    for name in gone:
        for remote in [f"{name}.jpg", *(f"{size}/{name}-{size}.jpg" for size in SIZES)]:
            try:
                up.ftp.delete(remote)
            except ftplib.error_perm:
                pass  # already gone
        log(f"  removed {name} from the server (no longer a favorite, or marked private)")
    return len(gone)


def publish(cfg: Config, conn: sqlite3.Connection, settings: BlogSettings, password: str | None,
            dry_run: bool = False, log: Callable[[str], None] = lambda _: None) -> Path:
    """Render every favorite (upright, GPS-free, at the blog's responsive sizes) plus
    browse-manifest.json, and upload both to `settings.remote_dir` on Turbify."""
    rows = favorite_rows(conn)
    if not rows:
        raise BrowseError("No favorites to publish yet (private photos are left out). "
                          "Star some photos (☆) in the review UI first.")
    staged = cfg.state_dir / "publish" / "browse"
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    files = stage_images(cfg, rows, staged)
    origin = urlsplit(settings.site_url)
    if not origin.scheme or not origin.netloc:
        raise BrowseError(f"Invalid site URL for CORS: {settings.site_url!r}")
    cors_path = staged / ".htaccess"
    cors_path.write_text(
        "<IfModule mod_headers.c>\n"
        f'  Header always set Access-Control-Allow-Origin "{origin.scheme}://{origin.netloc}"\n'
        "</IfModule>\n"
    )
    files.append((cors_path, ".htaccess"))
    log(f"Rendered {len(rows)} favorite(s), their sizes, and CORS config ({len(files)} files).")
    manifest_path = staged / MANIFEST_NAME
    manifest_path.write_text(json.dumps(build_manifest(conn), indent=1))
    if dry_run:
        log(f"Dry run: nothing uploaded or removed. Files are in {staged}.")
        return staged
    up = Uploader(settings, password or "")
    uploaded = skipped = 0
    try:
        for local, remote in files:
            if up.upload(local, remote) == "uploaded":
                uploaded += 1
            else:
                skipped += 1
        _upload_manifest(up, manifest_path, MANIFEST_NAME)  # first, so the page stops showing removed photos
        removed = _remove_unwanted(up, {r["name"] for r in rows}, log)
    finally:
        up.close()
    log(f"Uploaded {uploaded} image(s) to {settings.remote_dir}/ ({skipped} already there), plus {MANIFEST_NAME}."
        + (f" Removed {removed} photo(s) no longer published." if removed else ""))
    return staged

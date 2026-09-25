"""Videos (DESIGN.md §5.8): copied into a parallel videos/ tree that uses the same year/day/event
folder names as the photo library, and shown on each day's page in the review UI."""

import os
import sqlite3
import struct
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
from PIL import Image

from .config import Config
from .dates import NO_TIME
from .events import named_ranges, slug_for

# Browsers can play these directly (H.264/HEVC support varies by codec; the file link always works).
PLAYABLE = {".mp4", ".m4v", ".mov", ".webm"}
LIVE_PHOTO_MAX_SECONDS = 5.0
_MP4_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)


@dataclass
class VideoInfo:
    created: datetime | None  # local time, from MP4/MOV metadata
    duration: float | None
    width: int | None
    height: int | None


def mp4_creation_time(path: Path) -> datetime | None:
    """Creation time from an MP4/MOV 'mvhd' box (UTC, seconds since 1904), as local time."""
    try:
        with path.open("rb") as f:
            end = f.seek(0, os.SEEK_END)
            moov = _find_box(f, 0, end, b"moov")
            mvhd = moov and _find_box(f, moov[0], moov[1], b"mvhd")
            if not mvhd:
                return None
            f.seek(mvhd[0])
            version = f.read(4)[0]
            seconds = struct.unpack(">Q", f.read(8))[0] if version == 1 else struct.unpack(">I", f.read(4))[0]
    except (OSError, struct.error, IndexError):
        return None
    if seconds == 0:
        return None  # encoders often leave it unset
    try:
        local = (_MP4_EPOCH + timedelta(seconds=seconds)).astimezone().replace(tzinfo=None)
    except OverflowError:
        return None
    return local if 1990 <= local.year <= datetime.now().year + 1 else None


def _find_box(f, start: int, end: int, kind: bytes) -> tuple[int, int] | None:
    """(payload start, payload end) of the first `kind` box between start and end."""
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        size, box = struct.unpack(">I4s", f.read(8))
        header = 8
        if size == 1:
            size, header = struct.unpack(">Q", f.read(8))[0], 16
        elif size == 0:
            size = end - pos
        if size < header:
            return None
        if box == kind:
            return pos + header, pos + size
        pos += size
    return None


def probe(path: Path) -> VideoInfo:
    created = mp4_creation_time(path) if path.suffix.lower() in {".mp4", ".m4v", ".mov", ".3gp"} else None
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return VideoInfo(created, None, None, None)
        fps, frames = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        duration = frames / fps if fps > 0 and frames > 0 else None
        return VideoInfo(created, duration, int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None,
                         int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None)
    finally:
        cap.release()


def is_live_photo_clip(path: Path, sibling_names: set[str], duration: float | None) -> bool:
    """An iPhone Live Photo clip: a short .mov/.mp4 with the same name as a photo beside it."""
    if path.suffix.lower() not in {".mov", ".mp4"}:
        return False
    stem = path.stem.lower()
    has_photo = any(f"{stem}{ext}" in sibling_names for ext in (".heic", ".heif", ".jpg", ".jpeg"))
    return has_photo and (duration is None or duration <= LIVE_PHOTO_MAX_SECONDS)


def poster(cfg: Config, sha: str, library_path: str, size: int = 480) -> Path | None:
    """A still frame (about 1s in, or 10% for short clips) as a cached JPEG."""
    out = cfg.state_dir / "posters" / f"{sha}_{size}.jpg"
    if out.exists():
        return out
    src = cfg.videos / library_path
    cap = cv2.VideoCapture(str(src))
    try:
        fps, frames = cap.get(cv2.CAP_PROP_FPS), cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if fps > 0 and frames > 0:
            cap.set(cv2.CAP_PROP_POS_FRAMES, min(fps, frames * 0.1))
        ok, frame = cap.read()
    finally:
        cap.release()
    if not ok:
        return None
    img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    img.thumbnail((size, size))
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + f".{os.getpid()}.{id(img)}.partial")
    img.save(partial, "JPEG", quality=82)
    os.replace(partial, out)
    return out


# ---- Library layout ----

def assign_names(conn: sqlite3.Connection) -> None:
    taken = {r["name"] for r in conn.execute("SELECT name FROM videos WHERE name IS NOT NULL")}
    rows = conn.execute(
        """SELECT v.sha256, v.taken_at, (SELECT MIN(s.path) FROM sources s WHERE s.sha256 = v.sha256) AS first_path
           FROM videos v WHERE v.name IS NULL ORDER BY v.taken_at, first_path, v.sha256"""
    ).fetchall()
    for row in rows:
        stem = datetime.fromisoformat(row["taken_at"]).strftime("%Y%m%d_%H%M%S")
        name, n = stem, 0
        while name in taken:
            n += 1
            name = f"{stem}_{n}"
        taken.add(name)
        conn.execute("UPDATE videos SET name = ? WHERE sha256 = ?", (name, row["sha256"]))
    conn.commit()


def desired_paths(conn: sqlite3.Connection) -> dict[str, str]:
    """Same folder rules as photos, so a day's videos sit in the same-named folder under videos/."""
    ranges = named_ranges(conn)
    paths = {}
    for r in conn.execute("SELECT sha256, name, ext, taken_at, date_source FROM videos"):
        file = f"{r['name']}{r['ext']}"
        if r["date_source"] == "mtime":
            paths[r["sha256"]] = f"_undated/{file}"
        elif r["date_source"] == "folder-month":
            month = r["taken_at"][:7]
            paths[r["sha256"]] = f"{month[:4]}/{month}_unknown-day/{file}"
        else:
            day = r["taken_at"][:10]
            slug = None if r["date_source"] in NO_TIME else slug_for(r["taken_at"], ranges)
            paths[r["sha256"]] = f"{day[:4]}/{day}_{slug}/{file}" if slug else f"{day[:4]}/{day}/{file}"
    return paths


@dataclass
class VideoCurateStats:
    copied: int = 0
    moved: int = 0
    unchanged: int = 0
    missing: list[str] | None = None


def curate(cfg: Config, conn: sqlite3.Connection, dry_run: bool = False,
           log: Callable[[str], None] = print) -> VideoCurateStats:
    from .library import _copy_verified, _find_source, _remove_empty_parents  # shared helpers
    from .imaging import sha256_file

    assign_names(conn)
    stats = VideoCurateStats(missing=[])
    root = cfg.videos
    current = {r["sha256"]: r for r in conn.execute("SELECT sha256, name, library_path FROM videos")}
    for sha, target in sorted(desired_paths(conn).items(), key=lambda kv: kv[1]):
        row = current[sha]
        dest = root / target
        existing = root / row["library_path"] if row["library_path"] else None
        if row["library_path"] == target and dest.exists():
            stats.unchanged += 1
            continue
        if dest.exists():
            if sha256_file(dest) != sha:
                raise FileExistsError(f"{dest} exists and is a different file; refusing to overwrite")
        elif existing and existing.exists():
            log(f"  move video {row['library_path']} → {target}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(existing, dest)
                _remove_empty_parents(existing, root)
            stats.moved += 1
        else:
            src = _find_source(cfg, conn, sha)
            if src is None:
                stats.missing.append(row["name"])
                continue
            log(f"  copy video {src.relative_to(cfg.inbox)} → {target}")
            if not dry_run:
                _copy_verified(src, dest, sha)
            stats.copied += 1
        if not dry_run:
            conn.execute("UPDATE videos SET library_path = ? WHERE sha256 = ?", (target, sha))
            conn.commit()
    return stats

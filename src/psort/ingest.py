"""Stage 1: scan the inbox (read-only) and record every file (DESIGN.md §5.1)."""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import dates
from .config import Config
from .imaging import (
    IMAGE_EXTS,
    VIDEO_EXTS,
    FaceDetector,
    analyze,
    is_ignored,
    normalize_ext,
    sha256_file,
)


@dataclass
class IngestStats:
    new_photos: int = 0
    duplicates: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: int = 0
    redated: int = 0  # earlier undated photos now dated from folder names


def batch_of(rel: Path) -> str:
    return rel.parts[0] if len(rel.parts) > 1 else "(inbox root)"


def inbox_files(inbox: Path, under: Path | None = None):
    """Yield (absolute, relative-to-inbox) for every non-ignored file, in a stable order."""
    for path in sorted((under or inbox).rglob("*")):
        if path.is_file() and not is_ignored(path):
            yield path, path.relative_to(inbox)


def _taken_at(path: Path, rel: Path, exif_value: object, mtime: float) -> tuple[datetime, str]:
    if dt := dates.parse_exif_datetime(exif_value):
        return dt, "exif"
    if dt := dates.from_filename(path.name):
        return dt, "filename"
    if found := dates.from_folders(str(rel)):
        return found
    return datetime.fromtimestamp(mtime).replace(microsecond=0), "mtime"


def redate_from_folders(conn: sqlite3.Connection) -> int:
    """Photos ingested before folder dates existed (or dated only by file time): try their folder
    names now. Their library names follow the new date unless already in the tray or exported."""
    changed = 0
    rows = conn.execute(
        """SELECT p.sha256, (SELECT MIN(s.path) FROM sources s WHERE s.sha256 = p.sha256) AS path,
                  EXISTS (SELECT 1 FROM tray t WHERE t.sha256 = p.sha256)
                  OR EXISTS (SELECT 1 FROM exports e WHERE e.sha256 = p.sha256) AS published
           FROM photos p WHERE p.date_source = 'mtime'"""
    ).fetchall()
    for r in rows:
        if r["path"] and (found := dates.from_folders(r["path"])):
            dt, source = found
            conn.execute(
                "UPDATE photos SET taken_at = ?, date_source = ?" + ("" if r["published"] else ", name = NULL")
                + " WHERE sha256 = ?",
                (dt.isoformat(), source, r["sha256"]),
            )
            changed += 1
    conn.commit()
    return changed


def _is_screenshot(path: Path, camera: str | None) -> bool:
    return "screenshot" in path.name.lower() or (normalize_ext(path.suffix) == ".png" and camera is None)


def _record_source(conn, rel: Path, st, status: str, reason: str | None = None, sha: str | None = None):
    conn.execute(
        "INSERT OR REPLACE INTO sources (path, batch, size, mtime, status, reason, sha256) VALUES (?,?,?,?,?,?,?)",
        (str(rel), batch_of(rel), st.st_size, st.st_mtime, status, reason, sha),
    )


def ingest(cfg: Config, conn: sqlite3.Connection, log: Callable[[str], None] = print) -> IngestStats:
    if not cfg.inbox.is_dir():
        raise FileNotFoundError(f"Inbox not found: {cfg.inbox}")
    detector = FaceDetector.load(cfg.face_model)
    stats = IngestStats()

    for n, (path, rel) in enumerate(inbox_files(cfg.inbox), start=1):
        st = path.stat()
        known = conn.execute("SELECT size, mtime FROM sources WHERE path = ?", (str(rel),)).fetchone()
        if known and known["size"] == st.st_size and known["mtime"] == st.st_mtime:
            stats.unchanged += 1
            continue

        ext = path.suffix.lower()
        if ext in VIDEO_EXTS:
            _record_source(conn, rel, st, "skipped", "video (not supported)")
            stats.skipped += 1
        elif ext not in IMAGE_EXTS:
            _record_source(conn, rel, st, "skipped", f"unsupported file type '{ext or '(none)'}'")
            stats.skipped += 1
        else:
            try:
                sha = sha256_file(path)
                if conn.execute("SELECT 1 FROM photos WHERE sha256 = ?", (sha,)).fetchone():
                    stats.duplicates += 1
                else:
                    a = analyze(path, detector)
                    taken, source = _taken_at(path, rel, a.exif_datetime, st.st_mtime)
                    conn.execute(
                        """INSERT INTO photos (sha256, ext, taken_at, tz_offset, date_source, camera, width, height,
                               is_screenshot, phash, sharpness, exposure, faces, face_sharpness)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (sha, normalize_ext(ext), taken.isoformat(), a.exif_offset, source, a.camera, a.width,
                         a.height, int(_is_screenshot(path, a.camera)), a.phash, a.sharpness, a.exposure,
                         a.faces, a.face_sharpness),
                    )
                    stats.new_photos += 1
                _record_source(conn, rel, st, "image", sha=sha)
            except Exception as e:  # corrupt or truncated file: record it, keep going
                _record_source(conn, rel, st, "error", f"{type(e).__name__}: {e}")
                stats.errors += 1

        if n % 100 == 0:
            conn.commit()
            log(f"  …{n} files scanned")

    conn.commit()
    stats.redated = redate_from_folders(conn)
    return stats

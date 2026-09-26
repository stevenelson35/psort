"""Stage 1: scan the inbox (read-only) and record every file (DESIGN.md §5.1)."""

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import dates
from .config import Config
from . import rich
from . import videos as videos_mod
from .imaging import (
    IMAGE_EXTS,
    RICH_EXTS,
    SIDECAR_EXTS,
    VIDEO_EXTS,
    FaceDetector,
    analyze,
    is_junk,
    normalize_ext,
    sha256_file,
)


@dataclass
class IngestStats:
    new_photos: int = 0
    duplicates: int = 0
    unchanged: int = 0
    errors: int = 0  # unreadable photos/videos (still copied to unsorted_files)
    redated: int = 0  # earlier undated photos now dated from folder names
    new_videos: int = 0
    live_clips: int = 0  # iPhone Live Photo clips, kept beside their photo
    others: int = 0  # everything else, kept in unsorted_files
    junk: int = 0  # OS caches like Thumbs.db: not copied
    deleted: int = 0  # photos you deleted in psort, seen again in the inbox
    arriving: int = 0  # still being copied in: left for the next run
    replaced: int = 0  # changed since last seen (e.g. a partial copy finished): old copies cleaned up
    recovered: int = 0  # unreadable before, read fine now
    rich_packages: int = 0  # Nokia Rich Capture .nar packages
    rich_frames: int = 0  # photos unpacked from them


def batch_of(rel: Path) -> str:
    return rel.parts[0] if len(rel.parts) > 1 else "(inbox root)"


def inbox_files(inbox: Path, under: Path | None = None):
    """Yield (absolute, relative-to-inbox) for every file, in a stable order."""
    for path in sorted((under or inbox).rglob("*")):
        if path.is_file():
            yield path, path.relative_to(inbox)


def _taken_at(path: Path, rel: Path, exif_value: object, mtime: float) -> tuple[datetime, str]:
    if dt := dates.parse_exif_datetime(exif_value):
        return dt, "exif"
    if dt := dates.from_filename(path.name):
        return dt, "filename"
    if found := dates.from_folders(str(rel)):
        return found
    return datetime.fromtimestamp(mtime).replace(microsecond=0), "mtime"


def thm_date(video: Path) -> datetime | None:
    """Older cameras save a small JPEG (.THM) beside each video, with the date in its EXIF."""
    for ext in (".THM", ".thm"):
        thm = video.with_suffix(ext)
        if thm.exists():
            try:
                with Image.open(thm) as im:
                    exif = im.getexif()
                    return dates.parse_exif_datetime(exif.get_ifd(0x8769).get(36867) or exif.get(306))
            except OSError:
                return None
    return None


def redate_videos_from_thm(cfg: Config, conn: sqlite3.Connection) -> int:
    """Videos dated only roughly (folder or file time) whose .THM companion has the real date."""
    changed = 0
    rows = conn.execute(
        f"""SELECT v.sha256, (SELECT MIN(s.path) FROM sources s WHERE s.sha256 = v.sha256) AS path
            FROM videos v WHERE v.date_source IN {dates.sql_in(dates.NO_TIME)}"""
    ).fetchall()
    for r in rows:
        if r["path"] and (dt := thm_date(cfg.inbox / r["path"])):
            conn.execute("UPDATE videos SET taken_at = ?, date_source = 'meta', name = NULL WHERE sha256 = ?",
                         (dt.isoformat(), r["sha256"]))
            changed += 1
    conn.commit()
    return changed


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


def kind_of(path: Path) -> str:
    if is_junk(path):
        return "junk"
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in RICH_EXTS:
        return "rich"
    if ext in SIDECAR_EXTS:
        return "sidecar"
    return "other"


def _unchanged(known, st, path: Path) -> bool:
    """Already recorded and nothing to redo for this file."""
    return bool(
        known and known["size"] == st.st_size and known["mtime"] == st.st_mtime
        and (known["sha256"] is not None or known["status"] == "junk")
        and known["status"] != "error"  # unreadable last time: try again (psort may have improved)
        and not (known["status"] == "other" and kind_of(path) == "rich")  # handled as a package now
    )


def plan(cfg: Config, conn: sqlite3.Connection) -> tuple[list[tuple[Path, Path]], int]:
    """(every inbox file, how many need processing), for progress estimates."""
    files = list(inbox_files(cfg.inbox))
    known = {r["path"]: r for r in conn.execute("SELECT path, size, mtime, status, sha256 FROM sources")}
    pending = sum(1 for path, rel in files if not _unchanged(known.get(str(rel)), path.stat(), path))
    return files, pending


def ingest(cfg: Config, conn: sqlite3.Connection, log: Callable[[str], None] = print,
           progress: Callable[[int, int], None] | None = None,
           files: list[tuple[Path, Path]] | None = None) -> IngestStats:
    """Record every inbox file. Every file except OS caches ends up copied somewhere: photos to
    the library, videos to videos/, Live Photo clips beside their photo, the rest to unsorted_files/."""
    if not cfg.inbox.is_dir():
        raise FileNotFoundError(f"Inbox not found: {cfg.inbox}")
    detector = FaceDetector.load(cfg.face_model)
    stats = IngestStats()
    siblings: dict[Path, dict[str, str]] = {}  # folder → {lowercase name: real name}, for Live Photo pairing

    files = files if files is not None else list(inbox_files(cfg.inbox))
    for n, (path, rel) in enumerate(files, start=1):
        if progress:
            progress(n - 1, len(files))
        st = path.stat()
        known = conn.execute("SELECT size, mtime, status, sha256 FROM sources WHERE path = ?", (str(rel),)).fetchone()
        # Unchanged files are skipped, except ones recorded without a copy by an earlier version.
        if _unchanged(known, st, path):
            stats.unchanged += 1
            continue

        # Still being copied in? Windows keeps a file's change time current while it's written (its
        # size is often reserved up front), so a recently touched file is left for the next run.
        if time.time() - max(st.st_mtime, st.st_ctime) < cfg.settle_seconds:
            stats.arriving += 1
            continue

        kind = kind_of(path)
        ext = path.suffix.lower()
        old_sha = known["sha256"] if known else None
        try:
            if kind == "junk":
                _record_source(conn, rel, st, "junk", "OS cache file (rebuilt automatically; not needed)")
                stats.junk += 1
            elif kind == "image":
                _ingest_photo(cfg, conn, path, rel, st, ext, detector, stats)
            elif kind == "video":
                if path.parent not in siblings:
                    siblings[path.parent] = {p.name.lower(): p.name for p in path.parent.iterdir()}
                _ingest_video(conn, path, rel, st, ext, siblings[path.parent], stats)
            elif kind == "rich" and rich.is_package(path):
                if path.parent not in siblings:
                    siblings[path.parent] = {p.name.lower(): p.name for p in path.parent.iterdir()}
                _ingest_rich(cfg, conn, path, rel, st, siblings[path.parent], detector, stats)
            elif kind == "sidecar":
                _ingest_other(conn, path, rel, st, "sidecar", SIDECAR_EXTS[ext])
                stats.others += 1
            else:
                _ingest_other(conn, path, rel, st, "other", f"not a photo or video ('{ext or 'no extension'}')")
                stats.others += 1
        except StillArriving:
            stats.arriving += 1
            continue
        except Exception as e:  # corrupt or truncated file: keep its bytes in unsorted_files anyway
            reason = f"unreadable: {type(e).__name__}: {e}"
            try:
                _ingest_other(conn, path, rel, st, "error", reason)
            except StillArriving:  # "unreadable" only because it's half-copied
                stats.arriving += 1
                continue
            except OSError:
                _record_source(conn, rel, st, "error", reason)  # can't even read it; verify will flag it
            stats.errors += 1

        new = conn.execute("SELECT sha256, status FROM sources WHERE path = ?", (str(rel),)).fetchone()
        if old_sha and new and new["sha256"] != old_sha and forget_content(cfg, conn, old_sha):
            stats.replaced += 1
        if known and known["status"] == "error" and new and new["status"] != "error":
            stats.recovered += 1
            _drop_unsorted_copy(cfg, conn, new["sha256"])
        elif known and known["status"] == "other" and new and new["status"] == "rich":
            _drop_unsorted_copy(cfg, conn, new["sha256"])  # was filed as "other" before packages were understood

        if n % 100 == 0:
            conn.commit()
            if not progress:
                log(f"  …{n} files scanned")

    conn.commit()
    stats.redated = redate_from_folders(conn)
    redate_videos_from_thm(cfg, conn)
    return stats


class StillArriving(Exception):
    """The file changed while psort was reading it."""


def _steady(path: Path, st) -> None:
    now = path.stat()
    if (now.st_size, now.st_mtime) != (st.st_size, st.st_mtime):
        raise StillArriving


def forget_content(cfg: Config, conn: sqlite3.Connection, sha: str) -> bool:
    """Remove psort's copies of content no inbox file has any more (the partial version of a file
    that has since finished copying). Only ever touches files psort made."""
    if conn.execute("SELECT 1 FROM sources WHERE sha256 = ?", (sha,)).fetchone():
        return False  # still in the inbox somewhere
    removed = False
    # Frames unpacked from a package that's gone go with it.
    for f in conn.execute("SELECT sha256 FROM derived_frames WHERE package_sha = ?", (sha,)).fetchall():
        other = conn.execute("""SELECT 1 FROM derived_frames d JOIN sources s ON s.sha256 = d.package_sha
                                WHERE d.sha256 = ? AND d.package_sha != ?""", (f["sha256"], sha)).fetchone()
        if not other:
            forget_content(cfg, conn, f["sha256"])
            conn.execute("DELETE FROM derived_frames WHERE sha256 = ?", (f["sha256"],))
    for table, root in (("photos", cfg.library), ("videos", cfg.videos), ("other_files", cfg.unsorted),
                        ("live_clips", cfg.library), ("rich_packages", cfg.library)):
        row = conn.execute(f"SELECT library_path FROM {table} WHERE sha256 = ?", (sha,)).fetchone()
        if row is None:
            continue
        if row["library_path"] and (root / row["library_path"]).exists():
            (root / row["library_path"]).unlink()
            _remove_empty(root / row["library_path"], root)
        conn.execute(f"DELETE FROM {table} WHERE sha256 = ?", (sha,))
        removed = True
    if removed:
        for table in ("faces", "favorites", "tray", "tags"):
            conn.execute(f"DELETE FROM {table} WHERE sha256 = ?", (sha,))
        conn.commit()
    return removed


def _drop_unsorted_copy(cfg: Config, conn: sqlite3.Connection, sha: str | None) -> None:
    """A file that was kept in unsorted_files only because it couldn't be read, and now can."""
    if not sha or conn.execute("SELECT 1 FROM sources WHERE sha256 = ? AND status IN ('other', 'sidecar', 'error')",
                               (sha,)).fetchone():
        return
    row = conn.execute("SELECT library_path FROM other_files WHERE sha256 = ?", (sha,)).fetchone()
    if row:
        if row["library_path"] and (cfg.unsorted / row["library_path"]).exists():
            (cfg.unsorted / row["library_path"]).unlink()
            _remove_empty(cfg.unsorted / row["library_path"], cfg.unsorted)
        conn.execute("DELETE FROM other_files WHERE sha256 = ?", (sha,))
        conn.commit()


def _remove_empty(path: Path, root: Path) -> None:
    parent = path.parent
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _ingest_photo(cfg, conn, path, rel, st, ext, detector, stats) -> None:
    sha = sha256_file(path)
    if conn.execute("SELECT 1 FROM photos WHERE sha256 = ?", (sha,)).fetchone():
        stats.duplicates += 1
    elif conn.execute("SELECT 1 FROM deleted_photos WHERE sha256 = ?", (sha,)).fetchone():
        stats.deleted += 1  # you deleted it: never copied back
    else:
        a = analyze(path, detector)
        _steady(path, st)  # don't record a file that changed while we read it
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


def _ingest_video(conn, path, rel, st, ext, sibling_names: dict[str, str], stats) -> None:
    info = videos_mod.probe(path)
    sha = sha256_file(path)
    _steady(path, st)
    if videos_mod.is_live_photo_clip(path, set(sibling_names), info.duration):
        photo_name = next(sibling_names[f"{path.stem.lower()}{e}"] for e in (".heic", ".heif", ".jpg", ".jpeg")
                          if f"{path.stem.lower()}{e}" in sibling_names)
        conn.execute("INSERT OR IGNORE INTO live_clips (sha256, ext, photo_path) VALUES (?, ?, ?)",
                     (sha, ext, str(rel.parent / photo_name)))
        _record_source(conn, rel, st, "livephoto", "Live Photo clip (kept beside its photo)", sha)
        stats.live_clips += 1
        return
    if conn.execute("SELECT 1 FROM videos WHERE sha256 = ?", (sha,)).fetchone():
        stats.duplicates += 1
    else:
        if created := info.created or thm_date(path):
            taken, source = created, "meta"
        else:
            taken, source = _taken_at(path, rel, None, st.st_mtime)
        conn.execute(
            "INSERT INTO videos (sha256, ext, taken_at, date_source, duration, width, height) VALUES (?,?,?,?,?,?,?)",
            (sha, ext, taken.replace(microsecond=0).isoformat(), source, info.duration, info.width, info.height),
        )
        stats.new_videos += 1
    _record_source(conn, rel, st, "video", sha=sha)


def _ingest_rich(cfg, conn, path, rel, st, sibling_names: dict[str, str], detector, stats) -> None:
    """A Rich Capture package: kept beside its finished photo; each frame becomes a photo too."""
    sha = sha256_file(path)
    _steady(path, st)
    photo_name = next((sibling_names[f"{path.stem.lower()}{e}"] for e in (".jpg", ".jpeg")
                       if f"{path.stem.lower()}{e}" in sibling_names), None)
    conn.execute("INSERT OR IGNORE INTO rich_packages (sha256, photo_path) VALUES (?, ?)",
                 (sha, str(rel.parent / photo_name) if photo_name else None))
    for member, label, data in rich.frames(path):
        frame_sha = rich.sha256_bytes(data)
        conn.execute("INSERT OR IGNORE INTO derived_frames (sha256, package_sha, member, label) VALUES (?,?,?,?)",
                     (frame_sha, sha, member, label))
        if conn.execute("SELECT 1 FROM photos WHERE sha256 = ? UNION SELECT 1 FROM deleted_photos WHERE sha256 = ?",
                        (frame_sha, frame_sha)).fetchone():
            continue
        tmp = rich.with_frame_file(data, cfg, f"analyze-{frame_sha}.jpg")
        try:
            a = analyze(tmp, detector)
        finally:
            tmp.unlink(missing_ok=True)
        # Frames carry their own EXIF date; failing that, the package's name/folder/file time.
        taken, source = _taken_at(path, rel, a.exif_datetime, st.st_mtime)
        conn.execute(
            """INSERT INTO photos (sha256, ext, taken_at, tz_offset, date_source, camera, width, height,
                   is_screenshot, phash, sharpness, exposure, faces, face_sharpness)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (frame_sha, ".jpg", taken.isoformat(), a.exif_offset, source, a.camera, a.width, a.height, 0,
             a.phash, a.sharpness, a.exposure, a.faces, a.face_sharpness),
        )
        stats.rich_frames += 1
    _record_source(conn, rel, st, "rich", "Rich Capture package (kept beside its photo; frames added)", sha)
    stats.rich_packages += 1


def _ingest_other(conn, path, rel, st, status: str, reason: str) -> None:
    sha = sha256_file(path)
    _steady(path, st)
    conn.execute("INSERT OR IGNORE INTO other_files (sha256) VALUES (?)", (sha,))
    _record_source(conn, rel, st, status, reason, sha)

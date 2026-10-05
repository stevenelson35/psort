"""Recover unreadable images by opening whatever part can be read and saving it again as a new JPEG
(DESIGN.md §5.13). The damaged original is never changed or removed: it stays in the inbox and in
unsorted_files/. The re-saved copy is a separate photo; psort keeps it in the state directory
(`recovered/`) until the library copy is made."""

import hashlib
import io
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFile

from .config import Config, source_file
from .imaging import IMAGE_EXTS, FaceDetector, analyze, to_rgb
from .ingest import _taken_at, add_photo

QUALITY = 95  # re-encoding is lossy; keep the extra loss small
MAX_BYTES = 512 * 1024 * 1024  # bigger than any photo: not worth (or safe) loading into memory


@dataclass
class Candidate:
    source_path: str  # sources.path of the damaged original
    file: Path  # a readable copy of its bytes: the inbox file, else the one in unsorted_files/
    reason: str


@dataclass
class Outcome:
    candidate: Candidate
    sha: str | None  # the recovered photo, or None when nothing could be read
    new: bool  # added to the database now (False: it was already there, or recovery failed)
    note: str


def recovered_file(cfg: Config, sha: str) -> Path:
    return cfg.state_dir / "recovered" / f"{sha}.jpg"


def candidates(cfg: Config, conn: sqlite3.Connection, retry: bool = False) -> list[Candidate]:
    """Unreadable images not tried before (with `retry`, also the ones that failed before)."""
    found = []
    rows = conn.execute(
        """SELECT s.path, s.reason, o.library_path AS filed FROM sources s
           LEFT JOIN other_files o ON o.sha256 = s.sha256 WHERE s.status = 'error' ORDER BY s.path"""
    ).fetchall()
    for r in rows:
        if Path(r["path"]).suffix.lower() not in IMAGE_EXTS:
            continue
        tried = conn.execute("SELECT sha256 FROM recoveries WHERE source_path = ?", (r["path"],)).fetchone()
        if tried and not (retry and tried["sha256"] is None):
            continue
        copies = [source_file(cfg, r["path"])] + ([cfg.unsorted / r["filed"]] if r["filed"] else [])
        file = next((c for c in copies if c.is_file()), None)
        if file:
            found.append(Candidate(r["path"], file, r["reason"] or ""))
    return found


def _decode(path: Path) -> tuple[Image.Image, bytes, bytes | None] | None:
    """The picture (as much as there is), its EXIF and its color profile."""
    keep = ImageFile.LOAD_TRUNCATED_IMAGES
    ImageFile.LOAD_TRUNCATED_IMAGES = True  # decode what's there instead of giving up at the break
    try:
        with Image.open(path) as im:
            im.load()
            try:
                exif = im.getexif().tobytes()
            except Exception:  # odd metadata that can't be re-encoded: keep the pixels
                exif = b""
            return to_rgb(im), exif, im.info.get("icc_profile")
    except Exception:
        pass
    finally:
        ImageFile.LOAD_TRUNCATED_IMAGES = keep
    try:  # a different decoder sometimes copes with what Pillow can't
        data = np.fromfile(str(path), dtype=np.uint8)
        bgr = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    except Exception:
        bgr = None
    if bgr is None:
        return None
    return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)), b"", None


def _usable(im: Image.Image) -> bool:
    return min(im.size) >= 16 and float(np.asarray(im.resize((64, 64))).std()) >= 2  # not blank


def _record(conn: sqlite3.Connection, source_path: str, sha: str | None, note: str) -> None:
    conn.execute("INSERT OR REPLACE INTO recoveries (source_path, sha256, note) VALUES (?,?,?)",
                 (source_path, sha, note))
    conn.commit()


def recover_one(cfg: Config, conn: sqlite3.Connection, cand: Candidate,
                detector: FaceDetector | None = None) -> Outcome:
    size = cand.file.stat().st_size
    decoded = _decode(cand.file) if size <= MAX_BYTES else None
    if decoded is None or not _usable(decoded[0]):
        note = f"too large to be a photo ({size / 1e9:.1f} GB)" if size > MAX_BYTES else "nothing readable"
        _record(conn, cand.source_path, None, note)
        return Outcome(cand, None, False, note)
    img, exif, icc = decoded
    buf = io.BytesIO()
    extra = {"exif": exif} if exif else {}
    if icc:
        extra["icc_profile"] = icc
    img.save(buf, "JPEG", quality=QUALITY, optimize=True, **extra)
    data = buf.getvalue()
    sha = hashlib.sha256(data).hexdigest()

    out = recovered_file(cfg, sha)
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    partial.write_bytes(data)
    os.replace(partial, out)

    known = conn.execute("SELECT 1 FROM photos WHERE sha256 = ? UNION SELECT 1 FROM deleted_photos WHERE sha256 = ?",
                         (sha, sha)).fetchone()
    if not known:
        a = analyze(out, detector)
        taken, source = _taken_at(cand.file, Path(cand.source_path), a.exif_datetime, cand.file.stat().st_mtime)
        add_photo(conn, sha, ".jpg", a, taken, source, False)
    note = "re-saved; any damaged part of the picture may be missing or grey"
    _record(conn, cand.source_path, sha, note)
    return Outcome(cand, sha, not known, note)


def recover_all(cfg: Config, conn: sqlite3.Connection, found: list[Candidate],
                log: Callable[[str], None] = print) -> list[Outcome]:
    detector = FaceDetector.load(cfg.face_model)
    outcomes = []
    for cand in found:
        try:
            outcome = recover_one(cfg, conn, cand, detector)
        except Exception as e:  # one bad file mustn't stop the rest
            note = f"failed: {type(e).__name__}: {e}"
            _record(conn, cand.source_path, None, note)
            outcome = Outcome(cand, None, False, note)
        log(f"  {'recovered' if outcome.sha else 'could not recover'}: {cand.source_path} — {outcome.note}")
        outcomes.append(outcome)
    conn.commit()
    return outcomes

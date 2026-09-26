"""Nokia Lumia "Rich Capture" packages (DESIGN.md §5.12).

A Lumia saved WP_…_Rich.jpg (the finished photo) plus WP_…_Rich.nar: a ZIP of the frames it was
blended from (e.g. flash, no flash, a flash/no-flash blend) and two small XML files. psort keeps the
package beside its finished photo in the library, and adds each frame as a photo of its own so it
competes in the same moment; the best shot wins, the rest become alternates or duplicates.
"""

import hashlib
import re
import sqlite3
import zipfile
from pathlib import Path

from .config import Config

_LABELS = {"noflash": "no flash", "flash": "flash", "flashnoflash": "flash + no-flash blend"}


def is_package(path: Path) -> bool:
    return zipfile.is_zipfile(path)


def frames(path: Path) -> list[tuple[str, str | None, bytes]]:
    """(member name, label, JPEG bytes) for every frame in the package."""
    with zipfile.ZipFile(path) as z:
        labels = {}
        if "content.xml" in z.namelist():
            xml = z.read("content.xml").decode("utf-8", errors="replace")
            for props, name in re.findall(r'<image[^>]*properties="([^"]*)"[^>]*>([^<]+)</image>', xml):
                labels[name.strip()] = _LABELS.get(props.lower(), props)
        return [(n, labels.get(n), z.read(n)) for n in z.namelist() if n.lower().endswith((".jpg", ".jpeg"))]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def extract_frame(cfg: Config, conn: sqlite3.Connection, frame_sha: str) -> Path | None:
    """Write a frame out of its package (still in the inbox) to a temporary file, for copying."""
    row = conn.execute(
        """SELECT d.member, (SELECT MIN(s.path) FROM sources s WHERE s.sha256 = d.package_sha) AS package
           FROM derived_frames d WHERE d.sha256 = ?""", (frame_sha,)
    ).fetchone()
    if row is None or row["package"] is None or not (cfg.inbox / row["package"]).exists():
        return None
    with zipfile.ZipFile(cfg.inbox / row["package"]) as z:
        data = z.read(row["member"])
    if sha256_bytes(data) != frame_sha:
        return None
    out = cfg.state_dir / "tmp" / f"{frame_sha}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def with_frame_file(data: bytes, cfg: Config, name: str) -> Path:
    """A temporary file holding a frame, for analysis."""
    out = cfg.state_dir / "tmp" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def package_photo_sha(conn: sqlite3.Connection, package_sha: str) -> str | None:
    """The photo a package sits beside: its finished _Rich.jpg, or (if that's missing) its flash
    frame, so the package still has a home."""
    row = conn.execute(
        "SELECT s.sha256 FROM rich_packages r JOIN sources s ON s.path = r.photo_path WHERE r.sha256 = ?",
        (package_sha,),
    ).fetchone()
    if row and conn.execute("SELECT 1 FROM photos WHERE sha256 = ?", (row["sha256"],)).fetchone():
        return row["sha256"]
    frames_ = conn.execute(
        """SELECT d.sha256 FROM derived_frames d JOIN photos p ON p.sha256 = d.sha256
           WHERE d.package_sha = ? ORDER BY lower(d.member) != 'reference.jpg', d.member""", (package_sha,)
    ).fetchall()
    return frames_[0]["sha256"] if frames_ else None


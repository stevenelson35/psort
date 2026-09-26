"""Export photos for a blog post (DESIGN.md §7): upright, ≤2048px JPEGs with location data
stripped, written to <outbox>/<post>/ under their library names."""

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from .config import Config
from .events import slugify

MAX_EDGE = 2048  # the blog's largest responsive size
QUALITY = 85

# EXIF kept in exports. Everything else (GPS, serial numbers, lens/owner info, maker notes,
# thumbnails, XMP, comments) is dropped. Orientation is baked into the pixels instead.
_KEEP_IFD0 = {271: "Make", 272: "Model", 306: "DateTime"}
_KEEP_EXIF = {36867: "DateTimeOriginal", 36881: "OffsetTimeOriginal"}
_EXIF_IFD = 0x8769
_IMAGE_DESCRIPTION = 270  # Windows: Title / Subject
_XP_KEYWORDS = 0x9C9E  # Windows: Tags


class ExportError(Exception):
    pass


@dataclass
class ExportResult:
    folder: Path
    files: list[Path]


def _clean_exif(src: Image.Exif) -> Image.Exif:
    out = Image.Exif()
    for tag in _KEEP_IFD0:
        if tag in src:
            out[tag] = src[tag]
    src_ifd = src.get_ifd(_EXIF_IFD)
    kept = {tag: src_ifd[tag] for tag in _KEEP_EXIF if tag in src_ifd}
    if kept:
        out.get_ifd(_EXIF_IFD).update(kept)
    return out


def render(src: Path, dest: Path, description: str | None = None, keywords: list[str] | None = None) -> None:
    """Write one export: EXIF-rotated, flattened onto white if transparent, resized, re-encoded.
    `description` and `keywords` show in Windows as the file's Title/Subject and Tags."""
    with Image.open(src) as im:
        exif = _clean_exif(im.getexif())
        if description:
            exif[_IMAGE_DESCRIPTION] = description
        if keywords:
            exif[_XP_KEYWORDS] = ";".join(keywords).encode("utf-16-le") + b"\0\0"
        icc = im.info.get("icc_profile")  # keeps colors right (e.g. iPhone Display P3)
        im.draft("RGB", (MAX_EDGE, MAX_EDGE))
        img = ImageOps.exif_transpose(im)
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, "white")
        img.paste(rgba, mask=rgba.getchannel("A"))
    img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE), Image.LANCZOS)  # only ever shrinks
    img.info = {}  # otherwise Pillow carries the original's JPEG comment across
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    img.save(partial, "JPEG", quality=QUALITY, optimize=True, progressive=True,
             exif=exif.tobytes(), **({"icc_profile": icc} if icc else {}))
    os.replace(partial, dest)


def export(cfg: Config, conn: sqlite3.Connection, post: str, names: list[str] | None = None,
           keep_tray: bool = False) -> ExportResult:
    """Export the post tray (or the named library photos) to <outbox>/<post-slug>/.
    Photos exported from the tray leave the tray unless keep_tray."""
    slug = slugify(post)
    if names:
        marks = ",".join("?" * len(names))
        rows = conn.execute(f"SELECT sha256, name, library_path FROM photos WHERE name IN ({marks})", names).fetchall()
        if missing := set(names) - {r["name"] for r in rows}:
            raise ExportError(f"No photo(s) named {', '.join(sorted(missing))}")
    else:
        rows = conn.execute(
            "SELECT p.sha256, p.name, p.library_path FROM photos p JOIN tray t ON t.sha256 = p.sha256 "
            "ORDER BY p.taken_at, p.name"
        ).fetchall()
        if not rows:
            raise ExportError("The post tray is empty. Add photos to it in the review UI first.")

    folder = cfg.outbox / slug
    files = []
    for r in rows:
        src = cfg.library / (r["library_path"] or "")
        if not r["library_path"] or not src.exists():
            raise ExportError(f"{r['name']} isn't in the library (run `psort run`)")
        dest = folder / f"{r['name']}.jpg"
        render(src, dest)
        files.append(dest)
        conn.execute("INSERT OR REPLACE INTO exports (sha256, post) VALUES (?, ?)", (r["sha256"], slug))
    if not names and not keep_tray:
        conn.executemany("DELETE FROM tray WHERE sha256 = ?", [(r["sha256"],) for r in rows])
    conn.commit()
    return ExportResult(folder, files)

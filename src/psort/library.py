"""Stage 4: build the library from the inbox, plus verify and the manifest (DESIGN.md §5.4–5.5)."""

import json
import os
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .dates import NO_TIME
from .events import named_ranges, slug_for
from .imaging import sha256_file
from .ingest import inbox_files

UNDATED_DIR = "_undated"
ALTERNATES_DIR = "_alternates"
DUPLICATES_DIR = "_duplicates"
UNKNOWN_DAY = "unknown-day"


@dataclass
class CurateStats:
    copied: int = 0
    moved: int = 0
    unchanged: int = 0
    missing: list[str] = field(default_factory=list)  # names with no library copy and no inbox source
    videos_copied: int = 0
    videos_moved: int = 0
    clips_copied: int = 0
    others_copied: int = 0


def assign_names(conn: sqlite3.Connection) -> None:
    """Give each new photo a permanent library name, YYYYMMDD_HHMMSS[_n]. Names never change once
    assigned, so a name used on the blog keeps pointing at the same photo."""
    taken = {r["name"] for r in conn.execute("SELECT name FROM photos WHERE name IS NOT NULL")}
    # Same-second photos are numbered in original-filename order, which follows the burst sequence.
    rows = conn.execute(
        """SELECT p.sha256, p.taken_at,
                  (SELECT MIN(s.path) FROM sources s WHERE s.sha256 = p.sha256) AS first_path
           FROM photos p WHERE p.name IS NULL ORDER BY p.taken_at, first_path, p.sha256"""
    ).fetchall()
    for row in rows:
        stem = datetime.fromisoformat(row["taken_at"]).strftime("%Y%m%d_%H%M%S")
        name, n = stem, 0
        while name in taken:
            n += 1
            name = f"{stem}_{n}"
        taken.add(name)
        conn.execute("UPDATE photos SET name = ? WHERE sha256 = ?", (name, row["sha256"]))
    conn.commit()


def desired_paths(conn: sqlite3.Connection) -> dict[str, str]:
    """sha256 → library-relative path, from the current moments and best picks."""
    rows = conn.execute(
        "SELECT sha256, name, ext, taken_at, date_source, moment_id, is_best, duplicate_of FROM photos"
    ).fetchall()
    names = {r["sha256"]: r["name"] for r in rows}
    best = {r["moment_id"]: r for r in rows if r["is_best"]}
    ranges = named_ranges(conn)
    paths = {}
    for r in rows:
        if r["date_source"] == "mtime":
            paths[r["sha256"]] = f"{UNDATED_DIR}/{r['name']}{r['ext']}"
            continue
        if r["date_source"] == "folder-month":
            month = r["taken_at"][:7]  # the day is unknown, so it waits in a per-month folder
            paths[r["sha256"]] = f"{month[:4]}/{month}_{UNKNOWN_DAY}/{r['name']}{r['ext']}"
            continue
        b = best[r["moment_id"]]
        day = b["taken_at"][:10]  # alternates live with their best shot, even across midnight
        # Without a real time we can't tell whether it was during a named event.
        slug = None if b["date_source"] in NO_TIME else slug_for(b["taken_at"], ranges)
        folder = f"{day[:4]}/{day}_{slug}" if slug else f"{day[:4]}/{day}"
        if r["is_best"]:
            paths[r["sha256"]] = f"{folder}/{r['name']}{r['ext']}"
        elif r["duplicate_of"]:
            # Grouped under the copy that was kept, e.g. _duplicates/20260703_145634/…
            paths[r["sha256"]] = f"{folder}/{DUPLICATES_DIR}/{names[r['duplicate_of']]}/{r['name']}{r['ext']}"
        else:
            paths[r["sha256"]] = f"{folder}/{ALTERNATES_DIR}/{b['name']}/{r['name']}{r['ext']}"
    return paths


def _remove_empty_parents(path: Path, root: Path) -> None:
    parent = path.parent
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def _copy_verified(src: Path, dest: Path, sha: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    shutil.copy2(src, partial)
    if sha256_file(partial) != sha:
        partial.unlink()
        raise OSError(f"Copy of {src} did not verify")
    os.replace(partial, dest)


def curate(
    cfg: Config, conn: sqlite3.Connection, dry_run: bool = False, log: Callable[[str], None] = print,
    progress: Callable[[int, int], None] | None = None,
) -> CurateStats:
    assign_names(conn)
    stats = CurateStats()
    lib = cfg.library
    current = {r["sha256"]: r for r in conn.execute("SELECT sha256, name, library_path FROM photos")}

    desired = sorted(desired_paths(conn).items(), key=lambda kv: kv[1])
    for i, (sha, target) in enumerate(desired):
        if progress:
            progress(i, len(desired))
        row = current[sha]
        dest = lib / target
        existing = lib / row["library_path"] if row["library_path"] else None

        if row["library_path"] == target and dest.exists():
            stats.unchanged += 1
            continue

        if dest.exists():
            # Something already sits at the target. Adopt it only if it's this exact photo.
            if sha256_file(dest) != sha:
                raise FileExistsError(f"{dest} exists and is a different file; refusing to overwrite")
            action = "adopt"
        elif existing and existing.exists():
            action = "move"
        else:
            action = "copy"

        if action == "move":
            log(f"  move {row['library_path']} → {target}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(existing, dest)
                _remove_empty_parents(existing, lib)
            stats.moved += 1
        elif action == "copy":
            src = _find_source(cfg, conn, sha)
            if src is None:
                stats.missing.append(row["name"])
                continue
            if dry_run:
                log(f"  copy {src.relative_to(cfg.inbox)} → {target}")
            else:
                _copy_verified(src, dest, sha)
            stats.copied += 1

        if not dry_run:
            conn.execute("UPDATE photos SET library_path = ? WHERE sha256 = ?", (target, sha))
            conn.commit()

    # Videos follow the same folder names in their own tree, so an event rename moves both.
    from . import videos as videos_mod

    v = videos_mod.curate(cfg, conn, dry_run=dry_run, log=log)
    stats.videos_copied, stats.videos_moved = v.copied, v.moved
    stats.missing += v.missing or []

    # Live Photo clips follow their photo (same name, video extension), wherever it moves.
    copied, _, missing = _sync(cfg, conn, cfg.library, "live_clips", _clip_paths(conn), dry_run, log, "Live Photo clip")
    stats.clips_copied = copied
    stats.missing += missing
    # Rich Capture packages sit beside their finished photo (same name, .nar).
    copied, _, missing = _sync(cfg, conn, cfg.library, "rich_packages", _rich_paths(conn), dry_run, log,
                               "Rich Capture package")
    stats.clips_copied += copied
    stats.missing += missing
    # Everything else keeps its original inbox path under unsorted_files/.
    copied, _, missing = _sync(cfg, conn, cfg.unsorted, "other_files", _other_paths(conn), dry_run, log, "file")
    stats.others_copied = copied
    stats.missing += missing
    shutil.rmtree(cfg.state_dir / "tmp", ignore_errors=True)  # extracted frames, once copied
    return stats


def _clip_paths(conn: sqlite3.Connection) -> dict[str, str]:
    paths, used = {}, set()
    rows = conn.execute(
        """SELECT c.sha256, c.ext, p.library_path FROM live_clips c
           JOIN sources s ON s.path = c.photo_path JOIN photos p ON p.sha256 = s.sha256
           WHERE p.library_path IS NOT NULL ORDER BY c.sha256"""
    ).fetchall()
    for r in rows:
        base = r["library_path"].rsplit(".", 1)[0]
        target, n = f"{base}{r['ext'].lower()}", 1
        while target in used:  # a second, different clip for the same photo (rare)
            n += 1
            target = f"{base}_live{n}{r['ext'].lower()}"
        used.add(target)
        paths[r["sha256"]] = target
    return paths


def _rich_paths(conn: sqlite3.Connection) -> dict[str, str]:
    from .rich import package_photo_sha

    paths = {}
    for r in conn.execute("SELECT sha256 FROM rich_packages ORDER BY sha256").fetchall():
        photo = package_photo_sha(conn, r["sha256"])
        row = photo and conn.execute("SELECT library_path FROM photos WHERE sha256 = ?", (photo,)).fetchone()
        if row and row["library_path"]:
            paths[r["sha256"]] = row["library_path"].rsplit(".", 1)[0] + ".nar"
    return paths


def _no_spaces(part: str) -> str:
    """'2016-01-03 - Jacksonville Bank Marathon' → '2016-01-03-Jacksonville-Bank-Marathon'."""
    return re.sub(r"-{2,}", "-", re.sub(r"\s*-\s*|\s+", "-", part.strip())) or "_"


def _other_paths(conn: sqlite3.Connection) -> dict[str, str]:
    paths, used = {}, set()
    for r in conn.execute(
        "SELECT o.sha256, (SELECT MIN(s.path) FROM sources s WHERE s.sha256 = o.sha256) AS path "
        "FROM other_files o ORDER BY path"
    ):
        if r["path"] is None:
            continue
        target = "/".join(_no_spaces(p) for p in Path(r["path"]).parts)
        stem, dot, ext = target.rpartition(".") if "." in Path(target).name else (target, "", "")
        n = 1
        while target in used:
            n += 1
            target = f"{stem}_{n}{dot}{ext}"
        used.add(target)
        paths[r["sha256"]] = target
    return paths


def _sync(cfg: Config, conn: sqlite3.Connection, root: Path, table: str, desired: dict[str, str],
          dry_run: bool, log: Callable[[str], None], label: str) -> tuple[int, int, list[str]]:
    """Copy (from the inbox) or move files so each sha256 in `table` sits at its desired path."""
    copied = moved = 0
    missing: list[str] = []
    current = {r["sha256"]: r["library_path"] for r in conn.execute(f"SELECT sha256, library_path FROM {table}")}
    for sha, target in sorted(desired.items(), key=lambda kv: kv[1]):
        dest, cur = root / target, current.get(sha)
        if cur == target and dest.exists():
            continue
        if dest.exists():
            if sha256_file(dest) != sha:
                raise FileExistsError(f"{dest} exists and is a different file; refusing to overwrite")
        elif cur and (root / cur).exists():
            log(f"  move {label} {cur} → {target}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(root / cur, dest)
                _remove_empty_parents(root / cur, root)
            moved += 1
        else:
            src = _find_source(cfg, conn, sha)
            if src is None:
                missing.append(target)
                continue
            log(f"  copy {label} {src.relative_to(cfg.inbox)} → {target}")
            if not dry_run:
                _copy_verified(src, dest, sha)
            copied += 1
        if not dry_run:
            conn.execute(f"UPDATE {table} SET library_path = ? WHERE sha256 = ?", (target, sha))
            conn.commit()
    return copied, moved, missing


def _find_source(cfg: Config, conn: sqlite3.Connection, sha: str) -> Path | None:
    for r in conn.execute("SELECT path FROM sources WHERE sha256 = ? ORDER BY path", (sha,)):
        path = cfg.inbox / r["path"]
        if path.exists():
            return path
    # A frame unpacked from a Rich Capture package: extract it again from the package.
    from .rich import extract_frame

    return extract_frame(cfg, conn, sha)


@dataclass
class VerifyResult:
    path: str
    ok: bool
    message: str


def verify(cfg: Config, conn: sqlite3.Connection, batch: str | None = None) -> list[VerifyResult]:
    """Is every file in an inbox batch (or the whole inbox) safe to delete? (DESIGN.md §5.5)"""
    batch_dir = (cfg.inbox / batch).resolve() if batch else cfg.inbox.resolve()
    if not batch_dir.is_dir() or not batch_dir.is_relative_to(cfg.inbox.resolve()):
        raise FileNotFoundError(f"No batch folder {batch!r} in {cfg.inbox}")

    results = []
    for path, rel in inbox_files(cfg.inbox.resolve(), batch_dir):
        src = conn.execute("SELECT * FROM sources WHERE path = ?", (str(rel),)).fetchone()
        st = path.stat()
        if src is None:
            results.append(VerifyResult(str(rel), False, "not ingested yet (run `psort run`, or it's still arriving)"))
        elif src["size"] != st.st_size or src["mtime"] != st.st_mtime:
            results.append(VerifyResult(str(rel), False, "changed since it was ingested (run `psort run`)"))
        elif src["status"] == "junk":
            results.append(VerifyResult(str(rel), True, src["reason"]))
        elif src["status"] == "rich":
            results.append(_placed(conn, cfg.library, "rich_packages", src["sha256"],
                                   "Rich Capture package beside its photo: ", rel=str(rel)))
        elif src["status"] == "livephoto" and _photo_deleted(conn, str(rel)):
            results.append(VerifyResult(str(rel), True, "Live Photo clip of a photo you deleted"))
        elif src["status"] == "livephoto":
            results.append(_placed(conn, cfg.library, "live_clips", src["sha256"], "Live Photo clip beside its photo: ",
                                   rel=str(rel)))
        elif src["status"] in ("sidecar", "other", "error"):
            results.append(_placed(conn, cfg.unsorted, "other_files", src["sha256"],
                                   f"{src['reason']}; kept in unsorted_files/", rel=str(rel)))
        elif src["status"] == "video":
            video = conn.execute("SELECT library_path FROM videos WHERE sha256 = ?", (src["sha256"],)).fetchone()
            if video and video["library_path"] and (cfg.videos / video["library_path"]).exists():
                results.append(VerifyResult(str(rel), True, f"video: {video['library_path']}"))
            else:
                results.append(VerifyResult(str(rel), False, "video not copied yet (run `psort run`)"))
        elif src["status"] != "image":
            results.append(VerifyResult(str(rel), False, f"not in library: {src['reason']}"))
        elif deleted := conn.execute("SELECT purged FROM deleted_photos WHERE sha256 = ?", (src["sha256"],)).fetchone():
            results.append(VerifyResult(str(rel), True, "you deleted this photo" +
                                        (" (gone for good)" if deleted["purged"] else " (it's in library/_trash)")))
        else:
            photo = conn.execute("SELECT library_path FROM photos WHERE sha256 = ?", (src["sha256"],)).fetchone()
            if photo["library_path"] and (cfg.library / photo["library_path"]).exists():
                results.append(VerifyResult(str(rel), True, photo["library_path"]))
            else:
                results.append(VerifyResult(str(rel), False, "not copied to the library yet (run `psort run`)"))
    return results


def _photo_deleted(conn, clip_rel: str) -> bool:
    return bool(conn.execute(
        """SELECT 1 FROM live_clips c JOIN sources s ON s.path = c.photo_path
           JOIN deleted_photos d ON d.sha256 = s.sha256
           WHERE c.sha256 = (SELECT sha256 FROM sources WHERE path = ?)""", (clip_rel,)).fetchone())


def _placed(conn, root: Path, table: str, sha: str | None, ok_prefix: str, rel: str | None = None) -> "VerifyResult":
    row = sha and conn.execute(f"SELECT library_path FROM {table} WHERE sha256 = ?", (sha,)).fetchone()
    path = rel or ""
    if row and row["library_path"] and (root / row["library_path"]).exists():
        return VerifyResult(path, True, f"{ok_prefix}{row['library_path']}")
    if table == "rich_packages" and sha and conn.execute(
            """SELECT 1 FROM rich_packages r JOIN sources s ON s.path = r.photo_path
               JOIN deleted_photos d ON d.sha256 = s.sha256 WHERE r.sha256 = ?""", (sha,)).fetchone():
        return VerifyResult(path, True, "Rich Capture package of a photo you deleted")
    return VerifyResult(path, False, "not copied yet (run `psort run`)")


def write_manifest(cfg: Config, conn: sqlite3.Connection) -> Path:
    """A JSON copy of the state inside the library, so the library documents itself (DESIGN.md §2)."""
    people = {}
    for r in conn.execute(
        "SELECT DISTINCT f.sha256, p.name FROM faces f JOIN people p ON p.id = f.person_id ORDER BY p.name"
    ):
        people.setdefault(r["sha256"], []).append(r["name"])
    tags = {}
    for r in conn.execute("SELECT sha256, tag FROM tags ORDER BY tag"):
        tags.setdefault(r["sha256"], []).append(r["tag"])
    favorites = {r["sha256"] for r in conn.execute("SELECT sha256 FROM favorites")}
    photos = [
        {**dict(r), "people": people.get(r["sha256"], []), "tags": tags.get(r["sha256"], []),
         "favorite": r["sha256"] in favorites}
        for r in conn.execute("SELECT * FROM photos ORDER BY taken_at, name")
    ]
    events = [dict(r) for r in named_ranges(conn)]
    sources = [dict(r) for r in conn.execute("SELECT path, sha256 FROM sources WHERE status = 'image' ORDER BY path")]
    out = cfg.library / ".psort" / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".partial")
    partial.write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "photos": photos,
                "events": events,
                "videos": [dict(r) for r in conn.execute("SELECT * FROM videos ORDER BY taken_at, name")],
                "live_clips": [dict(r) for r in conn.execute("SELECT * FROM live_clips ORDER BY library_path")],
                "rich_packages": [dict(r) for r in conn.execute("SELECT * FROM rich_packages ORDER BY library_path")],
                "derived_frames": [dict(r) for r in conn.execute("SELECT * FROM derived_frames")],
                "other_files": [dict(r) for r in conn.execute("SELECT * FROM other_files ORDER BY library_path")],
                "deleted_photos": [{k: r[k] for k in ("sha256", "name", "taken_at", "trash_path", "purged", "deleted_at")}
                                   for r in conn.execute("SELECT * FROM deleted_photos ORDER BY deleted_at")],
                "sources": sources,
            },
            indent=1,
        )
    )
    os.replace(partial, out)
    return out

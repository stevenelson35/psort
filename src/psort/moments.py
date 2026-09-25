"""Stages 2 and 3: group near-duplicate bursts into moments, then pick each moment's best shot
(DESIGN.md §5.2–5.3)."""

import sqlite3
from datetime import datetime
from itertools import groupby

import imagehash

from .config import Config


def cluster(cfg: Config, conn: sqlite3.Connection) -> int:
    """Assign moment_id to every photo. Returns the number of moments."""
    rows = conn.execute(
        "SELECT sha256, taken_at, date_source, camera, phash FROM photos ORDER BY camera, taken_at, sha256"
    ).fetchall()

    moments: list[list[sqlite3.Row]] = []
    # Photos dated only by file mtime can share bogus timestamps (bulk copies), so never group them.
    moments.extend([r] for r in rows if r["date_source"] == "mtime")
    dated = [r for r in rows if r["date_source"] != "mtime"]

    hashes = {r["sha256"]: imagehash.hex_to_hash(r["phash"]) for r in dated}
    for _, camera_rows in groupby(dated, key=lambda r: r["camera"] or ""):
        current: list[sqlite3.Row] = []
        for row in camera_rows:
            if current and _joins(cfg, row, current, hashes):
                current.append(row)
            else:
                if current:
                    moments.append(current)
                current = [row]
        if current:
            moments.append(current)

    for members in moments:
        moment_id = members[0]["sha256"]  # earliest photo: stable as later batches arrive
        conn.executemany(
            "UPDATE photos SET moment_id = ? WHERE sha256 = ?", [(moment_id, m["sha256"]) for m in members]
        )
    conn.commit()
    mark_duplicates(cfg, conn)
    return len(moments)


def _is_copy(cfg: Config, a, b, hashes) -> bool:
    """Visual duplicate: the same picture saved twice (re-download, re-compression, resized share),
    as opposed to two frames of a burst. Same camera is implied (moments are per camera)."""
    gap = abs(datetime.fromisoformat(a["taken_at"]) - datetime.fromisoformat(b["taken_at"]))
    if gap.total_seconds() > cfg.duplicate_gap_seconds:
        return False
    if hashes[a["sha256"]] - hashes[b["sha256"]] > cfg.duplicate_phash_threshold:
        return False
    if abs(a["exposure"] - b["exposure"]) > 0.05:
        return False
    if (a["width"], a["height"]) != (b["width"], b["height"]):
        # One camera shoots one size, so a same-shape, different-size twin is a resized copy.
        return abs(a["width"] / a["height"] - b["width"] / b["height"]) < 0.01
    # Same size: a blurry burst frame looks identical to a sharp one at hash level, so require
    # matching sharpness too. Real re-saved copies measure within a few percent.
    return min(a["sharpness"], b["sharpness"]) >= 0.9 * max(a["sharpness"], b["sharpness"])


def mark_duplicates(cfg: Config, conn: sqlite3.Connection) -> int:
    """Within each moment, set duplicate_of on every visual copy except the best-quality one
    (most pixels, then sharpest, then largest file). Returns how many copies were set aside."""
    rows = conn.execute(
        """SELECT p.sha256, p.moment_id, p.taken_at, p.phash, p.width, p.height, p.sharpness, p.exposure,
                  (SELECT MAX(s.size) FROM sources s WHERE s.sha256 = p.sha256) AS size
           FROM photos p WHERE p.date_source != 'mtime' ORDER BY p.moment_id, p.taken_at"""
    ).fetchall()
    hashes = {r["sha256"]: imagehash.hex_to_hash(r["phash"]) for r in rows}
    duplicate_of: dict[str, str | None] = {r["sha256"]: None for r in rows}

    for _, group in groupby(rows, key=lambda r: r["moment_id"]):
        members = list(group)
        if len(members) < 2:
            continue
        # Group copies transitively, then keep the best-quality member of each group.
        parent = {m["sha256"]: m["sha256"] for m in members}

        def find(x):
            while parent[x] != x:
                x = parent[x]
            return x

        for i, a in enumerate(members):
            for b in members[i + 1 :]:
                if _is_copy(cfg, a, b, hashes):
                    parent[find(b["sha256"])] = find(a["sha256"])
        copies: dict[str, list] = {}
        for m in members:
            copies.setdefault(find(m["sha256"]), []).append(m)
        for group_members in copies.values():
            if len(group_members) > 1:
                keeper = max(group_members, key=lambda m: (m["width"] * m["height"], m["sharpness"], m["size"] or 0))
                for m in group_members:
                    if m is not keeper:
                        duplicate_of[m["sha256"]] = keeper["sha256"]

    conn.execute("UPDATE photos SET duplicate_of = NULL WHERE date_source = 'mtime'")
    conn.executemany("UPDATE photos SET duplicate_of = ? WHERE sha256 = ?", [(v, k) for k, v in duplicate_of.items()])
    conn.commit()
    return sum(v is not None for v in duplicate_of.values())


def _joins(cfg: Config, row, current, hashes) -> bool:
    gap = datetime.fromisoformat(row["taken_at"]) - datetime.fromisoformat(current[-1]["taken_at"])
    if gap.total_seconds() > cfg.burst_gap_seconds:
        return False
    h = hashes[row["sha256"]]
    return min(h - hashes[m["sha256"]] for m in current) <= cfg.phash_threshold


def score(cfg: Config, conn: sqlite3.Connection) -> None:
    """Score each photo relative to its moment and mark the best (a user pick always wins)."""
    w = cfg.weights
    rows = conn.execute("SELECT * FROM photos ORDER BY moment_id, taken_at, sha256").fetchall()
    for _, group in groupby(rows, key=lambda r: r["moment_id"]):
        everyone = list(group)
        # Visual duplicates never compete: they're copies of a member that's still here.
        members = [m for m in everyone if not m["duplicate_of"]]
        max_sharp = max(m["sharpness"] for m in members) or 1.0
        max_faces = max(m["faces"] or 0 for m in members)
        max_face_sharp = max(m["face_sharpness"] or 0.0 for m in members)

        scores = {}
        for m in members:
            s = w.sharpness * m["sharpness"] / max_sharp + w.exposure * m["exposure"]
            if max_faces:
                s += w.faces * (m["faces"] or 0) / max_faces
            if max_face_sharp:
                s += w.face_sharpness * (m["face_sharpness"] or 0.0) / max_face_sharp
            scores[m["sha256"]] = s

        # A pick of a copy counts as a pick of the copy it duplicates.
        user_picks = [m["duplicate_of"] or m["sha256"] for m in everyone if m["user_best"]]
        best = user_picks[0] if user_picks else max(members, key=lambda m: scores[m["sha256"]])["sha256"]
        # Close call: the runner-up is nearly as good, so the automatic pick deserves a look.
        # Once you've picked, it's settled.
        ranked = sorted(scores.values(), reverse=True)
        close = not user_picks and len(ranked) > 1 and ranked[0] - ranked[1] <= cfg.close_call_margin * ranked[0]
        conn.executemany(
            "UPDATE photos SET score = ?, is_best = ?, close_call = ? WHERE sha256 = ?",
            [(scores.get(m["sha256"]), int(m["sha256"] == best), int(close and not m["duplicate_of"]), m["sha256"])
             for m in everyone],
        )
    conn.commit()

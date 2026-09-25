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
    return len(moments)


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
        members = list(group)
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

        user_picks = [m["sha256"] for m in members if m["user_best"]]
        best = user_picks[0] if user_picks else max(members, key=lambda m: scores[m["sha256"]])["sha256"]
        conn.executemany(
            "UPDATE photos SET score = ?, is_best = ? WHERE sha256 = ?",
            [(scores[m["sha256"]], int(m["sha256"] == best), m["sha256"]) for m in members],
        )
    conn.commit()

"""Face recognition (DESIGN.md §5.7). Everything stays local; embeddings never leave the database.

1. scan:   find faces in each library photo and store a 128-number embedding per face.
2. assign: faces close to ones you've named get that person automatically ("auto");
           the rest are grouped into unnamed clusters of look-alike faces.
3. label:  you name a cluster (or single faces); `assign` then spreads the name to similar faces.
"""

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import cv2
import numpy as np
from PIL import Image
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from .config import Config
from .events import slugify
from .imaging import FaceDetector, load_small

MIN_FACE_PX = 32  # at analysis size; smaller faces are too blurry to recognize reliably
BLOCK = 1024  # rows per similarity block, to bound memory with many faces
NEIGHBORS = 10  # look-alikes linked per unnamed face when grouping


class Recognizer:
    def __init__(self, cfg: Config):
        self.detector = FaceDetector(cfg.face_model)
        self._sface = cv2.FaceRecognizerSF.create(str(cfg.recognition_model), "")

    @classmethod
    def load(cls, cfg: Config) -> "Recognizer | None":
        return cls(cfg) if cfg.face_model.exists() and cfg.recognition_model.exists() else None

    def embed(self, bgr: np.ndarray, row: np.ndarray) -> np.ndarray:
        aligned = self._sface.alignCrop(bgr, row)
        vec = self._sface.feature(aligned).astype(np.float32).ravel()
        return vec / (np.linalg.norm(vec) or 1.0)


def scan(cfg: Config, conn: sqlite3.Connection, log: Callable[[str], None] = print) -> int | None:
    """Detect and embed faces in library photos not yet scanned. None if the models are missing."""
    rec = Recognizer.load(cfg)
    if rec is None:
        return None
    todo = conn.execute(
        "SELECT sha256, library_path FROM photos WHERE faces_scanned = 0 AND library_path IS NOT NULL"
    ).fetchall()
    for n, photo in enumerate(todo, start=1):
        path = cfg.library / photo["library_path"]
        if path.exists():
            small, _, _, _ = load_small(path)
            bgr = cv2.cvtColor(np.asarray(small), cv2.COLOR_RGB2BGR)
            h, w = bgr.shape[:2]
            for row in rec.detector.detect_rows(bgr):
                x, y, fw, fh = (float(v) for v in row[:4])
                if min(fw, fh) < MIN_FACE_PX:
                    continue
                conn.execute(
                    "INSERT INTO faces (sha256, x, y, w, h, confidence, embedding) VALUES (?,?,?,?,?,?,?)",
                    (photo["sha256"], x / w, y / h, fw / w, fh / h, float(row[14]), rec.embed(bgr, row).tobytes()),
                )
            conn.execute("UPDATE photos SET faces_scanned = 1 WHERE sha256 = ?", (photo["sha256"],))
        if n % 50 == 0:
            conn.commit()
            log(f"  …{n}/{len(todo)} photos scanned for faces")
    conn.commit()
    return len(todo)


def _similarities(a: np.ndarray, b: np.ndarray):
    """Yield (row offset, cosine-similarity block) for a against b, BLOCK rows at a time."""
    for i in range(0, len(a), BLOCK):
        yield i, a[i : i + BLOCK] @ b.T


def assign(cfg: Config, conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, embedding, person_id, label_source FROM faces ORDER BY id").fetchall()
    if not rows:
        return
    ids = np.array([r["id"] for r in rows])
    emb = np.stack([np.frombuffer(r["embedding"], np.float32) for r in rows])
    user = np.array([r["label_source"] == "user" for r in rows])
    user_person = np.array([r["person_id"] if r["label_source"] == "user" else -1 for r in rows])

    person = np.where(user, user_person, -1)
    similarity = np.full(len(rows), np.nan)

    # Auto-label: each face takes the person whose user-named faces it most resembles, if close
    # enough, skipping any person you've said it isn't.
    todo = np.flatnonzero(~user)
    if user.any() and len(todo):
        named = np.flatnonzero(user)
        persons = np.unique(user_person[named])
        column = {int(p): j for j, p in enumerate(persons)}
        row_of = {int(fid): i for i, fid in enumerate(ids[todo])}
        rejected = [(row_of[r["face_id"]], column[r["person_id"]]) for r in conn.execute(
            "SELECT face_id, person_id FROM face_rejections")
            if r["face_id"] in row_of and r["person_id"] in column]
        for off, sims in _similarities(emb[todo], emb[named]):
            # Best similarity to each person = max over that person's named faces.
            per_person = np.stack([sims[:, user_person[named] == p].max(axis=1) for p in persons], axis=1)
            for i, j in rejected:
                if off <= i < off + len(per_person):
                    per_person[i - off, j] = -np.inf
            best = per_person.argmax(axis=1)
            best_sim = per_person[np.arange(len(best)), best]
            chunk = todo[off : off + len(best)]
            hit = best_sim >= cfg.face_match_threshold
            person[chunk[hit]] = persons[best[hit]]
            similarity[chunk[hit]] = best_sim[hit]

    # Group the still-unnamed faces: link each to its nearest look-alikes above the cluster
    # threshold, then take connected components. Nearest-k keeps this fast with many faces.
    unknown = np.flatnonzero(person == -1)
    cluster = np.full(len(rows), -1)
    if len(unknown):
        k = min(NEIGHBORS, len(unknown))
        src, dst = [], []
        for off, sims in _similarities(emb[unknown], emb[unknown]):
            nn = np.argpartition(-sims, k - 1, axis=1)[:, :k]
            keep = np.take_along_axis(sims, nn, axis=1) >= cfg.face_cluster_threshold
            src.append(np.repeat(np.arange(off, off + len(sims)), k)[keep.ravel()])
            dst.append(nn.ravel()[keep.ravel()])
        src, dst = np.concatenate(src), np.concatenate(dst)
        graph = coo_matrix((np.ones(len(src)), (src, dst)), shape=(len(unknown), len(unknown)))
        _, component = connected_components(graph, directed=False)
        # Name each group after its smallest face id, so ids stay put as long as that face does.
        group_id = np.full(component.max() + 1, np.iinfo(np.int64).max)
        np.minimum.at(group_id, component, ids[unknown])
        cluster[unknown] = group_id[component]

    updates = []
    for i, r in enumerate(rows):
        if user[i]:
            updates.append((r["person_id"], "user", None, None, r["id"]))
        elif person[i] != -1:
            updates.append((int(person[i]), "auto", float(similarity[i]), None, r["id"]))
        else:
            updates.append((None, None, None, int(cluster[i]), r["id"]))
    conn.executemany(
        "UPDATE faces SET person_id = ?, label_source = ?, similarity = ?, cluster = ? WHERE id = ?", updates
    )
    conn.commit()


class FaceError(Exception):
    pass


def label(cfg: Config, conn: sqlite3.Connection, name: str, cluster: int | None = None,
          face_ids: list[int] | None = None, rejected_ids: list[int] | None = None) -> int:
    """Name a whole unnamed cluster and/or specific faces. `rejected_ids` are faces you've said are
    NOT this person; they'll never be auto-matched to it. Returns faces labeled."""
    name = name.strip()
    if not name:
        raise FaceError("Name can't be empty")
    ids = list(face_ids or [])
    if cluster is not None:
        ids += [r["id"] for r in conn.execute("SELECT id FROM faces WHERE cluster = ?", (cluster,))]
        if not ids:
            raise FaceError(f"No unnamed group {cluster}. Run `psort faces list`.")
    _check_faces(conn, ids + list(rejected_ids or []))
    conn.execute("INSERT OR IGNORE INTO people (name) VALUES (?)", (name,))
    pid = conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()["id"]
    conn.executemany(
        "UPDATE faces SET person_id = ?, label_source = 'user', cluster = NULL WHERE id = ?", [(pid, i) for i in ids]
    )
    # Naming a face explicitly overrides an earlier "not this person".
    conn.executemany("DELETE FROM face_rejections WHERE face_id = ? AND person_id = ?", [(i, pid) for i in ids])
    conn.executemany(
        "INSERT OR IGNORE INTO face_rejections (face_id, person_id) VALUES (?, ?)",
        [(i, pid) for i in rejected_ids or []],
    )
    conn.commit()
    assign(cfg, conn)
    return len(ids)


def unlabel(cfg: Config, conn: sqlite3.Connection, face_ids: list[int]) -> None:
    """"Not this person": remove the name and never auto-match these faces to that person again."""
    _check_faces(conn, face_ids)
    marks = f"({','.join('?' * len(face_ids))})"
    conn.execute(
        f"""INSERT OR IGNORE INTO face_rejections (face_id, person_id)
            SELECT id, person_id FROM faces WHERE id IN {marks} AND person_id IS NOT NULL""",
        face_ids,
    )
    conn.execute(f"UPDATE faces SET person_id = NULL, label_source = NULL WHERE id IN {marks}", face_ids)
    conn.commit()
    assign(cfg, conn)
    # A person left with no named faces is gone (their auto matches were just cleared by assign).
    orphans = "SELECT id FROM people WHERE id NOT IN (SELECT person_id FROM faces WHERE person_id IS NOT NULL)"
    conn.execute(f"DELETE FROM face_rejections WHERE person_id IN ({orphans})")
    conn.execute(f"DELETE FROM people WHERE id IN ({orphans})")
    conn.commit()


def _check_faces(conn: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    known = {r["id"] for r in conn.execute(f"SELECT id FROM faces WHERE id IN ({','.join('?' * len(ids))})", ids)}
    if missing := set(ids) - known:
        raise FaceError(f"No face(s) {sorted(missing)}")


def summary(conn: sqlite3.Connection, top: int = 20):
    people = conn.execute(
        """SELECT p.name, COUNT(DISTINCT f.sha256) AS photos,
                  SUM(f.label_source = 'user') AS user, SUM(f.label_source = 'auto') AS auto
           FROM people p JOIN faces f ON f.person_id = p.id GROUP BY p.id ORDER BY photos DESC"""
    ).fetchall()
    clusters = conn.execute(
        """SELECT cluster, COUNT(*) AS faces, COUNT(DISTINCT sha256) AS photos FROM faces
           WHERE cluster IS NOT NULL GROUP BY cluster ORDER BY faces DESC, cluster LIMIT ?""",
        (top,),
    ).fetchall()
    return people, clusters


def write_crops(cfg: Config, conn: sqlite3.Connection, per_group: int = 12, top: int = 20) -> Path:
    """Face thumbnails to browse before the review UI exists:
    faces/people/<name>/ and faces/unnamed/group-<id>/, each file named face-<id>.jpg."""
    out = cfg.state_dir / "faces"
    shutil.rmtree(out, ignore_errors=True)
    people, clusters = summary(conn, top)
    jobs = []
    for p in people:
        faces = conn.execute(
            """SELECT f.*, ph.library_path FROM faces f JOIN people pe ON pe.id = f.person_id
               JOIN photos ph ON ph.sha256 = f.sha256 WHERE pe.name = ?
               ORDER BY f.label_source = 'user' DESC, f.similarity DESC LIMIT ?""",
            (p["name"], per_group),
        ).fetchall()
        jobs.append((out / "people" / slugify(p["name"]), faces))
    for c in clusters:
        faces = conn.execute(
            """SELECT f.*, ph.library_path FROM faces f JOIN photos ph ON ph.sha256 = f.sha256
               WHERE f.cluster = ? ORDER BY f.confidence DESC LIMIT ?""",
            (c["cluster"], per_group),
        ).fetchall()
        jobs.append((out / "unnamed" / f"group-{c['cluster']}", faces))

    for folder, faces in jobs:
        folder.mkdir(parents=True, exist_ok=True)
        for f in faces:
            crop = crop_face(cfg, f)
            if crop is not None:
                suffix = f"_{f['label_source']}" if f["label_source"] else ""
                crop.save(folder / f"face-{f['id']}{suffix}.jpg", quality=85)
    return out


def crop_face(cfg: Config, face: sqlite3.Row, size: int = 160) -> Image.Image | None:
    """A padded square-ish thumbnail of one face, from its library photo. `face` needs the faces
    columns plus library_path."""
    path = cfg.library / face["library_path"]
    if not path.exists():
        return None
    small, _, _, _ = load_small(path)
    W, H = small.size
    pad = 0.25
    box = (
        max(0, int((face["x"] - face["w"] * pad) * W)), max(0, int((face["y"] - face["h"] * pad) * H)),
        min(W, int((face["x"] + face["w"] * (1 + pad)) * W)), min(H, int((face["y"] + face["h"] * (1 + pad)) * H)),
    )
    crop = small.crop(box)
    crop.thumbnail((size, size), Image.LANCZOS)
    return crop

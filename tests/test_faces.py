"""Face grouping and labeling logic, using synthetic embeddings (no models or real faces needed)."""

import sqlite3

import numpy as np
import pytest

from psort.config import Config
from psort.db import SCHEMA, connect
from psort.faces import FaceError, assign, label, summary, unlabel


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    return connect(tmp_path / "psort.db")


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(inbox=tmp_path, library=tmp_path, outbox=tmp_path, state_dir=tmp_path)


def add_faces(conn, vectors_by_person: dict[str, int], dim=128, seed=0):
    """Insert faces: each 'person' is a random direction; their faces are small perturbations of it."""
    rng = np.random.default_rng(seed)
    face_person = {}
    for person, count in vectors_by_person.items():
        center = rng.normal(size=dim)
        for i in range(count):
            vec = center + rng.normal(scale=0.3, size=dim)
            vec = (vec / np.linalg.norm(vec)).astype(np.float32)
            sha = f"{person}-{i}"
            conn.execute(
                "INSERT INTO photos (sha256, ext, taken_at, date_source, phash, sharpness, exposure) "
                "VALUES (?, '.jpg', '2026-07-03T10:00:00', 'exif', '0', 1, 1)",
                (sha,),
            )
            cur = conn.execute(
                "INSERT INTO faces (sha256, x, y, w, h, confidence, embedding) VALUES (?, 0, 0, .1, .1, .9, ?)",
                (sha, vec.tobytes()),
            )
            face_person[cur.lastrowid] = person
    conn.commit()
    return face_person


def clusters(conn):
    groups = {}
    for r in conn.execute("SELECT id, cluster FROM faces WHERE cluster IS NOT NULL"):
        groups.setdefault(r["cluster"], set()).add(r["id"])
    return sorted(groups.values(), key=min)


def test_unnamed_faces_group_by_person(conn, cfg):
    truth = add_faces(conn, {"a": 5, "b": 4, "c": 1})
    assign(cfg, conn)
    groups = clusters(conn)
    assert len(groups) == 3
    for g in groups:
        assert len({truth[i] for i in g}) == 1  # each group is one person
    # Group id is the smallest face id in it.
    assert {r["cluster"] for r in conn.execute("SELECT cluster FROM faces")} == {min(g) for g in groups}


def test_labeling_a_group_names_it_and_spreads(conn, cfg):
    truth = add_faces(conn, {"a": 5, "b": 4})
    assign(cfg, conn)
    a_face = next(i for i, p in truth.items() if p == "a")
    a_group = conn.execute("SELECT cluster FROM faces WHERE id = ?", (a_face,)).fetchone()[0]

    # Name just two of a's faces; the other three should follow automatically.
    two = [i for i, p in truth.items() if p == "a"][:2]
    assert label(cfg, conn, "Alice", face_ids=two) == 2
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM faces")}
    for i, p in truth.items():
        if p == "a":
            assert rows[i]["label_source"] == ("user" if i in two else "auto")
        else:
            assert rows[i]["person_id"] is None and rows[i]["cluster"] is not None

    people, groups = summary(conn)
    assert [(p["name"], p["photos"], p["user"], p["auto"]) for p in people] == [("Alice", 5, 2, 3)]
    assert len(groups) == 1  # only b remains unnamed

    b_group = groups[0]["cluster"]
    label(cfg, conn, "Bob", cluster=b_group)
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id IS NULL").fetchone()[0] == 0
    assert a_group != b_group


def test_unlabel_returns_faces_to_groups(conn, cfg):
    truth = add_faces(conn, {"a": 3})
    assign(cfg, conn)
    ids = list(truth)
    label(cfg, conn, "Alice", face_ids=ids)
    unlabel(cfg, conn, ids)
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 0
    assert len(clusters(conn)) == 1


def test_rejections_stick(conn, cfg):
    truth = add_faces(conn, {"a": 4})
    assign(cfg, conn)
    ids = sorted(truth)
    label(cfg, conn, "Alice", face_ids=ids[:1])
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE label_source = 'auto'").fetchone()[0] == 3

    # "Not Alice" on an auto match: it stays un-matched even after re-assigning.
    unlabel(cfg, conn, [ids[3]])
    assign(cfg, conn)
    row = conn.execute("SELECT person_id FROM faces WHERE id = ?", (ids[3],)).fetchone()
    assert row["person_id"] is None
    assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 1  # Alice still has her named face

    # Naming it Alice explicitly overrides the rejection.
    label(cfg, conn, "Alice", face_ids=[ids[3]])
    row = conn.execute("SELECT label_source FROM faces WHERE id = ?", (ids[3],)).fetchone()
    assert row["label_source"] == "user"


def test_label_errors(conn, cfg):
    add_faces(conn, {"a": 2})
    assign(cfg, conn)
    with pytest.raises(FaceError):
        label(cfg, conn, "Alice", cluster=99999)
    with pytest.raises(FaceError):
        label(cfg, conn, "Alice", face_ids=[99999])
    with pytest.raises(FaceError):
        label(cfg, conn, "   ", face_ids=[1])


def test_old_database_is_upgraded(tmp_path):
    path = tmp_path / "old.db"
    # The first release's schema: today's, minus the columns added since.
    slice1 = "\n".join(
        line for line in SCHEMA.splitlines() if not line.strip().startswith(("close_call", "faces_scanned"))
    )
    old = sqlite3.connect(path)
    old.executescript(slice1)
    old.close()
    conn = connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
    assert {"close_call", "faces_scanned"} <= cols

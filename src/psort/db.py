"""SQLite state (DESIGN.md §2). Photos are keyed by content hash."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos (
    sha256         TEXT PRIMARY KEY,
    ext            TEXT NOT NULL,          -- normalized: .jpg .heic .png
    taken_at       TEXT NOT NULL,          -- local capture time, ISO 'YYYY-MM-DDTHH:MM:SS'
    tz_offset      TEXT,                   -- e.g. '-05:00' when EXIF has it
    date_source    TEXT NOT NULL,          -- exif | filename | mtime (mtime = uncertain)
    camera         TEXT,
    width          INTEGER,
    height         INTEGER,
    is_screenshot  INTEGER NOT NULL DEFAULT 0,
    phash          TEXT NOT NULL,
    sharpness      REAL NOT NULL,
    exposure       REAL NOT NULL,
    faces          INTEGER,                -- NULL when face detection unavailable
    face_sharpness REAL,
    moment_id      TEXT,                   -- sha256 of the moment's earliest photo
    score          REAL,
    is_best        INTEGER NOT NULL DEFAULT 0,
    close_call     INTEGER NOT NULL DEFAULT 0,  -- runner-up nearly as good: worth a look
    duplicate_of   TEXT,                   -- visual copy of this sha256 (same picture saved twice)
    user_best      INTEGER NOT NULL DEFAULT 0,  -- review UI override
    name           TEXT UNIQUE,            -- library stem, assigned once: YYYYMMDD_HHMMSS[_n]
    library_path   TEXT,                   -- relative to library root
    faces_scanned  INTEGER NOT NULL DEFAULT 0,  -- face recognition has run on the library copy
    first_seen     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    sha256       TEXT PRIMARY KEY,
    ext          TEXT NOT NULL,
    taken_at     TEXT NOT NULL,
    date_source  TEXT NOT NULL,            -- meta | filename | folder | folder-month | mtime | user
    duration     REAL,                     -- seconds
    width        INTEGER,
    height       INTEGER,
    name         TEXT UNIQUE,
    library_path TEXT,                     -- relative to the videos root
    first_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every file seen in the inbox, including duplicates and skipped files.
CREATE TABLE IF NOT EXISTS sources (
    path    TEXT PRIMARY KEY,              -- relative to inbox
    batch   TEXT NOT NULL,
    size    INTEGER NOT NULL,
    mtime   REAL NOT NULL,
    status  TEXT NOT NULL,                 -- image | video | livephoto | sidecar | skipped | error
    reason  TEXT,
    sha256  TEXT REFERENCES photos(sha256)
);

-- Events you've named. Any photo taken within [start, end] belongs to the event.
CREATE TABLE IF NOT EXISTS named_events (
    slug   TEXT PRIMARY KEY,               -- lowercase-hyphenated, used in folder names
    start  TEXT NOT NULL,
    end    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    id    INTEGER PRIMARY KEY,
    name  TEXT UNIQUE NOT NULL
);

-- Face embeddings are biometric data: they stay in this database and never go in the manifest.
CREATE TABLE IF NOT EXISTS faces (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL REFERENCES photos(sha256),
    x REAL, y REAL, w REAL, h REAL,        -- fractions of the upright image
    confidence    REAL,
    embedding     BLOB NOT NULL,           -- 128 float32, L2-normalized
    person_id     INTEGER REFERENCES people(id),
    label_source  TEXT,                    -- user | auto
    similarity    REAL,                    -- for auto labels
    cluster       INTEGER                  -- unnamed-face group: smallest face id in the group
);

-- "This face is not that person": auto-matching never re-applies a rejected name.
CREATE TABLE IF NOT EXISTS face_rejections (
    face_id    INTEGER NOT NULL REFERENCES faces(id),
    person_id  INTEGER NOT NULL REFERENCES people(id),
    PRIMARY KEY (face_id, person_id)
);

CREATE TABLE IF NOT EXISTS tags (
    sha256  TEXT NOT NULL REFERENCES photos(sha256),
    tag     TEXT NOT NULL,
    PRIMARY KEY (sha256, tag)
);

-- Photos picked for the next blog post (exported in the next stage).
CREATE TABLE IF NOT EXISTS tray (
    sha256    TEXT PRIMARY KEY REFERENCES photos(sha256),
    added_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Which photos went into which post (by outbox folder name).
CREATE TABLE IF NOT EXISTS exports (
    sha256       TEXT NOT NULL REFERENCES photos(sha256),
    post         TEXT NOT NULL,
    exported_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (sha256, post)
);

-- Days you've finished reviewing, 'YYYY-MM-DD'.
CREATE TABLE IF NOT EXISTS reviewed (
    day          TEXT PRIMARY KEY,
    reviewed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS sources_sha ON sources(sha256);
CREATE INDEX IF NOT EXISTS photos_moment ON photos(moment_id);
CREATE INDEX IF NOT EXISTS faces_sha ON faces(sha256);
CREATE INDEX IF NOT EXISTS faces_person ON faces(person_id);
"""

# Columns added after the first release, for databases created before them.
_ADDED_COLUMNS = [
    ("photos", "close_call", "INTEGER NOT NULL DEFAULT 0"),
    ("photos", "faces_scanned", "INTEGER NOT NULL DEFAULT 0"),
    ("photos", "duplicate_of", "TEXT"),
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for table, column, decl in _ADDED_COLUMNS:
        if column not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return conn

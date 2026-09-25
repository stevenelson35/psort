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
    user_best      INTEGER NOT NULL DEFAULT 0,  -- review UI override
    name           TEXT UNIQUE,            -- library stem, assigned once: YYYYMMDD_HHMMSS[_n]
    library_path   TEXT,                   -- relative to library root
    first_seen     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Every file seen in the inbox, including duplicates and skipped files.
CREATE TABLE IF NOT EXISTS sources (
    path    TEXT PRIMARY KEY,              -- relative to inbox
    batch   TEXT NOT NULL,
    size    INTEGER NOT NULL,
    mtime   REAL NOT NULL,
    status  TEXT NOT NULL,                 -- image | skipped | error
    reason  TEXT,
    sha256  TEXT REFERENCES photos(sha256)
);

CREATE INDEX IF NOT EXISTS sources_sha ON sources(sha256);
CREATE INDEX IF NOT EXISTS photos_moment ON photos(moment_id);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn

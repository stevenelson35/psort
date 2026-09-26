"""Running psort while files are still being copied into the inbox."""

import re
import sqlite3
from pathlib import Path

from conftest import save, scene
from psort import ingest
from test_pipeline import library_files


def set_settle(tmp_path: Path, seconds: int) -> None:
    cfg = tmp_path / "psort.toml"
    cfg.write_text(re.sub(r"settle_seconds = \d+", f"settle_seconds = {seconds}", cfg.read_text()))


def test_recently_changed_files_wait_for_the_next_run(psort, tmp_path):
    set_settle(tmp_path, 3600)  # everything in the fixture was just written
    out = psort("run").output
    assert "Ingest: 0 new" in out and "still arriving" in out
    assert library_files(tmp_path / "library") == set()
    assert "not ingested yet (run `psort run`, or it's still arriving)" in psort("verify", expect=2).output

    set_settle(tmp_path, 0)
    assert "Ingest: 11 new" in psort("run").output


def test_file_changing_while_read_is_skipped(psort, tmp_path, sample_inbox, monkeypatch):
    target = sample_inbox / "2026-phone-dump/IMG_0004.jpg"
    real_hash = ingest.sha256_file

    def hash_then_grow(path):
        digest = real_hash(path)
        if Path(path) == target:
            with open(target, "ab") as f:
                f.write(b"more bytes arriving")  # the copy is still going
        return digest

    monkeypatch.setattr(ingest, "sha256_file", hash_then_grow)
    out = psort("run").output
    assert "1 file(s) still arriving" in out and "Ingest: 10 new" in out
    monkeypatch.setattr(ingest, "sha256_file", real_hash)
    assert "Ingest: 1 new" in psort("run").output


def test_partial_copy_is_replaced_and_cleaned_up(psort, tmp_path, sample_inbox):
    # A run caught a file mid-copy (here: a different, smaller picture at the same path)...
    path = sample_inbox / "late/IMG_9000.jpg"
    save(path, scene(300, size=(200, 150)), "2026:08:01 10:00:00")
    psort("run")
    assert "2026/2026-08-01/20260801_100000.jpg" in library_files(tmp_path / "library")
    # ...then the copy finished and the file is now the real photo.
    save(path, scene(301), "2026:08:02 11:00:00")
    out = psort("run").output
    assert "1 file(s) changed since last seen" in out
    files = library_files(tmp_path / "library")
    assert "2026/2026-08-02/20260802_110000.jpg" in files
    assert not any("20260801" in f for f in files)  # the partial version's copy is gone
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    assert conn.execute("SELECT COUNT(*) FROM photos WHERE taken_at LIKE '2026-08-01%'").fetchone()[0] == 0


def test_cleanup_keeps_content_still_in_the_inbox(psort, tmp_path, sample_inbox):
    # The same photo in two places; one place changes. The other still needs the library copy.
    a = sample_inbox / "x/A.jpg"
    save(a, scene(310), "2026:08:03 09:00:00")
    (sample_inbox / "y").mkdir()
    (sample_inbox / "y/A-copy.jpg").write_bytes(a.read_bytes())
    psort("run")
    save(a, scene(311), "2026:08:04 09:00:00")
    psort("run")
    files = library_files(tmp_path / "library")
    assert "2026/2026-08-03/20260803_090000.jpg" in files and "2026/2026-08-04/20260804_090000.jpg" in files


def test_half_copied_file_that_looks_corrupt_waits(psort, tmp_path, sample_inbox, monkeypatch):
    """A half-copied JPEG can look corrupt; if it's still changing it's 'arriving', not 'unreadable'."""
    broken = sample_inbox / "late/IMG_9100.jpg"
    broken.parent.mkdir()
    broken.write_bytes(b"\xff\xd8 half a jpeg")
    real_hash = ingest.sha256_file

    def hash_then_grow(path):
        digest = real_hash(path)
        if Path(path) == broken:
            with open(broken, "ab") as f:
                f.write(b"rest of it")
        return digest

    monkeypatch.setattr(ingest, "sha256_file", hash_then_grow)
    out = psort("run").output
    assert "still arriving" in out and "1 unreadable" not in out

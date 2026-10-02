import json
import sqlite3
import shutil
from pathlib import Path

from PIL import Image

from psort.imaging import sha256_file


def snapshot(folder: Path) -> dict[str, tuple[str, float]]:
    return {str(p.relative_to(folder)): (sha256_file(p), p.stat().st_mtime) for p in folder.rglob("*") if p.is_file()}


def library_files(library: Path) -> set[str]:
    return {str(p.relative_to(library)) for p in library.rglob("*") if p.is_file() and ".psort" not in p.parts}


def db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / "state" / "psort.db")
    conn.row_factory = sqlite3.Row
    return conn


def test_run_builds_library(psort, tmp_path, sample_inbox):
    before = snapshot(sample_inbox)
    out = psort("run").output

    assert "Ingest: 11 new, 1 exact duplicates" in out
    assert "1 other files" in out  # notes.txt (Thumbs.db is an OS cache: recorded, not copied)
    assert "1 Live Photo clip(s) kept beside their photos" in out  # IMG_0009.MOV beside IMG_0009.HEIC
    assert library_files(tmp_path / "library") == {
        # Burst: sharp IMG_0002 is best; the other two are its alternates.
        "2026/2026-07-03/20260703_145634.jpg",
        "2026/2026-07-03/_alternates/20260703_145634/20260703_145633.jpg",
        "2026/2026-07-03/_alternates/20260703_145634/20260703_145636.jpg",
        "2026/2026-07-03/20260703_145640.jpg",
        "2026/2026-07-03/20260703_150640.jpg",
        # Same second: numbered in filename order, matching the blog's _1 style.
        "2026/2026-07-03/20260703_180000.jpg",
        "2026/2026-07-03/20260703_180000_1.jpg",
        "2026/2026-07-04/20260704_101500.jpg",
        "2026/2026-07-05/20260705_090000.jpg",
        "2026/2026-07-05/20260705_120000.heic",
        "_undated/20200102_030405.jpg",
        # The Live Photo clip sits beside its photo, with the same name.
        "2026/2026-07-05/20260705_120000.mov",
    }
    # The inbox is never touched.
    assert snapshot(sample_inbox) == before
    # Library files are byte-identical copies.
    lib_best = tmp_path / "library/2026/2026-07-03/20260703_145634.jpg"
    assert sha256_file(lib_best) == sha256_file(sample_inbox / "2026-phone-dump/IMG_0002.jpg")


def test_metadata(psort, tmp_path):
    psort("run")
    rows = {r["name"]: r for r in db(tmp_path).execute("SELECT * FROM photos")}
    assert rows["20260704_101500"]["date_source"] == "filename"
    assert rows["20200102_030405"]["date_source"] == "mtime"
    assert rows["20260705_120000"]["camera"] == "Apple iPhone 15"
    rotated = rows["20260705_090000"]
    assert (rotated["width"], rotated["height"]) == (600, 800)  # upright portrait dimensions
    manifest = json.loads((tmp_path / "library/.psort/manifest.json").read_text())
    assert len(manifest["photos"]) == 11


def test_rerun_is_idempotent(psort, tmp_path):
    psort("run")
    lib_before = snapshot(tmp_path / "library")
    out = psort("run").output
    assert "Ingest: 0 new, 0 exact duplicates, 15 unchanged" in out
    assert "Curate: 0 copied, 0 moved, 11 unchanged" in out
    assert {k: v for k, v in snapshot(tmp_path / "library").items() if ".psort" not in k} == {
        k: v for k, v in lib_before.items() if ".psort" not in k
    }


def test_manual_moment_override_survives_rerun(psort, tmp_path):
    psort("run")
    conn = db(tmp_path)
    photos = conn.execute(
        "SELECT sha256, moment_id FROM photos WHERE is_best = 1 ORDER BY taken_at LIMIT 2"
    ).fetchall()
    assert photos[0]["moment_id"] != photos[1]["moment_id"]
    conn.execute("INSERT INTO moment_overrides (sha256, moment_id) VALUES (?, ?)",
                 (photos[1]["sha256"], photos[0]["moment_id"]))
    conn.commit()
    conn.close()

    psort("run")
    conn = db(tmp_path)
    moment_ids = {r[0] for r in conn.execute("SELECT DISTINCT moment_id FROM photos WHERE sha256 IN (?, ?)",
                                               (photos[0]["sha256"], photos[1]["sha256"]))}
    assert moment_ids == {photos[0]["moment_id"]}


def test_user_best_pick_moves_files(psort, tmp_path):
    psort("run")
    conn = db(tmp_path)
    conn.execute("UPDATE photos SET user_best = 1 WHERE name = '20260703_145633'")  # the blurry one
    conn.commit()
    out = psort("run").output
    assert "3 moved" in out
    day = "2026/2026-07-03"
    files = library_files(tmp_path / "library")
    assert f"{day}/20260703_145633.jpg" in files
    assert f"{day}/_alternates/20260703_145633/20260703_145634.jpg" in files
    assert f"{day}/_alternates/20260703_145633/20260703_145636.jpg" in files
    assert not (tmp_path / "library" / day / "_alternates/20260703_145634").exists()  # old folder cleaned up


def test_new_batch_extends_library(psort, tmp_path, sample_inbox):
    psort("run")
    from conftest import save, scene

    save(sample_inbox / "later/IMG_1000.jpg", scene(99), "2026:08:01 10:00:00")
    out = psort("run").output
    assert "Ingest: 1 new" in out
    assert "Curate: 1 copied, 0 moved, 11 unchanged" in out


def test_verify(psort, tmp_path, sample_inbox):
    out = psort("verify", "old-backup", expect=2).output
    assert "not ingested yet" in out

    psort("run")
    out = psort("verify", "old-backup").output
    assert "safe to delete" in out

    # Every file is copied somewhere now, so the whole batch is safe.
    out = psort("verify", "2026-phone-dump", "--all").output
    assert "All 14 files are copied" in out
    assert "Live Photo clip beside its photo: 2026/2026-07-05/20260705_120000.mov" in out
    assert "kept in unsorted_files/2026-phone-dump/notes.txt" in out
    assert "Thumbs.db" in out and "OS cache file" in out
    assert "The whole inbox is safe to delete" in psort("verify").output

    psort("verify", "../..", expect=1)


def test_multiple_inboxes_keep_sources_distinct_and_reuse_content(psort, tmp_path, sample_inbox):
    psort("run")
    second = tmp_path / "second-inbox"
    same_folder = second / "2026-phone-dump"
    same_folder.mkdir(parents=True)
    (same_folder / "IMG_0002.jpg").write_bytes((sample_inbox / "2026-phone-dump/IMG_0002.jpg").read_bytes())
    (same_folder / "notes.txt").write_text("a distinct note from the second inbox")

    psort("init", "--force", "--inbox", str(sample_inbox), "--inbox", str(second),
          "--library", str(tmp_path / "library"), "--outbox", str(tmp_path / "outbox"),
          "--state-dir", str(tmp_path / "state"), "--no-face-model")
    config = tmp_path / "psort.toml"
    config.write_text(config.read_text().replace("settle_seconds = 120", "settle_seconds = 0"))
    output = psort("run").output

    assert "0 new, 1 exact duplicates" in output
    assert "Inbox 1/2:" in output and "Inbox 2/2:" in output
    assert "Inbox 1/2: scan complete in " in output
    assert "Inbox 2/2: " in output and "— scanning " in output
    assert "Inbox 2/2: processing complete in " in output
    assert output.index("Inbox 1/2: processing complete") < output.index("Inbox 2/2: processing")
    conn = db(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 11
    source_paths = {r[0] for r in conn.execute("SELECT path FROM sources WHERE path LIKE '%notes.txt'")}
    assert source_paths == {
        "2026-phone-dump/notes.txt",
        "_psort_inbox_1/2026-phone-dump/notes.txt",
    }
    assert "_psort_inbox_1/2026-phone-dump/notes.txt" in library_files(tmp_path / "unsorted_files")
    assert "safe to delete" in psort("verify").output


def test_moved_machine_paths_reuse_transferred_state(psort, tmp_path, sample_inbox):
    psort("run")
    conn = db(tmp_path)
    conn.execute("UPDATE photos SET user_best = 1 WHERE name = '20260703_145633'")
    conn.commit()
    conn.close()
    psort("run")

    new_machine = tmp_path / "new-machine"
    moved_inbox = new_machine / "inputs"
    moved_library = new_machine / "library"
    moved_state = new_machine / "state"
    moved_outbox = new_machine / "outbox"
    new_machine.mkdir()
    shutil.copytree(sample_inbox, moved_inbox)
    shutil.copytree(tmp_path / "library", moved_library)
    moved_state.mkdir()
    source_db = sqlite3.connect(tmp_path / "state/psort.db")
    target_db = sqlite3.connect(moved_state / "psort.db")
    source_db.backup(target_db)
    source_db.close()
    target_db.close()

    psort("init", "--force", "--inbox", str(moved_inbox), "--library", str(moved_library),
          "--outbox", str(moved_outbox), "--state-dir", str(moved_state), "--no-face-model")
    config = tmp_path / "psort.toml"
    config.write_text(config.read_text().replace("settle_seconds = 120", "settle_seconds = 0"))
    output = psort("run").output

    assert "0 new, 0 exact duplicates, 15 unchanged" in output
    conn = sqlite3.connect(moved_state / "psort.db")
    assert conn.execute("SELECT user_best FROM photos WHERE name = '20260703_145633'").fetchone()[0] == 1
    assert library_files(moved_library) == library_files(tmp_path / "library")
    conn.close()


def test_dry_run_leaves_library_empty(psort, tmp_path):
    out = psort("run", "--dry-run").output
    assert "Would curate: 11 copied" in out
    assert library_files(tmp_path / "library") == set()


def test_status(psort):
    psort("run")
    out = psort("status").output
    assert "Photos:            11" in out
    assert "Exact duplicates:  1" in out
    assert "Undated:           1" in out
    assert "Face detection:    off" in out


def test_heic_copy_is_original_format(psort, tmp_path):
    psort("run")
    with Image.open(tmp_path / "library/2026/2026-07-05/20260705_120000.heic") as im:
        assert im.format == "HEIF"


def _three_inboxes(psort, tmp_path, sample_inbox):
    """Inbox 1 = sample_inbox, 2 and 3 hold one distinct photo each; all ingested."""
    from conftest import save, scene

    second, third = tmp_path / "second-inbox", tmp_path / "third-inbox"
    save(second / "trip/IMG_5001.jpg", scene(81), "2026:08:01 10:00:00")
    save(third / "trip/IMG_6001.jpg", scene(82), "2026:08:02 10:00:00")
    (third / "trip/notes.txt").write_text("third inbox note")
    psort("init", "--force", "--inbox", str(sample_inbox), "--inbox", str(second), "--inbox", str(third),
          "--library", str(tmp_path / "library"), "--outbox", str(tmp_path / "outbox"),
          "--state-dir", str(tmp_path / "state"), "--no-face-model")
    config = tmp_path / "psort.toml"
    config.write_text(config.read_text().replace("settle_seconds = 120", "settle_seconds = 0"))
    psort("run")
    return second, third


def _snapshot(tmp_path):
    conn = db(tmp_path)
    return (sorted(r[0] for r in conn.execute("SELECT path FROM sources")),
            sorted(r[0] for r in conn.execute("SELECT sha256 FROM photos")),
            sorted(library_files(tmp_path / "library")))


def test_missing_middle_inbox_is_skipped_and_later_inboxes_keep_their_keys(psort, tmp_path, sample_inbox):
    import shutil
    from conftest import save, scene

    second, third = _three_inboxes(psort, tmp_path, sample_inbox)
    before = _snapshot(tmp_path)
    assert "_psort_inbox_2/trip/IMG_6001.jpg" in before[0] and "_psort_inbox_1/trip/IMG_5001.jpg" in before[0]

    shutil.rmtree(second)  # directory removed, entry left in place
    out = psort("run").output
    assert "Inbox 2 not found, skipped" in out and "Inbox 2/3:" in out and "not found, skipped" in out
    assert _snapshot(tmp_path) == before  # nothing forgotten, nothing re-keyed, nothing copied or removed

    save(third / "trip/IMG_6002.jpg", scene(83), "2026:08:03 10:00:00")  # new file in the inbox AFTER the gap
    psort("run")
    sources, photos, _ = _snapshot(tmp_path)
    assert "_psort_inbox_2/trip/IMG_6002.jpg" in sources
    assert len(photos) == len(before[1]) + 1
    assert not any(s.startswith("_psort_inbox_1/") and s not in before[0] for s in sources)
    assert "Inbox 2 not found" in psort("verify").output  # verify checks the inboxes that are there


def test_missing_first_inbox_is_skipped(psort, tmp_path, sample_inbox):
    import shutil

    _three_inboxes(psort, tmp_path, sample_inbox)
    before = _snapshot(tmp_path)
    shutil.rmtree(sample_inbox)
    out = psort("run").output
    assert "Inbox 1 not found, skipped" in out
    assert _snapshot(tmp_path) == before
    assert db(tmp_path).execute("SELECT COUNT(*) FROM photos").fetchone()[0] == len(before[1])


def test_run_stops_only_when_every_inbox_is_missing(psort, tmp_path, sample_inbox):
    import shutil

    shutil.rmtree(sample_inbox)
    assert "Inbox not found" in psort("run", expect=1).output


def test_reordered_inboxes_never_duplicate_or_lose_photos(psort, tmp_path, sample_inbox):
    """Swapping the order re-keys sources but photos are content-addressed: no dupes, nothing deleted."""
    second, third = _three_inboxes(psort, tmp_path, sample_inbox)
    before = _snapshot(tmp_path)
    config = tmp_path / "psort.toml"
    text = config.read_text()
    swapped = text.replace(f'"{second}", "{third}"', f'"{third}", "{second}"')
    assert swapped != text
    config.write_text(swapped)
    psort("run")
    sources, photos, files = _snapshot(tmp_path)
    assert photos == before[1] and files == before[2]

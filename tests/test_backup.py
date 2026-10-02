"""Backing up psort's config and state directories (DESIGN.md §12)."""

import re
import sqlite3
import tarfile

import pytest

from psort import backup
from psort.config import Config


def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture
def layout(tmp_path):
    """A realistic layout: config and state are separate directories, like a real install."""
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir.mkdir()
    state_dir.mkdir()
    (config_dir / "psort.toml").write_text("[paths]\n")
    (config_dir / "secrets.toml").write_text("ftp_password = \"x\"\n")
    (state_dir / "psort.db").write_bytes(b"not a real db, just backup content")
    (state_dir / "models").mkdir()
    (state_dir / "models" / "face.onnx").write_bytes(b"model bytes")
    for cache in ("thumbs", "facethumbs", "posters", "tmp", "publish", "faces"):
        d = state_dir / cache
        d.mkdir()
        (d / "cached.jpg").write_bytes(b"regenerable")
    cfg = Config(inbox=tmp_path, library=tmp_path, outbox=tmp_path, state_dir=state_dir)
    return config_dir / "psort.toml", cfg


def test_backup_includes_config_and_state_but_skips_caches_by_default(tmp_path, layout):
    config_path, cfg = layout
    out_dir = tmp_path / "backups"
    archive = backup.create_backup(config_path, cfg, out_dir)

    assert archive.parent == out_dir
    assert re.match(r"psort-backup-\d{8}-\d{6}-.+\.tar\.gz", archive.name)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
        info = tar.extractfile("backup-info.json").read().decode()
    assert "config/psort.toml" in names and "config/secrets.toml" in names
    assert "state/psort.db" in names and "state/models/face.onnx" in names
    for cache in ("thumbs", "facethumbs", "posters", "tmp", "publish", "faces"):
        assert not any(n.startswith(f"state/{cache}/") for n in names)
    assert '"included_caches": false' in info
    assert '"psort_commit"' in info


def test_backup_can_include_caches(tmp_path, layout):
    config_path, cfg = layout
    archive = backup.create_backup(config_path, cfg, tmp_path / "backups", include_caches=True)
    with tarfile.open(archive) as tar:
        names = set(tar.getnames())
    assert "state/thumbs/cached.jpg" in names


def test_backup_refuses_missing_directories(tmp_path):
    cfg = Config(inbox=tmp_path, library=tmp_path, outbox=tmp_path, state_dir=tmp_path / "nope")
    with pytest.raises(backup.BackupError):
        backup.create_backup(tmp_path / "missing" / "psort.toml", cfg, tmp_path / "backups")


def test_list_backups_reports_newest_first(tmp_path, layout):
    config_path, cfg = layout
    out_dir = tmp_path / "backups"
    first = backup.create_backup(config_path, cfg, out_dir)
    import time

    time.sleep(1.1)  # the filename only has second resolution; force a distinct name
    second = backup.create_backup(config_path, cfg, out_dir)
    listed = backup.list_backups(out_dir)
    assert [b["name"] for b in listed] == [second.name, first.name]
    assert all(b["size"].strip() for b in listed)


def test_list_backups_empty_when_no_directory(tmp_path):
    assert backup.list_backups(tmp_path / "nope") == []

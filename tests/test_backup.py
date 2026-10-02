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


# --- restore / relocate -----------------------------------------------------------------------

import tomllib

from typer.testing import CliRunner

from psort.cli import app

COMMENTED = '''# my notes
[paths]
inboxes = [
  "/old/inbox_one",   # phone
  '/old/inbox_two',
]
library = "/old/library"
outbox = "/old/outbox"
state_dir = "/old/state"

[inbox]
settle_seconds = 120
'''


def test_rewrite_paths_keeps_comments_and_handles_multiline_arrays():
    out = backup.rewrite_paths(COMMENTED, {"inboxes": ["/n/one", "/n/two"], "library": "/n/lib"})
    assert out.startswith("# my notes\n[paths]\ninboxes = [\"/n/one\", \"/n/two\"]\nlibrary = \"/n/lib\"\noutbox")
    assert tomllib.loads(out)["paths"]["inboxes"] == ["/n/one", "/n/two"]
    assert "settle_seconds = 120" in out
    with pytest.raises(backup.BackupError):
        backup.rewrite_paths(COMMENTED, {"videos": "/x"})  # not a setting in this file


def test_rewrite_paths_quotes_awkward_characters():
    out = backup.rewrite_paths(COMMENTED, {"library": 'C:\\Users\\a "b"'})
    assert tomllib.loads(out)["paths"]["library"] == 'C:\\Users\\a "b"'


def test_relocate_config_asks_in_order_and_only_rewrites_changes(tmp_path):
    config = tmp_path / "psort.toml"
    config.write_text(COMMENTED)
    asked = []

    def ask(label, current):
        asked.append(label)
        return "/new/inbox_two" if label == "inbox 2" else ""

    changed = backup.relocate_config(config, ask)
    assert asked == ["inbox 1", "inbox 2", "library", "outbox", "state_dir"]
    assert changed == {"inboxes": ["/old/inbox_one", "/new/inbox_two"]}  # position 2 stays position 2
    assert tomllib.loads(config.read_text())["paths"]["inboxes"] == ["/old/inbox_one", "/new/inbox_two"]
    assert (tmp_path / "psort.toml.before-relocate").read_text() == COMMENTED

    before = config.read_text()
    assert backup.relocate_config(config, lambda label, cur: "  ") == {}
    assert config.read_text() == before


def _real_backup(psort, tmp_path):
    psort("run")
    out = tmp_path / "backups"
    psort("backup", "--out", str(out))
    return next(out.glob("psort-backup-*.tar.gz"))


def _new_machine(tmp_path):
    runner = CliRunner()
    config = tmp_path / "new-config" / "psort.toml"

    def invoke(*args, expect=0, input=None):
        result = runner.invoke(app, ["--config", str(config), *args], input=input)
        assert result.exit_code == expect, result.output + repr(result.exception)
        return result

    return config, invoke


def test_restore_with_relocate_moves_everything_to_new_paths(psort, tmp_path, sample_inbox):
    archive = _real_backup(psort, tmp_path)
    old_state = tmp_path / "state"
    old_photos = db(tmp_path).execute("SELECT COUNT(*) FROM photos").fetchone()[0]
    config, new = _new_machine(tmp_path)
    nm = tmp_path / "new-machine"
    # inbox 1, library, outbox, videos, unsorted, highlights, state_dir
    answers = [str(nm / "inbox"), str(nm / "library"), "", "", "", "", str(nm / "state")]

    out = new("restore", str(archive), "--relocate", input="\n".join(answers) + "\n").output
    assert "Restored backup" in out and "missing" in out  # the new inbox/library don't exist yet

    cfg = tomllib.loads(config.read_text())["paths"]
    assert cfg["inboxes"] == [str(nm / "inbox")] and cfg["library"] == str(nm / "library")
    assert cfg["outbox"].endswith("outbox") and cfg["state_dir"] == str(nm / "state")
    restored = sqlite3.connect(nm / "state/psort.db")
    assert restored.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == old_photos
    assert old_state.exists()  # restoring never touches the machine it came from
    assert not list(config.parent.glob(".psort-restore-*"))  # staging folder cleaned up
    assert not list(config.parent.glob("*.before-relocate"))


def test_restore_refuses_to_overwrite_unless_forced_and_never_deletes(psort, tmp_path):
    archive = _real_backup(psort, tmp_path)
    config, new = _new_machine(tmp_path)
    nm = tmp_path / "nm"
    answers = "\n".join([str(nm / "inbox"), str(nm / "library"), "", "", "", "", str(nm / "state")]) + "\n"
    new("restore", str(archive), "--relocate", input=answers)

    (nm / "state/precious.txt").write_text("keep me")
    refused = new("restore", str(archive), "--relocate", input=answers, expect=1)
    assert "--force" in refused.output
    assert (nm / "state/precious.txt").exists()

    forced = new("restore", str(archive), "--relocate", "--force", input=answers)
    assert "moved aside" in forced.output
    aside = list(nm.glob("state.before-restore-*"))
    assert len(aside) == 1 and (aside[0] / "precious.txt").read_text() == "keep me"
    assert list(config.parent.glob("psort.toml.before-restore-*"))
    assert (nm / "state/psort.db").exists() and not (nm / "state/precious.txt").exists()


def test_restore_rejects_files_that_are_not_psort_backups(tmp_path):
    bogus = tmp_path / "psort-backup-bogus.tar.gz"
    bogus.write_bytes(b"not a tarball")
    with pytest.raises(backup.BackupError):
        backup.restore_backup(bogus, tmp_path / "cfg" / "psort.toml")

    empty = tmp_path / "psort-backup-empty.tar.gz"
    with tarfile.open(empty, "w:gz") as tar:
        tar.addfile(tarfile.TarInfo("README"))
    with pytest.raises(backup.BackupError):
        backup.restore_backup(empty, tmp_path / "cfg2" / "psort.toml")
    assert not list((tmp_path / "cfg2").glob(".psort-restore-*"))


def test_relocate_command_edits_the_current_config(psort, tmp_path, sample_inbox):
    out = psort("relocate", input="\n".join([str(tmp_path / "moved-inbox"), "", "", "", "", "", ""]) + "\n").output
    assert "Updated" in out and "moved-inbox" in out
    assert tomllib.loads((tmp_path / "psort.toml").read_text())["paths"]["inboxes"] == [str(tmp_path / "moved-inbox")]
    assert "No changes." in psort("relocate", input="\n" * 8).output

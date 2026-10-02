"""Back up psort's config and state (database, face models) for disaster recovery (DESIGN.md §12).

Everything psort needs to resume exactly where you left off lives in two places: the config
directory (psort.toml, and the FTP secrets file if blog publishing is set up) and the state
directory (the SQLite database, face models, and various caches). Neither is inside the library,
so neither is covered by OneDrive/library backups on its own.
"""

import io
import json
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, ConfigError
from .config import load as load_config

# Regenerated automatically on the next `psort run` or review-page visit; skipped by default to
# keep backups small and fast. `models/` is kept: it's a one-time download, not a cache of your data.
CACHE_DIRS = {"thumbs", "facethumbs", "posters", "tmp", "publish", "faces"}


class BackupError(Exception):
    pass


def _git_commit() -> str:
    """The commit of the psort checkout this code is running from, or 'unknown' if that can't be
    determined (not a git checkout, or git isn't installed)."""
    repo = Path(__file__).resolve().parents[2]  # .../psort/src/psort/backup.py -> .../psort
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=5
        )
    except OSError:
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"


def _skip_caches(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(info.name).parts  # e.g. ('state', 'thumbs', '…'): drop anything under a cache dir
    if len(parts) >= 2 and parts[1] in CACHE_DIRS:
        return None
    return info


def create_backup(config_path: Path, cfg: Config, out_dir: Path, include_caches: bool = False) -> Path:
    """Archive the config directory and the state directory into one dated, commit-tagged
    .tar.gz. Returns the archive's path."""
    config_dir = config_path.parent
    if not config_dir.is_dir():
        raise BackupError(f"No config directory at {config_dir}")
    if not cfg.state_dir.is_dir():
        raise BackupError(f"No state directory at {cfg.state_dir}")

    commit = _git_commit()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"psort-backup-{stamp}-{commit}.tar.gz"
    if archive.exists():
        raise BackupError(f"{archive} already exists")

    info = {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "psort_commit": commit,
        "host": platform.node(),
        "config_path": str(config_path),
        "config_dir": str(config_dir),
        "state_dir": str(cfg.state_dir),
        "included_caches": include_caches,
        "excluded_dirs": [] if include_caches else sorted(CACHE_DIRS),
    }
    info_bytes = json.dumps(info, indent=2).encode()

    partial = archive.with_suffix(archive.suffix + ".partial")
    try:
        with tarfile.open(partial, "w:gz") as tar:
            tar.add(config_dir, arcname="config")
            tar.add(cfg.state_dir, arcname="state", filter=None if include_caches else _skip_caches)
            member = tarfile.TarInfo("backup-info.json")
            member.size = len(info_bytes)
            member.mtime = int(datetime.now().timestamp())
            tar.addfile(member, io.BytesIO(info_bytes))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.rename(archive)
    return archive


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def list_backups(out_dir: Path) -> list[dict]:
    """Existing backups in `out_dir`, most recent first."""
    if not out_dir.is_dir():
        return []
    files = sorted(out_dir.glob("psort-backup-*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "name": p.name,
            "size": _human_size(p.stat().st_size),
            "created": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
        for p in files
    ]


# --- Restore / relocate (DESIGN.md §12) -------------------------------------------------------

# The [paths] settings that name places on one particular computer, in the order they're asked.
PATH_KEYS = ("inboxes", "inbox", "library", "outbox", "videos", "unsorted", "highlights", "state_dir")

_STRING = r"""(?:"(?:[^"\\\n]|\\.)*"|'[^'\n]*')"""
_VALUE = rf"(?:\[(?:\s|#[^\n]*|,|{_STRING})*\]|{_STRING})"


def _paths_section(text: str) -> tuple[int, int]:
    start = re.search(r"(?m)^\[paths\]\s*(?:#.*)?$", text)
    if not start:
        raise BackupError("The config has no [paths] section.")
    end = re.search(r"(?m)^\[", text[start.end():])
    return start.end(), start.end() + end.start() if end else len(text)


def _toml_value(value: str | list[str]) -> str:
    # json.dumps produces a valid TOML basic string for a path.
    if isinstance(value, list):
        return "[" + ", ".join(json.dumps(v) for v in value) + "]"
    return json.dumps(value)


def rewrite_paths(text: str, updates: dict[str, str | list[str]]) -> str:
    """`text` with these [paths] settings replaced, leaving comments and everything else alone."""
    for key, value in updates.items():
        begin, end = _paths_section(text)
        section = text[begin:end]
        new, count = re.subn(rf"(?m)^{re.escape(key)}[ \t]*=[ \t]*{_VALUE}", lambda _: f"{key} = {_toml_value(value)}",
                             section, count=1)
        if not count:
            raise BackupError(f"Couldn't find `{key}` in [paths] to update.")
        text = text[:begin] + new + text[end:]
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:  # never write a config psort can't read
        raise BackupError(f"Edited config isn't valid TOML ({e}).") from e
    return text


def _clean(answer: str) -> str:
    return answer.strip().strip("\"'")


def relocate_config(config_path: Path, ask: Callable[[str, str], str],
                    keys: tuple[str, ...] = PATH_KEYS, keep_copy: bool = True) -> dict[str, str | list[str]]:
    """Walk through the machine-specific paths in `config_path`. `ask(label, current)` returns the
    path to use (empty keeps `current`). Entries in `inboxes` are asked in order, and none can be
    removed or reordered here: their positions are part of every recorded source key.
    Returns what changed, and rewrites the file only if something did."""
    text = config_path.read_text()
    try:
        paths = tomllib.loads(text).get("paths", {})
    except tomllib.TOMLDecodeError as e:
        raise BackupError(f"{config_path} isn't valid TOML ({e}).") from e
    updates: dict[str, str | list[str]] = {}
    for key in keys:
        if key not in paths:
            continue
        current = paths[key]
        if isinstance(current, list):
            chosen = [_clean(ask(f"inbox {i}", v)) or v for i, v in enumerate(current, 1)]
        else:
            chosen = _clean(ask(key, current)) or current
        if chosen != current:
            updates[key] = chosen
    if updates:
        new_text = rewrite_paths(text, updates)
        if keep_copy:
            shutil.copy2(config_path, config_path.with_name(config_path.name + ".before-relocate"))
        config_path.write_text(new_text)
    return updates


def path_report(cfg: Config) -> list[tuple[str, Path, bool]]:
    """(label, path, exists) for each place on this computer the config points at."""
    rows = [(f"inbox {i}", root, root.is_dir()) for i, root in enumerate(cfg.input_roots, 1)]
    for label, path in (("library", cfg.library), ("outbox", cfg.outbox), ("videos", cfg.videos),
                        ("unsorted", cfg.unsorted), ("highlights", cfg.highlights), ("state", cfg.state_dir)):
        rows.append((label, path, path.is_dir()))
    return rows


@dataclass
class RestoreResult:
    info: dict
    config_path: Path
    state_dir: Path
    moved_aside: list[Path] = field(default_factory=list)
    relocated: dict[str, str | list[str]] = field(default_factory=dict)


def read_backup_info(archive: Path) -> dict:
    try:
        with tarfile.open(archive, "r:gz") as tar:
            member = tar.extractfile("backup-info.json")
            return json.loads(member.read()) if member else {}
    except (tarfile.TarError, OSError, KeyError, ValueError) as e:
        raise BackupError(f"{archive} isn't a psort backup ({e}).") from e


def _aside(path: Path, stamp: str) -> Path:
    target = path.with_name(f"{path.name}.before-restore-{stamp}")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.before-restore-{stamp}-{n}")
    path.rename(target)
    return target


def restore_backup(archive: Path, config_path: Path, *, force: bool = False,
                   ask: Callable[[str, str], str] | None = None) -> RestoreResult:
    """Put a backup's config and state back. With `ask`, the paths in the restored config are
    reviewed first (see `relocate_config`), which is how you move to a new computer.

    Nothing real is touched until everything has been unpacked, checked and (if asked) edited in a
    staging folder. Existing config/state files are only replaced with `force`, and are moved aside
    (`*.before-restore-<time>`), never deleted."""
    info = read_backup_info(archive)
    config_path = config_path.expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".psort-restore-", dir=config_path.parent))
    try:
        try:
            with tarfile.open(archive, "r:gz") as tar:
                tar.extractall(staging, filter="data")  # refuses absolute paths, links out of the tree, etc.
        except (tarfile.TarError, OSError) as e:
            raise BackupError(f"Couldn't unpack {archive}: {e}") from e
        staged_config_dir, staged_state = staging / "config", staging / "state"
        staged_config = staged_config_dir / Path(info.get("config_path") or "psort.toml").name
        if not staged_config.is_file() or not staged_state.is_dir():
            raise BackupError(f"{archive} doesn't contain a psort config and state.")

        relocated = relocate_config(staged_config, ask, keep_copy=False) if ask else {}
        try:
            restored = load_config(staged_config)
        except ConfigError as e:
            raise BackupError(str(e)) from e
        state_dir = restored.state_dir
        if staged_config.name != config_path.name:  # restoring under a different --config name
            staged_config = staged_config.rename(staged_config_dir / config_path.name)

        config_files = [f for f in staged_config_dir.iterdir() if f.is_file()]
        clashes = [config_path.parent / f.name for f in config_files if (config_path.parent / f.name).exists()]
        if state_dir.exists() and any(state_dir.iterdir()):
            clashes.append(state_dir)
        if clashes and not force:
            raise BackupError("Won't overwrite existing files (use --force to move them aside first): "
                              + ", ".join(str(c) for c in clashes))

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        result = RestoreResult(info, config_path, state_dir, relocated=relocated)
        for clash in clashes:
            result.moved_aside.append(_aside(clash, stamp))
        for f in config_files:
            shutil.move(str(f), config_path.parent / f.name)
        state_dir.parent.mkdir(parents=True, exist_ok=True)
        if state_dir.exists():
            state_dir.rmdir()  # empty (anything in it was moved aside above)
        shutil.move(str(staged_state), state_dir)
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)

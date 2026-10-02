"""Back up psort's config and state (database, face models) for disaster recovery (DESIGN.md §12).

Everything psort needs to resume exactly where you left off lives in two places: the config
directory (psort.toml, and the FTP secrets file if blog publishing is set up) and the state
directory (the SQLite database, face models, and various caches). Neither is inside the library,
so neither is covered by OneDrive/library backups on its own.
"""

import io
import json
import platform
import subprocess
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

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

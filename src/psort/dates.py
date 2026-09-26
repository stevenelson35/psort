"""Capture-time parsing: EXIF first, then filename patterns (DESIGN.md §5.1)."""

import re
from datetime import datetime
from pathlib import PurePath

# How sure we are of a photo's capture time, by where it came from:
#   exif, filename, user  → real date and time
#   folder                → the day, from a folder name like "2016-01-03 - Marathon" (time unknown)
#   folder-month          → only the month, from "2016-04 - PhotoPass" (day and time unknown)
#   mtime                 → the file's modified time: a guess
#   user-day              → a day you set for several photos at once (time unknown)
UNCERTAIN = ("mtime", "folder-month")  # listed on the Undated page for you to fix
NO_TIME = ("mtime", "folder", "folder-month", "user-day")  # no real time: never grouped into bursts or events


def sql_in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{v}'" for v in values) + ")"

_FILENAME_PATTERNS = [
    # IMG_20260703_145633, PXL_20260703_145633123, 20260703_145633, VID-20260703-145633
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"),
    # Windows Phone: WP_20151004_06_56_36
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})_(\d{2})_(\d{2})_(\d{2})(?!\d)"),
    # Screenshot_2026-07-03-14-56-33, 2026-07-03 14.56.33
    re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[-_ ](\d{2})[-.](\d{2})[-.](\d{2})"),
]


def _plausible(dt: datetime) -> bool:
    return 1990 <= dt.year <= datetime.now().year + 1


def parse_exif_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.strptime(value.strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return dt if _plausible(dt) else None


def from_filename(name: str) -> datetime | None:
    for pattern in _FILENAME_PATTERNS:
        for match in pattern.finditer(name):
            try:
                dt = datetime(*map(int, match.groups()))
            except ValueError:
                continue
            if _plausible(dt):
                return dt
    return None


_FOLDER_DAY = re.compile(r"(?<!\d)(\d{4})[-_.](\d{2})[-_.](\d{2})(?!\d)")
_FOLDER_MONTH = re.compile(r"(?<!\d)(\d{4})[-_.](\d{2})(?![-_.]?\d)")


def from_folders(rel_path: str) -> tuple[datetime, str] | None:
    """A date from the photo's folder names, nearest folder first: a day ('folder') or just a
    month ('folder-month'). The time is set to noon since it's unknown."""
    for folder in reversed(PurePath(rel_path).parts[:-1]):
        for pattern, source in ((_FOLDER_DAY, "folder"), (_FOLDER_MONTH, "folder-month")):
            for match in pattern.finditer(folder):
                parts = [int(g) for g in match.groups()] + ([1] if source == "folder-month" else [])
                try:
                    dt = datetime(*parts, 12, 0, 0)
                except ValueError:
                    continue
                if _plausible(dt):
                    return dt, source
    return None

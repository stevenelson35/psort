"""Capture-time parsing: EXIF first, then filename patterns (DESIGN.md §5.1)."""

import re
from datetime import datetime

_FILENAME_PATTERNS = [
    # IMG_20260703_145633, PXL_20260703_145633123, 20260703_145633, VID-20260703-145633
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})[_-](\d{2})(\d{2})(\d{2})"),
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

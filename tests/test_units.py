from datetime import datetime

import numpy as np

from psort.dates import from_filename, parse_exif_datetime
from psort.imaging import exposure_quality


def test_exif_datetime():
    assert parse_exif_datetime("2026:07:03 14:56:33") == datetime(2026, 7, 3, 14, 56, 33)
    assert parse_exif_datetime("0000:00:00 00:00:00") is None
    assert parse_exif_datetime("1970:01:01 00:00:00") is None  # implausible camera-reset date
    assert parse_exif_datetime(None) is None


def test_filename_dates():
    expected = datetime(2026, 7, 3, 14, 56, 33)
    for name in [
        "IMG_20260703_145633.jpg",
        "PXL_20260703_145633123.jpg",
        "20260703_145633_1.jpg",
        "Screenshot_2026-07-03-14-56-33.png",
        "2026-07-03 14.56.33.jpg",
    ]:
        assert from_filename(name) == expected, name
    assert from_filename("IMG_0001.jpg") is None
    assert from_filename("20261399_999999.jpg") is None


def test_exposure_prefers_midtones():
    mid = np.full((100, 100), 120, np.uint8)
    white = np.full((100, 100), 255, np.uint8)
    dark = np.full((100, 100), 20, np.uint8)
    assert exposure_quality(mid) > exposure_quality(dark) > exposure_quality(white)

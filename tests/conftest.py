"""Synthetic sample photos covering the cases in DESIGN.md §10."""

import os
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageFilter
from typer.testing import CliRunner

from psort.cli import app

EXIF_IFD = 0x8769


def scene(seed: int, size=(800, 600)) -> Image.Image:
    """A textured image whose perceptual hash is distinct per seed."""
    rng = np.random.default_rng(seed)
    coarse = Image.fromarray(rng.integers(30, 225, (6, 8, 3), dtype=np.uint8)).resize(size, Image.BICUBIC)
    detail = rng.integers(-40, 40, (size[1], size[0], 3))
    return Image.fromarray(np.clip(np.asarray(coarse, dtype=int) + detail, 0, 255).astype(np.uint8))


def save(path: Path, img: Image.Image, taken: str | None = None, model: str | None = "Pixel 8",
         orientation: int | None = None, fmt: str = "JPEG", make: str = "Google") -> Path:
    exif = Image.Exif()
    if model:
        exif[271], exif[272] = make, model
    if orientation:
        exif[274] = orientation
    if taken:
        exif.get_ifd(EXIF_IFD)[36867] = taken
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, fmt, exif=exif.tobytes())
    return path


def add_close_call(inbox: Path) -> None:
    """Two frames of the same scene, slightly shifted: a genuine near tie (not duplicates)."""
    twin = scene(60)
    save(inbox / "twins/IMG_3000.jpg", twin, "2026:07:06 10:00:00")
    save(inbox / "twins/IMG_3001.jpg", Image.fromarray(np.roll(np.asarray(twin), 12, axis=1)), "2026:07:06 10:00:01")


@pytest.fixture
def sample_inbox(tmp_path: Path) -> Path:
    inbox = tmp_path / "inbox"
    b1 = inbox / "2026-phone-dump"
    base = scene(1)

    # A burst: blurry, sharp, slightly blurry. The sharp middle shot should win.
    save(b1 / "IMG_0001.jpg", base.filter(ImageFilter.GaussianBlur(4)), "2026:07:03 14:56:33")
    save(b1 / "IMG_0002.jpg", base, "2026:07:03 14:56:34")
    save(b1 / "IMG_0003.jpg", base.filter(ImageFilter.GaussianBlur(1.5)), "2026:07:03 14:56:36")
    # Different scene within the burst window: its own moment.
    save(b1 / "IMG_0004.jpg", scene(2), "2026:07:03 14:56:40")
    # Same scene ten minutes later: its own moment.
    save(b1 / "IMG_0005.jpg", base, "2026:07:03 15:06:40", model="Pixel 8 ")  # distinct bytes
    # Two different photos in the same second.
    save(b1 / "IMG_0006.jpg", scene(3), "2026:07:03 18:00:00")
    save(b1 / "IMG_0007.jpg", scene(4), "2026:07:03 18:00:00")
    # No EXIF: date from filename.
    save(b1 / "PXL_20260704_101500123.jpg", scene(5), model=None)
    # No EXIF, no date in name: undated.
    save(b1 / "mystery.jpg", scene(6), model=None)
    # Portrait photo stored sideways with an orientation tag.
    save(b1 / "IMG_0008.jpg", scene(7), "2026:07:05 09:00:00", orientation=6)
    # iPhone HEIC.
    save(b1 / "IMG_0009.HEIC", scene(8), "2026:07:05 12:00:00", model="iPhone 15", make="Apple", fmt="HEIF")
    # Things that aren't photos.
    (b1 / "IMG_0009.MOV").write_bytes(b"not really a video")
    (b1 / "notes.txt").write_text("hello")
    (b1 / "Thumbs.db").write_bytes(b"windows clutter")
    # A second batch holding an exact copy of a photo already in batch 1.
    b2 = inbox / "old-backup"
    b2.mkdir()
    shutil.copy2(b1 / "IMG_0003.jpg", b2 / "copy of IMG_0003.jpg")

    # Stable mtimes so "undated" gets a predictable name.
    ts = datetime(2020, 1, 2, 3, 4, 5).timestamp()
    os.utime(b1 / "mystery.jpg", (ts, ts))
    return inbox


@pytest.fixture
def psort(tmp_path: Path, sample_inbox: Path):
    """Run psort CLI commands against an isolated config."""
    runner = CliRunner()
    config = tmp_path / "psort.toml"

    def invoke(*args: str, expect: int = 0, input: str | None = None):
        result = runner.invoke(app, ["--config", str(config), *args], input=input)
        assert result.exit_code == expect, result.output + repr(result.exception)
        return result

    invoke(
        "init",
        "--inbox", str(sample_inbox),
        "--library", str(tmp_path / "library"),
        "--outbox", str(tmp_path / "outbox"),
        "--state-dir", str(tmp_path / "state"),
        "--no-face-model",
    )
    # Test files are all brand new, so don't wait for them to "settle" (tests of that set it back).
    config.write_text(config.read_text().replace("settle_seconds = 120", "settle_seconds = 0"))
    return invoke

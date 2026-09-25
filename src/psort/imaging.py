"""Reading images and measuring them: EXIF, perceptual hash, sharpness, exposure, faces."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import cv2
import imagehash
import numpy as np
import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()
cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)  # hide backend chatter

IMAGE_EXTS = {".jpg", ".jpeg", ".heic", ".heif", ".png"}
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".avi", ".3gp", ".mkv", ".mts", ".wmv"}
# OS clutter that's neither a photo nor worth reporting.
IGNORED_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}

ANALYSIS_SIZE = 1024  # long edge, pixels

_EXIF_IFD = 0x8769
_TAG_DATETIME = 306
_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_ORIENTATION = 274
_TAG_DATETIME_ORIGINAL = 36867
_TAG_OFFSET_TIME_ORIGINAL = 36881


def is_ignored(path: Path) -> bool:
    return path.name.startswith(".") or path.name.lower() in IGNORED_NAMES


def normalize_ext(ext: str) -> str:
    ext = ext.lower()
    return {".jpeg": ".jpg", ".heif": ".heic"}.get(ext, ext)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class Analysis:
    exif_datetime: str | None
    exif_offset: str | None
    camera: str | None
    width: int
    height: int
    phash: str
    sharpness: float
    exposure: float
    faces: int | None
    face_sharpness: float | None


class FaceDetector:
    """OpenCV YuNet. Optional: psort works without it, just without face signals."""

    def __init__(self, model: Path):
        self._detector = cv2.FaceDetectorYN.create(str(model), "", (320, 320), 0.8)

    @classmethod
    def load(cls, model: Path) -> "FaceDetector | None":
        return cls(model) if model.exists() else None

    def detect_rows(self, bgr: np.ndarray) -> np.ndarray:
        """Raw YuNet rows: box (4), five landmarks (10), confidence (1)."""
        h, w = bgr.shape[:2]
        self._detector.setInputSize((w, h))
        _, faces = self._detector.detect(bgr)
        return faces if faces is not None else np.empty((0, 15), np.float32)

    def detect(self, bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
        return [tuple(int(v) for v in row[:4]) for row in self.detect_rows(bgr)]


def exposure_quality(gray: np.ndarray) -> float:
    """1.0 = well exposed. Penalizes clipped shadows/highlights and a mean far from mid-tone."""
    clipped = float(np.mean((gray <= 4) | (gray >= 251)))
    mean = float(gray.mean()) / 255
    return max(0.0, min(1.0, 1 - 2 * clipped - abs(mean - 0.46)))


def _sharpness(gray: np.ndarray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def load_small(path: Path) -> tuple[Image.Image, Image.Exif, int, int]:
    """Upright RGB copy at analysis size, plus EXIF and the full upright width/height."""
    with Image.open(path) as im:
        exif = im.getexif()
        width, height = im.size
        if exif.get(_TAG_ORIENTATION) in (5, 6, 7, 8):
            width, height = height, width
        im.draft("RGB", (ANALYSIS_SIZE, ANALYSIS_SIZE))  # fast JPEG downscale; no-op otherwise
        small = ImageOps.exif_transpose(im).convert("RGB")
    small.thumbnail((ANALYSIS_SIZE, ANALYSIS_SIZE))
    return small, exif, width, height


def analyze(path: Path, faces: FaceDetector | None = None) -> Analysis:
    small, exif, width, height = load_small(path)
    exif_ifd = exif.get_ifd(_EXIF_IFD)

    make = str(exif.get(_TAG_MAKE, "")).strip()
    model = str(exif.get(_TAG_MODEL, "")).strip()
    camera = model if model.lower().startswith(make.lower()) else f"{make} {model}".strip()

    rgb = np.asarray(small)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    face_count = face_sharpness = None
    if faces is not None:
        boxes = faces.detect(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
        face_count = len(boxes)
        crops = [gray[max(y, 0) : y + h, max(x, 0) : x + w] for x, y, w, h in boxes]
        crops = [c for c in crops if c.size > 0]
        face_sharpness = float(np.mean([_sharpness(c) for c in crops])) if crops else 0.0

    return Analysis(
        exif_datetime=exif_ifd.get(_TAG_DATETIME_ORIGINAL) or exif.get(_TAG_DATETIME),
        exif_offset=exif_ifd.get(_TAG_OFFSET_TIME_ORIGINAL),
        camera=camera or None,
        width=width,
        height=height,
        phash=str(imagehash.phash(small)),
        sharpness=_sharpness(gray),
        exposure=exposure_quality(gray),
        faces=face_count,
        face_sharpness=face_sharpness,
    )

"""psort.toml loading and defaults (DESIGN.md §2)."""

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(os.environ.get("PSORT_CONFIG", Path.home() / ".config/psort/psort.toml"))
DEFAULT_STATE_DIR = Path.home() / ".local/share/psort"
_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models/"
FACE_MODEL_NAME = "face_detection_yunet_2023mar.onnx"
FACE_MODEL_URL = _ZOO + "face_detection_yunet/" + FACE_MODEL_NAME
SFACE_MODEL_NAME = "face_recognition_sface_2021dec.onnx"
SFACE_MODEL_URL = _ZOO + "face_recognition_sface/" + SFACE_MODEL_NAME


@dataclass
class Weights:
    sharpness: float = 0.5
    exposure: float = 0.2
    faces: float = 0.15
    face_sharpness: float = 0.15


@dataclass
class Config:
    inbox: Path
    library: Path
    outbox: Path
    state_dir: Path = DEFAULT_STATE_DIR
    videos_dir: Path | None = None  # default: a videos/ folder beside the library
    unsorted_dir: Path | None = None  # default: an unsorted_files/ folder beside the library
    highlights_dir: Path | None = None  # default: a highlights/ folder beside the library
    settle_seconds: float = 120.0  # files changed more recently than this are still arriving
    burst_gap_seconds: float = 10.0
    phash_threshold: int = 10
    close_call_margin: float = 0.05
    duplicate_gap_seconds: float = 1.0
    duplicate_phash_threshold: int = 2
    event_gap_hours: float = 3.0
    face_match_threshold: float = 0.45
    face_cluster_threshold: float = 0.5
    weights: Weights = field(default_factory=Weights)

    @property
    def videos(self) -> Path:
        return self.videos_dir or self.library.parent / "videos"

    @property
    def unsorted(self) -> Path:
        return self.unsorted_dir or self.library.parent / "unsorted_files"

    @property
    def highlights(self) -> Path:
        return self.highlights_dir or self.library.parent / "highlights"

    @property
    def db_path(self) -> Path:
        return self.state_dir / "psort.db"

    @property
    def face_model(self) -> Path:
        return self.state_dir / "models" / FACE_MODEL_NAME

    @property
    def recognition_model(self) -> Path:
        return self.state_dir / "models" / SFACE_MODEL_NAME

    @property
    def models(self) -> list[tuple[Path, str]]:
        return [(self.face_model, FACE_MODEL_URL), (self.recognition_model, SFACE_MODEL_URL)]


class ConfigError(Exception):
    pass


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"No config at {path}. Run `psort init` first.")
    data = tomllib.loads(path.read_text())
    try:
        paths = data["paths"]
        cluster = data.get("cluster", {})
        inbox_opts = data.get("inbox", {})
        events = data.get("events", {})
        faces = data.get("faces", {})
        return Config(
            inbox=Path(paths["inbox"]).expanduser(),
            library=Path(paths["library"]).expanduser(),
            outbox=Path(paths["outbox"]).expanduser(),
            state_dir=Path(paths.get("state_dir", DEFAULT_STATE_DIR)).expanduser(),
            videos_dir=Path(paths["videos"]).expanduser() if "videos" in paths else None,
            unsorted_dir=Path(paths["unsorted"]).expanduser() if "unsorted" in paths else None,
            highlights_dir=Path(paths["highlights"]).expanduser() if "highlights" in paths else None,
            burst_gap_seconds=float(cluster.get("burst_gap_seconds", 10.0)),
            settle_seconds=float(inbox_opts.get("settle_seconds", 120.0)),
            phash_threshold=int(cluster.get("phash_threshold", 10)),
            close_call_margin=float(cluster.get("close_call_margin", 0.05)),
            duplicate_gap_seconds=float(cluster.get("duplicate_gap_seconds", 1.0)),
            duplicate_phash_threshold=int(cluster.get("duplicate_phash_threshold", 2)),
            event_gap_hours=float(events.get("gap_hours", 3.0)),
            face_match_threshold=float(faces.get("match_threshold", 0.45)),
            face_cluster_threshold=float(faces.get("cluster_threshold", 0.5)),
            weights=Weights(**data.get("weights", {})),
        )
    except (KeyError, TypeError) as e:
        raise ConfigError(f"Invalid config {path}: {e}") from e


def write_default(path: Path, inbox: Path, library: Path, outbox: Path, state_dir: Path,
                  videos: Path | None = None) -> None:
    # json.dumps produces valid TOML basic strings for paths.
    q = lambda p: json.dumps(str(p))  # noqa: E731
    w = Weights()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""[paths]
inbox = {q(inbox)}
library = {q(library)}
outbox = {q(outbox)}
# Videos get the same year/day/event folders as the library, in their own tree.
videos = {q(videos or library.parent / "videos")}
# Every other file from the inbox (helper files, documents, unreadable files), under its original path.
unsorted = {q(library.parent / "unsorted_files")}
# Small copies of your favorites, kept in sync automatically (same folders and names as the library).
highlights = {q(library.parent / "highlights")}
state_dir = {q(state_dir)}

[inbox]
# Files changed within this many seconds are treated as still being copied in and left for the
# next run, so psort never records a half-copied file.
settle_seconds = 120

[cluster]
# Shots this close together in time AND this visually similar form one moment.
burst_gap_seconds = 10
# Perceptual-hash Hamming distance out of 64 bits; lower = stricter.
phash_threshold = 10
# Flag a moment for review when the runner-up scores within this fraction of the best.
close_call_margin = 0.05
# Visual duplicates (the same picture saved twice) must be this close in time and look.
# The best-quality copy stays; the others go to _duplicates/.
duplicate_gap_seconds = 1
duplicate_phash_threshold = 2

[events]
# A gap longer than this between photos starts a new suggested event.
gap_hours = 3

[faces]
# Cosine similarity needed to auto-tag a face as a named person (higher = stricter).
match_threshold = 0.45
# Similarity needed to group unnamed faces together.
cluster_threshold = 0.5

[weights]
# Best-shot score, compared only within a moment.
sharpness = {w.sharpness}
exposure = {w.exposure}
faces = {w.faces}
face_sharpness = {w.face_sharpness}
"""
    )

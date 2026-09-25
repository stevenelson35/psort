"""psort.toml loading and defaults (DESIGN.md §2)."""

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(os.environ.get("PSORT_CONFIG", Path.home() / ".config/psort/psort.toml"))
DEFAULT_STATE_DIR = Path.home() / ".local/share/psort"
FACE_MODEL_NAME = "face_detection_yunet_2023mar.onnx"
FACE_MODEL_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + FACE_MODEL_NAME
)


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
    burst_gap_seconds: float = 10.0
    phash_threshold: int = 10
    weights: Weights = field(default_factory=Weights)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "psort.db"

    @property
    def face_model(self) -> Path:
        return self.state_dir / "models" / FACE_MODEL_NAME


class ConfigError(Exception):
    pass


def load(path: Path) -> Config:
    if not path.exists():
        raise ConfigError(f"No config at {path}. Run `psort init` first.")
    data = tomllib.loads(path.read_text())
    try:
        paths = data["paths"]
        cluster = data.get("cluster", {})
        return Config(
            inbox=Path(paths["inbox"]).expanduser(),
            library=Path(paths["library"]).expanduser(),
            outbox=Path(paths["outbox"]).expanduser(),
            state_dir=Path(paths.get("state_dir", DEFAULT_STATE_DIR)).expanduser(),
            burst_gap_seconds=float(cluster.get("burst_gap_seconds", 10.0)),
            phash_threshold=int(cluster.get("phash_threshold", 10)),
            weights=Weights(**data.get("weights", {})),
        )
    except (KeyError, TypeError) as e:
        raise ConfigError(f"Invalid config {path}: {e}") from e


def write_default(path: Path, inbox: Path, library: Path, outbox: Path, state_dir: Path) -> None:
    # json.dumps produces valid TOML basic strings for paths.
    q = lambda p: json.dumps(str(p))  # noqa: E731
    w = Weights()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""[paths]
inbox = {q(inbox)}
library = {q(library)}
outbox = {q(outbox)}
state_dir = {q(state_dir)}

[cluster]
# Shots this close together in time AND this visually similar form one moment.
burst_gap_seconds = 10
# Perceptual-hash Hamming distance out of 64 bits; lower = stricter.
phash_threshold = 10

[weights]
# Best-shot score, compared only within a moment.
sharpness = {w.sharpness}
exposure = {w.exposure}
faces = {w.faces}
face_sharpness = {w.face_sharpness}
"""
    )

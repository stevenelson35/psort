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
    resolution: float = 0.1


@dataclass
class Config:
    inbox: Path
    library: Path
    outbox: Path
    state_dir: Path = DEFAULT_STATE_DIR
    videos_dir: Path | None = None  # default: a videos/ folder beside the library
    unsorted_dir: Path | None = None  # default: an unsorted_files/ folder beside the library
    highlights_dir: Path | None = None  # default: a highlights/ folder beside the library
    top_picks_dir: Path | None = None  # default: a top_picks/ folder beside the library
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
    inboxes: tuple[Path, ...] = ()

    @property
    def input_roots(self) -> tuple[Path, ...]:
        return self.inboxes or (self.inbox,)

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
    def top_picks(self) -> Path:
        return self.top_picks_dir or self.library.parent / "top_picks"

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


def missing_inboxes(cfg: Config) -> list[tuple[int, Path]]:
    """(1-based position, path) of configured inboxes that aren't there right now (drive unplugged,
    folder deleted). They're skipped, never dropped from the list: positions are part of every
    source key, so the entry must stay where it is."""
    return [(i + 1, root) for i, root in enumerate(cfg.input_roots) if not root.is_dir()]


def source_key(root_index: int, relative: Path) -> Path:
    """Keep root zero's historical keys; namespace later roots to avoid relative-path collisions."""
    marker = relative.parts[0] if relative.parts else ""
    if root_index == 0 and not marker.startswith("_psort_inbox_"):
        return relative
    return Path(f"_psort_inbox_{root_index}") / relative


def source_location(cfg: Config, stored: str | Path) -> tuple[Path, Path]:
    """Resolve a DB source key to its configured root and root-relative path."""
    key = Path(stored)
    parts = key.parts
    prefix = parts[0] if parts else ""
    marker = "_psort_inbox_"
    if prefix.startswith(marker) and prefix[len(marker):].isdigit():
        index = int(prefix[len(marker):])
        if index < len(cfg.input_roots) and len(parts) >= 2:
            return cfg.input_roots[index], Path(*parts[1:])
    return cfg.input_roots[0], key


def source_file(cfg: Config, stored: str | Path) -> Path:
    root, relative = source_location(cfg, stored)
    return root / relative


def source_display(cfg: Config, path: Path) -> Path:
    """Return an input path relative to whichever configured root contains it."""
    for root in cfg.input_roots:
        try:
            return path.relative_to(root)
        except ValueError:
            continue
    return path


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
        configured_inboxes = paths.get("inboxes")
        if configured_inboxes is not None and (not isinstance(configured_inboxes, list) or not configured_inboxes):
            raise ConfigError("paths.inboxes must be a non-empty array of directories.")
        inboxes = tuple(Path(p).expanduser() for p in configured_inboxes) if configured_inboxes is not None else ()
        inbox = inboxes[0] if inboxes else Path(paths["inbox"]).expanduser()
        return Config(
            inbox=inbox,
            library=Path(paths["library"]).expanduser(),
            outbox=Path(paths["outbox"]).expanduser(),
            inboxes=inboxes,
            state_dir=Path(paths.get("state_dir", DEFAULT_STATE_DIR)).expanduser(),
            videos_dir=Path(paths["videos"]).expanduser() if "videos" in paths else None,
            unsorted_dir=Path(paths["unsorted"]).expanduser() if "unsorted" in paths else None,
            highlights_dir=Path(paths["highlights"]).expanduser() if "highlights" in paths else None,
            top_picks_dir=Path(paths["top_picks"]).expanduser() if "top_picks" in paths else None,
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


def write_default(path: Path, inbox: Path | list[Path] | tuple[Path, ...], library: Path, outbox: Path, state_dir: Path,
                  videos: Path | None = None) -> None:
    # json.dumps produces valid TOML basic strings for paths.
    q = lambda p: json.dumps(str(p))  # noqa: E731
    inboxes = (inbox,) if isinstance(inbox, Path) else tuple(inbox)
    if not inboxes:
        raise ConfigError("At least one inbox directory is required.")
    w = Weights()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""[paths]
inboxes = [{", ".join(q(p) for p in inboxes)}]
library = {q(library)}
outbox = {q(outbox)}
# Videos get the same year/day/event folders as the library, in their own tree.
videos = {q(videos or library.parent / "videos")}
# Every other file from the inbox (helper files, documents, unreadable files), under its original path.
unsorted = {q(library.parent / "unsorted_files")}
# Small copies of your favorites, kept in sync automatically (same folders and names as the library).
highlights = {q(library.parent / "highlights")}
# Your very best photos (marked ◆ in the review UI), flat in one folder so they're easy to copy or show off.
top_picks = {q(library.parent / "top_picks")}
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
# Prefers more pixels when otherwise-similar shots differ in resolution (e.g. a screenshot or a
# resized copy mixed into the same burst).
resolution = {w.resolution}
"""
    )

import re
import sqlite3
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np

from conftest import save, scene
from psort.config import load
from psort.review import create_app
from psort.videos import mp4_creation_time


def make_video(path: Path, seconds: float = 2, fps: int = 10, fourcc: str = "mp4v", shade: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*fourcc), fps, (320, 240))
    for i in range(int(seconds * fps)):
        writer.write(np.full((240, 320, 3), (shade + i * 5) % 255, np.uint8))
    writer.release()
    return path


def video_files(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} if root.exists() else set()


def test_videos_go_to_parallel_tree(psort, tmp_path, sample_inbox):
    make_video(sample_inbox / "2026-phone-dump/VID_20260703_150000.mp4")
    make_video(sample_inbox / "old-backup/other.mp4", shade=50)  # different content: its own video
    (sample_inbox / "old-backup/VID_copy.mp4").write_bytes((sample_inbox / "2026-phone-dump/VID_20260703_150000.mp4").read_bytes())
    fargo = sample_inbox / "2015-05-fargo"
    make_video(fargo / "MOV05714.MPG", fps=25, fourcc="PIM1")
    (fargo / "MOV05714.THM").write_bytes(b"camera thumbnail")

    out = psort("run").output
    assert "2 exact duplicates" in out  # the sample's photo copy + VID_copy.mp4
    assert "Videos: 3 new" in out and "2 other files" in out  # the .THM and the sample's notes.txt
    videos = video_files(tmp_path / "videos")  # beside the library by default
    assert "2026/2026-07-03/20260703_150000.mp4" in videos
    assert "2015/2015-05_unknown-day/20150501_120000.mpg" in videos  # month from the folder name
    assert len(videos) == 3  # the exact copy wasn't duplicated

    out = psort("verify", "2015-05-fargo", "--all").output
    assert "safe to delete" in out and "video: 2015/2015-05_unknown-day" in out
    assert "camera preview" in out and "kept in unsorted_files/2015-05-fargo/MOV05714.THM" in out
    assert (tmp_path / "unsorted_files/2015-05-fargo/MOV05714.THM").read_bytes() == b"camera thumbnail"
    assert "Videos:            3" in psort("status").output

    # Idempotent.
    out = psort("run").output
    assert "videos:" not in out.lower().split("curate")[-1]


def test_event_names_apply_to_video_folders(psort, tmp_path, sample_inbox):
    make_video(sample_inbox / "2026-phone-dump/VID_20260703_150000.mp4")
    psort("run")
    psort("events", "name", "20260703_145633", "birthday-party")
    videos = video_files(tmp_path / "videos")
    assert "2026/2026-07-03_birthday-party/20260703_150000.mp4" in videos
    assert (tmp_path / "library/2026/2026-07-03_birthday-party").is_dir()  # same folder name in both trees


def test_live_photo_clips_kept_beside_photos_and_long_videos_kept(psort, tmp_path, sample_inbox):
    live = sample_inbox / "iphone"
    save(live / "IMG_5000.HEIC", scene(95), "2026:07:07 10:00:00", model="iPhone 15", make="Apple", fmt="HEIF")
    make_video(live / "IMG_5000.MOV", seconds=2)
    save(live / "IMG_5001.HEIC", scene(96), "2026:07:07 11:00:00", model="iPhone 15", make="Apple", fmt="HEIF")
    make_video(live / "IMG_5001.MOV", seconds=8, shade=100)  # too long for a Live Photo: a real video
    out = psort("run").output
    assert "Videos: 1 new, 2 Live Photo clip(s) kept beside their photos" in out  # + the sample's IMG_0009.MOV
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
    library = tmp_path / "library/2026/2026-07-07"
    assert sorted(p.name for p in library.iterdir()) == ["20260707_100000.heic", "20260707_100000.mov",
                                                         "20260707_110000.heic"]
    vids = video_files(tmp_path / "videos")  # the 8-second one is a real video (no date inside, so undated)
    assert len(vids) == 1 and next(iter(vids)).endswith(".mov")
    assert "safe to delete" in psort("verify", "iphone").output


def test_previously_skipped_videos_are_picked_up(psort, tmp_path, sample_inbox):
    make_video(sample_inbox / "2015-05-fargo/MOV05714.MPG", fps=25, fourcc="PIM1")
    psort("run")
    # Simulate a library from before videos were supported.
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    conn.execute("UPDATE sources SET status = 'skipped', reason = 'video (not supported)', sha256 = NULL "
                 "WHERE path LIKE '%.MPG'")
    conn.execute("DELETE FROM videos")
    conn.commit()
    out = psort("run").output
    assert "Videos: 1 new" in out


def test_mp4_creation_time():
    utc = datetime(2016, 4, 15, 17, 2, 12, tzinfo=timezone.utc)
    seconds = int((utc - datetime(1904, 1, 1, tzinfo=timezone.utc)).total_seconds())
    mvhd_payload = bytes([0, 0, 0, 0]) + struct.pack(">II", seconds, seconds) + b"\0" * 88
    mvhd = struct.pack(">I4s", 8 + len(mvhd_payload), b"mvhd") + mvhd_payload
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom\0\0\0\0"
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(ftyp + moov)
        f.flush()
        assert mp4_creation_time(Path(f.name)) == utc.astimezone().replace(tzinfo=None)
    with tempfile.NamedTemporaryFile(suffix=".mp4") as f:
        f.write(b"not an mp4 at all")
        f.flush()
        assert mp4_creation_time(Path(f.name)) is None


def test_review_ui_shows_videos(psort, tmp_path, sample_inbox):
    make_video(sample_inbox / "2026-phone-dump/VID_20260703_150000.mp4")
    make_video(sample_inbox / "2015-05-fargo/MOV05714.MPG", fps=25, fourcc="PIM1")
    psort("run")
    client = create_app(load(tmp_path / "psort.toml")).test_client()
    home = client.get("/").get_data(as_text=True)
    assert "🎬 1" in home and "May 2015" in home  # a video-only month folder still appears

    day = client.get("/folder/2026/2026-07-03").get_data(as_text=True)
    assert "1 video from this day" in day and "<video" in day
    sha = re.search(r'/video/([0-9a-f]{64})', day).group(1)
    assert client.get(f"/poster/{sha}.jpg").mimetype == "image/jpeg"
    ranged = client.get(f"/video/{sha}", headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206 and len(ranged.data) == 100

    month = client.get("/folder/2015/2015-05_unknown-day").get_data(as_text=True)
    assert "won't play in the browser" in month and "<video" not in month
    listing = client.get("/videos").get_data(as_text=True)
    assert listing.count('class="card video"') == 2


def test_thm_companion_supplies_the_date(psort, tmp_path, sample_inbox):
    from PIL import Image

    fargo = sample_inbox / "2015-05-fargo"
    make_video(fargo / "MOV05714.MPG", fps=25, fourcc="PIM1")
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2015:05:09 14:43:23"
    Image.new("RGB", (160, 120)).save(fargo / "MOV05714.THM", "JPEG", exif=exif.tobytes())
    psort("run")
    assert "2015/2015-05-09/20150509_144323.mpg" in video_files(tmp_path / "videos")


def test_thm_date_fixes_videos_already_filed_by_month(psort, tmp_path, sample_inbox):
    from PIL import Image

    fargo = sample_inbox / "2015-05-fargo"
    make_video(fargo / "MOV05714.MPG", fps=25, fourcc="PIM1")
    psort("run")
    assert "2015/2015-05_unknown-day/20150501_120000.mpg" in video_files(tmp_path / "videos")
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = "2015:05:09 14:43:23"
    Image.new("RGB", (160, 120)).save(fargo / "MOV05714.THM", "JPEG", exif=exif.tobytes())
    psort("run")
    files = video_files(tmp_path / "videos")
    assert "2015/2015-05-09/20150509_144323.mpg" in files and not any("unknown-day" in f for f in files)


def test_empty_files_and_unfinished_recordings(psort, tmp_path, sample_inbox):
    phone = sample_inbox / "zfold"
    phone.mkdir(parents=True, exist_ok=True)
    (phone / ".temp-20250726_133028002_BACK.mp4").write_bytes(b"")  # an abandoned, empty recording
    make_video(phone / ".temp-20250727_090000001_BACK.mp4")  # unfinished but with content
    (phone / "IMG_7000.JPG").write_bytes(b"")
    save(phone / "IMG_7001.HEIC", scene(97), "2026:07:08 10:00:00", model="iPhone 15", make="Apple", fmt="HEIF")
    (phone / "IMG_7001.MOV").write_bytes(b"")  # an empty Live Photo clip

    out = psort("run").output
    assert "Empty files: 3 skipped" in out
    assert not any(f.endswith((".temp-20250726_133028002_BACK.mp4",)) for f in video_files(tmp_path / "videos"))
    assert not any(p.stat().st_size == 0 for p in (tmp_path / "videos").rglob("*") if p.is_file())
    assert not (tmp_path / "library/2026/2026-07-08/20260708_100000.mov").exists()  # no empty clip beside the photo
    # The unfinished recording with content is kept, but not among the videos.
    assert (tmp_path / "unsorted_files/zfold/.temp-20250727_090000001_BACK.mp4").exists()
    verify = psort("verify", "zfold", "--all").output
    assert "safe to delete" in verify and "empty file (0 bytes)" in verify and "unfinished recording" in verify


def test_empty_copies_from_older_runs_are_cleaned_up(psort, tmp_path, sample_inbox):
    phone = sample_inbox / "zfold"
    phone.mkdir(parents=True, exist_ok=True)
    (phone / "VID_20250726_133028.mp4").write_bytes(b"")
    psort("run")
    # Simulate a library from before empty files were skipped: the empty video was copied in.
    conn = sqlite3.connect(tmp_path / "state/psort.db")
    empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    conn.execute("UPDATE sources SET status = 'video', reason = NULL WHERE sha256 = ?", (empty,))
    conn.execute("INSERT INTO videos (sha256, ext, taken_at, date_source, name, library_path) VALUES "
                 "(?, '.mp4', '2025-07-26T13:30:28', 'filename', '20250726_133028', '2025/2025-07-26/20250726_133028.mp4')", (empty,))
    conn.commit()
    copy = tmp_path / "videos/2025/2025-07-26/20250726_133028.mp4"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(b"")

    out = psort("run").output
    assert "removed the copies of 1 empty" in out
    assert not copy.exists() and not copy.parent.exists()
    assert conn.execute("SELECT COUNT(*) FROM videos WHERE sha256 = ?", (empty,)).fetchone()[0] == 0
    assert conn.execute("SELECT status FROM sources WHERE sha256 = ?", (empty,)).fetchone()[0] == "junk"
    assert "safe to delete" in psort("verify", "zfold").output

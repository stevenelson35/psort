"""Photos that tripped up Pillow: odd EXIF (Windows Phone 'Rich Capture') and very large images."""

from pathlib import Path

import pytest
from PIL import Image

from conftest import save, scene
from psort.export import render
from psort.imaging import analyze


@pytest.fixture
def sideways(tmp_path) -> Path:
    return save(tmp_path / "WP_20170821_15_00_23_Rich.jpg", scene(400), "2017:08:21 15:00:23",
                model="Lumia 950 XL", make="Microsoft", orientation=6)


def test_rotation_never_re_encodes_exif(sideways, monkeypatch):
    # Pillow's exif_transpose re-encodes EXIF, which crashes on some real-world metadata.
    def boom(self, *args, **kwargs):
        raise TypeError("The fill character must be a unicode character, not bytes")

    monkeypatch.setattr(Image.Exif, "tobytes", boom)
    a = analyze(sideways)
    assert (a.width, a.height) == (600, 800) and a.exif_datetime == "2017:08:21 15:00:23"
    out = sideways.with_name("out.jpg")
    render(sideways, out)  # exports even when the metadata can't be re-encoded
    with Image.open(out) as im:
        assert im.size == (600, 800)


def test_previously_unreadable_file_is_retried_and_moved_out_of_unsorted(psort, tmp_path, sample_inbox, monkeypatch):
    from psort import ingest

    path = save(sample_inbox / "wp/WP_20170821_15_00_23_Rich.jpg", scene(401), "2017:08:21 15:00:23",
                model="Lumia 950 XL", make="Microsoft")
    real = ingest.analyze
    def fails_on_rich(p, *args, **kwargs):
        if Path(p).name == path.name:
            raise TypeError("odd EXIF")
        return real(p, *args, **kwargs)

    monkeypatch.setattr(ingest, "analyze", fails_on_rich)
    assert "1 unreadable" in psort("run").output
    assert (tmp_path / "unsorted_files/wp/WP_20170821_15_00_23_Rich.jpg").exists()

    monkeypatch.setattr(ingest, "analyze", real)  # a newer psort can read it
    out = psort("run").output
    assert "1 previously unreadable file(s) read fine now" in out
    assert (tmp_path / "library/2017/2017-08-21/20170821_150023.jpg").exists()
    assert not (tmp_path / "unsorted_files/wp").exists()


def test_large_images_are_allowed():
    assert Image.MAX_IMAGE_PIXELS >= 300_000_000  # set when psort is imported

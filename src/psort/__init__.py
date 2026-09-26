"""psort — local photo curation. See DESIGN.md."""

from PIL import Image

# Pillow warns above ~89 MP and refuses above ~179 MP to guard against malicious downloads.
# These are your own photos (panoramas, posters), so allow up to 300 MP (about 1 GB to decode).
Image.MAX_IMAGE_PIXELS = 300_000_000

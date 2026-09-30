"""
Magnified crops of an ECG photo, because one whole-sheet view does not have
the pixels to see a bundle branch block.

The model was missing right bundle branch block and S1Q3T3 on a tracing
that plainly had both. The first fix was a prompt fix -- the output spec
never asked about QRS shape or cross-lead patterns, so they were never
looked for (see ecg_lab_ai._PATTERN_CHECKLIST). With the checklist in
place the read came back explicitly asserting "no rSR' pattern in V1-V2"
and "No S1Q3T3". So it was looking, and still not seeing, which is a
different problem and this module is the answer to it.

The arithmetic: a 12-lead sheet is about 250 mm wide, and images are
downscaled so the long edge fits roughly 1568 px, giving about 6 px/mm.
At 25 mm/s one small box (40 ms) is 1 mm. RBBB's defining feature is a
terminal R' roughly 40-60 ms wide, so on the full sheet it occupies about
6-9 pixels -- and that is BEFORE JPEG compression of a phone photo, which
eats exactly this kind of small high-contrast detail. The same goes for
the small Q in lead III and the S wave in lead I that make up S1Q3T3.
Those features are not being overlooked; they are not in the data.

So the tracing is sent several times: the whole sheet for layout and
rhythm, plus overlapping quadrants, each scaled to the cap in its own
right rather than as a quarter of a bigger picture.

That buys two different things depending on what arrived, and it is worth
being precise about which:

- A LARGE source (an ECG sent as a file, a scan, a screenshot) is being
  downscaled before the model sees it, and cropping recovers detail that
  the downscale was destroying. A quadrant spans a little over half the
  sheet, so it lands at about 1.8x the pixels per mm of paper -- carrying
  a 40 ms deflection from ~6 px to ~11 px. This is a real information
  gain.
- A SMALL source has already lost that detail before it reached us, and
  no amount of cropping brings it back. Telegram compresses photos, and
  `message.photo[-1]` is typically capped around 1280 px on the long edge,
  so the ordinary path through the bot lands here. The quadrants are still
  upscaled to the cap, which adds no information but does change how the
  model's own patching resolves fine detail: a 6 px deflection can fall
  inside a single patch and be averaged out of existence, where the same
  deflection spanning 15 px cannot. That helps, but far less, and the
  actual remedy is to receive the tracing uncompressed -- which is why the
  prompt asking for it now says to send it as a file.

The grid is fixed at 2x2 rather than chosen from the image size. Finer
grids keep helping, but each one multiplies the image tokens on a read
that already makes two passes, and five images per pass is the budget this
is worth.
"""

import io
import logging

logger = logging.getLogger(__name__)

try:
    from PIL import Image
except Exception:  # pragma: no cover - Pillow is in requirements.txt
    Image = None

# Images whose long edge exceeds this are downscaled to fit it before the
# model ever sees them, so this is the real resolution ceiling of a single
# image block -- and the reason cropping recovers anything at all.
API_MAX_EDGE = 1568

# Below this a quadrant is a thumbnail, and blowing one up produces a blur
# the model will read confident nonsense off. Above it the crops are worth
# making even when no detail is recoverable, for the patching reason in the
# module docstring.
MIN_EDGE_TO_TILE = 700

# Quadrants overlap so that a feature sitting on a seam is whole in at
# least one view. Without it a cut straight down the middle of the sheet
# can land between the limb and precordial columns -- or worse, through the
# middle of a QRS -- and the one lead that mattered is in neither half.
# It is paid for in magnification: the wider the overlap, the more paper a
# quadrant spans and the less it is enlarged (0.12 costs about 5% of the
# gain). Cheap insurance at this size, but not free.
OVERLAP = 0.12

# Reading order, matching the labels handed to the model.
_QUADRANTS = [
    ("top-left quadrant", 0, 0),
    ("top-right quadrant", 1, 0),
    ("bottom-left quadrant", 0, 1),
    ("bottom-right quadrant", 1, 1),
]

WHOLE_SHEET_LABEL = "the complete sheet"


# Image tokens are computed from an image's DIMENSIONS, not its file size,
# so quality above the default is free in tokens and costs only payload.
# Worth spending on the quadrants: a QRS trace is a thin high-contrast line
# and its terminal slur is exactly the low-amplitude edge detail JPEG
# discards first -- and that slur is what decides whether a QRS measures
# 100 ms or 120 ms. The full sheet stays lower: it is read for layout and
# R-R spacing, neither of which turns on a sub-millimetre edge.
_QUADRANT_QUALITY = 95
_WHOLE_SHEET_QUALITY = 90


def _encode(img, quality: int = _WHOLE_SHEET_QUALITY) -> bytes:
    """JPEG: these are photographs of paper, so PNG would multiply the
    payload for no visible gain on a continuous-tone image."""
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _scale_to_cap(img, upscale: bool):
    """
    Bring an image to the resolution cap.

    Downscaling happens here rather than in transit: the end size is the
    same, but with a known-good filter and a far smaller request body.

    Upscaling is only for the quadrants, and only ever to the cap -- never
    past it, since anything beyond would just be downscaled again on the
    way out. It creates no detail; it gives detail that is already there
    enough pixels to survive the model's patching.
    """
    longest = max(img.size)
    if longest == API_MAX_EDGE or (longest < API_MAX_EDGE and not upscale):
        return img
    scale = API_MAX_EDGE / longest
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)


def build_views(image_bytes: bytes) -> list[tuple[str, bytes, str]]:
    """
    [(label, jpeg_bytes, "image/jpeg"), ...] -- the whole sheet first, then
    the magnified quadrants.

    Returns [] when tiling is not worth doing or not possible, and the
    caller then sends the original image exactly as before. Every failure
    path returns [] rather than raising: a tracing read at the old
    resolution is a worse read, but a tracing that cannot be read at all
    because the cropping blew up is a broken feature.
    """
    if Image is None:
        logger.warning("Pillow unavailable -- ECG sent as a single un-magnified image")
        return []
    try:
        with Image.open(io.BytesIO(image_bytes)) as src:
            src.load()
            # EXIF orientation: a phone photo held sideways reports its
            # pixels rotated, and quadrants cut from the unrotated frame
            # would not correspond to the halves of the sheet the model is
            # told they are.
            try:
                from PIL import ImageOps

                src = ImageOps.exif_transpose(src)
            except Exception:
                pass

            width, height = src.size
            if max(width, height) < MIN_EDGE_TO_TILE:
                # Too small to crop usefully -- see MIN_EDGE_TO_TILE.
                return []

            # The whole sheet is never upscaled: it is there for layout and
            # R-R spacing, both of which are legible at any size, and
            # enlarging it would cost a full image's tokens for nothing.
            views = [(WHOLE_SHEET_LABEL, _encode(_scale_to_cap(src.copy(), upscale=False)), "image/jpeg")]

            pad_x, pad_y = int(width * OVERLAP / 2), int(height * OVERLAP / 2)
            for label, col, row in _QUADRANTS:
                left = max(0, col * width // 2 - pad_x)
                top = max(0, row * height // 2 - pad_y)
                right = min(width, (col + 1) * width // 2 + pad_x)
                bottom = min(height, (row + 1) * height // 2 + pad_y)
                crop = src.crop((left, top, right, bottom))
                views.append(
                    (label, _encode(_scale_to_cap(crop, upscale=True), _QUADRANT_QUALITY), "image/jpeg")
                )
            return views
    except Image.UnidentifiedImageError:
        # A file the user sent that isn't really an image. Not a bug here,
        # and the API will reject it on its own terms -- so no traceback.
        logger.warning("ECG image could not be decoded for tiling -- sending it through as-is")
        return []
    except Exception:
        logger.exception("ECG tiling failed (non-fatal, falling back to the single full-sheet image)")
        return []

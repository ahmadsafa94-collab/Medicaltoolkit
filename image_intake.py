"""
Turning whatever the user actually sent into something the model can read.

The vision API accepts four formats: JPEG, PNG, GIF, WebP. Devices do not
restrict themselves to four formats. This became a live problem the moment
the ECG prompt started asking for the tracing as a FILE rather than a
photo (see ecg_lab_flow) -- which was the right thing to ask for, because
Telegram re-compresses anything sent as a photo and destroys exactly the
fine detail an ECG read depends on. But a photo arrives as Telegram's own
re-encoded JPEG, whereas a FILE arrives in whatever the device stores:
JPEG 2000 (.jpf, .jp2) from some scanners and hospital systems, HEIC from
iPhones, TIFF from flatbed scanners, BMP from Windows tools, .jfif from
Windows browsers. So the advice that improved the read also broke the
upload for anyone whose device does not store plain JPEG.

The rule here is that THE DECODER IS THE ARBITER, not the filename and not
the MIME type Telegram guessed. A MIME type is a claim about a file made
by whoever named it: Telegram sends application/octet-stream for anything
it does not recognise, .jfif is JPEG with the wrong extension, and a file
called .png is routinely a JPEG. Attempting the decode answers the actual
question -- can this be read at all -- and everything that decodes is then
re-encoded into one of the four formats the API takes.

Anything already in a supported format is passed through untouched rather
than round-tripped, so the common case adds no generation loss and no
work. The conversion target is PNG, because it is lossless and because
whatever comes out of here may be cropped and re-encoded downstream by
ecg_tiles: a lossy intake followed by a lossy tile would compress the same
image twice, and the artifacts that eats are the same 1 mm deflections the
tiling exists to preserve. PNG loses that argument only on size, so an
oversized PNG falls back to high-quality JPEG.
"""

import io
import logging

# Shared ceiling: ecg_tiles crops against the same number, since images are
# downscaled to fit it before the model sees them either way.
from ecg_tiles import API_MAX_EDGE

logger = logging.getLogger(__name__)

try:
    from PIL import Image

    # iPhones store HEIC, and "send it as a file" makes that the single
    # most likely non-JPEG upload the bot will see. Optional because it is
    # a separate wheel with its own native dependency: without it HEIC
    # simply reports as unreadable, which is the same outcome as before.
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:
        logger.info("pillow-heif unavailable -- HEIC/HEIF uploads will not be accepted")
except Exception:  # pragma: no cover - Pillow is in requirements.txt
    Image = None

# What the vision API itself will take. Everything else has to be converted
# before it gets there.
API_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


# Pillow's format name -> the media type to declare for it.
_FORMAT_MEDIA_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "GIF": "image/gif",
    "WEBP": "image/webp",
}

# The API rejects a single image over 5 MB, and base64 inflates bytes by a
# third on the way out, so the real ceiling for encoded bytes is lower than
# 5 MB. A lossless scan of a full ECG sheet passes this easily.
_MAX_ENCODED_BYTES = 3_500_000

# Tried in order when PNG is too big. Resolution is defended ahead of
# fidelity on purpose: ecg_tiles crops magnified quadrants out of whatever
# comes from here, so pixels lost at intake are pixels no tiling can get
# back, whereas a q75 JPEG of a full-resolution sheet still shows a 1 mm
# deflection. Downscaling is the last resort, not the first.
_JPEG_QUALITY_LADDER = (92, 85, 78, 70)


class UnsupportedImage(Exception):
    """Raised with a message meant to be shown to the user as-is."""


# EXIF orientation values other than these mean the stored pixels are
# rotated or mirrored relative to how the photo should be shown.
_UPRIGHT_ORIENTATIONS = (0, 1)


def _decode(raw: bytes):
    """(upright image, Pillow's format name, whether EXIF said it was rotated)."""
    img = Image.open(io.BytesIO(raw))
    img.load()
    detected = (img.format or "").upper()
    rotated = False
    try:
        from PIL import ImageOps

        # A phone held sideways stores its pixels rotated with an EXIF tag
        # saying so. It has to be applied before anything reads the image:
        # a sideways ECG is not a slightly worse read, it is a nonsense one
        # -- lead I is not where lead I should be, so the axis, the
        # cross-lead patterns and the lead-by-lead morphology are all wrong.
        rotated = img.getexif().get(274, 1) not in _UPRIGHT_ORIENTATIONS
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img, detected, rotated


def normalize(raw: bytes, media_type: str | None = None, file_name: str | None = None) -> tuple[bytes, str]:
    """
    (image_bytes, media_type) that the vision API will accept.

    Raises UnsupportedImage, with a message written for the user, when the
    bytes cannot be decoded as an image at all.
    """
    label = (file_name or media_type or "that file").strip()

    if Image is None:  # pragma: no cover
        # No decoder available: the only safe move is to pass through what
        # was already declared as a supported type and refuse the rest.
        if media_type in API_MEDIA_TYPES:
            return raw, media_type
        raise UnsupportedImage(
            "Couldn't read that image on the server. Please send it as a JPG or PNG."
        )

    if not raw:
        raise UnsupportedImage("That file is empty. Please send the image again.")

    try:
        img, detected, rotated = _decode(raw)
    except Exception:
        logger.info("Undecodable upload rejected (declared=%s, name=%s)", media_type, file_name)
        raise UnsupportedImage(
            f"Couldn't read {label} as an image. Send the ECG or report as a JPG, PNG, WebP, "
            "JPEG 2000 (.jp2/.jpf), TIFF, BMP or HEIC file -- or as an ordinary photo, which "
            "always works but is compressed by Telegram and so less reliable for fine detail."
        )

    with img:
        # A rotated original is re-encoded whatever format it came in, so
        # that the uprighted pixels are what gets sent. Passing the raw
        # bytes through here would hand the API a sideways tracing and rely
        # on it honouring an EXIF tag, which is not worth betting a read on.
        if detected in _FORMAT_MEDIA_TYPES and not rotated:
            # Already something the API takes. If the declared type agrees
            # with what it actually is, hand it straight through
            # unre-encoded; if not, stop believing the label rather than
            # round-tripping the pixels (a .jfif called
            # application/octet-stream, a JPEG named .png).
            corrected = _FORMAT_MEDIA_TYPES[detected]
            if media_type != corrected:
                logger.info("Corrected declared media type %s -> %s for %s", media_type, corrected, label)
            return raw, corrected

        return _convert(img, detected, label)


def _encode(img, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format=fmt, **kw)
    return buf.getvalue()


def _convert(img, detected: str, label: str) -> tuple[bytes, str]:
    """
    Re-encode into a format the API takes, staying under the size ceiling.

    PNG first because it is lossless and the output may be cropped and
    re-encoded again downstream. Failing that, JPEG down the quality
    ladder at full resolution, and only if even the bottom of the ladder is
    too large does the image get downscaled -- at which point there is no
    alternative, and the ceiling is the same one the API would impose
    anyway.
    """
    converted = img.convert("RGBA" if "A" in img.getbands() else "RGB")

    data = _encode(converted, "PNG", optimize=True)
    if len(data) <= _MAX_ENCODED_BYTES:
        logger.info("Converted %s (%s) to PNG for the vision API", label, detected or "unknown")
        return data, "image/png"

    rgb = converted.convert("RGB")
    for quality in _JPEG_QUALITY_LADDER:
        data = _encode(rgb, "JPEG", quality=quality, optimize=True)
        if len(data) <= _MAX_ENCODED_BYTES:
            logger.info(
                "Converted %s (%s) to JPEG q%d, %d bytes (PNG was too large)",
                label, detected or "unknown", quality, len(data),
            )
            return data, "image/jpeg"

    # Pathological input (a noisy full-bleed scan). Nothing is gained by
    # keeping pixels we cannot send, and the API's own cap is where they
    # would be lost regardless.
    longest = max(rgb.size)
    if longest > API_MAX_EDGE:
        scale = API_MAX_EDGE / longest
        rgb = rgb.resize((max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))), Image.LANCZOS)
    data = _encode(rgb, "JPEG", quality=_JPEG_QUALITY_LADDER[-1], optimize=True)
    logger.warning(
        "Downscaled %s (%s) to %dx%d to fit the size limit -- fine detail may be lost",
        label, detected or "unknown", rgb.width, rgb.height,
    )
    return data, "image/jpeg"

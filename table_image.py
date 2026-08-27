"""
Render a preserved block of table-like FDA label text as a clean PNG image,
so a Telegram message shows a readable picture instead of a wall of
run-on text.

Deliberately does NOT try to parse the block into rows/columns -- FDA
label tables get flattened to plain text with inconsistent conventions
across manufacturers (see drug_lookup._extract_structured_blocks), and
guessing wrong at column boundaries could silently misalign a dosing
number. Instead this renders the block exactly as extracted, in a
monospace font, so whatever space-alignment was present in the original
text lines back up visually on its own -- no re-parsing, no risk of
transposing a value.

The font is bundled in fonts/ (DejaVu Sans Mono, redistributed under its
own permissive license -- see fonts/LICENSE.txt) rather than relying on
whatever fonts happen to be installed on the deploy host, so rendering
looks the same locally and on Railway.
"""

import io
import os
import re

from PIL import Image, ImageDraw, ImageFont

# DejaVu Sans Mono (like most non-emoji fonts) has no emoji glyphs -- drawing
# one produces a "tofu" box instead. The FIELDS emoji (💉🎯🚫⚠️ etc.) are meant
# for Telegram's own text rendering (the photo caption), not this PNG, so
# strip them from any title drawn INTO the image.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"
    "]+",
    flags=re.UNICODE,
)


def _strip_emoji(text: str) -> str:
    return re.sub(r"\s+", " ", _EMOJI_RE.sub("", text)).strip()

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_REGULAR_FONT_PATH = os.path.join(_FONT_DIR, "DejaVuSansMono.ttf")
_BOLD_FONT_PATH = os.path.join(_FONT_DIR, "DejaVuSansMono-Bold.ttf")

_MAX_CONTENT_WIDTH = 1400  # px, before we start shrinking the font to fit
_MIN_FONT_SIZE = 14
_MAX_FONT_SIZE = 28
_PADDING = 24
_LINE_SPACING = 8

_BG_COLOR = (255, 255, 255)
_TITLE_BG_COLOR = (33, 47, 66)
_TITLE_TEXT_COLOR = (255, 255, 255)
_BODY_TEXT_COLOR = (25, 25, 25)
_BORDER_COLOR = (210, 214, 220)


class TableImageError(Exception):
    pass


def _load_font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError as e:
        # The bundled font should always be present, but if it's ever
        # missing (e.g. stripped from a deploy artifact), fall back to
        # PIL's built-in font rather than crashing the whole lookup --
        # ugly output beats a broken feature.
        import logging

        logging.getLogger(__name__).warning("Could not load font %s (%s), using PIL default", path, e)
        return ImageFont.load_default()


def render_table_image(text_block: str, title: str | None = None) -> bytes:
    """
    Render `text_block` verbatim, in a monospace font, as a PNG. `title`
    (if given) is drawn as a bold header bar above the block.
    Returns PNG bytes. Raises TableImageError if the block is empty.
    """
    lines = [ln.expandtabs(4).rstrip() for ln in text_block.strip("\n").split("\n")]
    lines = [ln for ln in lines if ln.strip()]
    if not lines:
        raise TableImageError("Nothing to render: table block was empty after cleanup.")

    if title:
        title = _strip_emoji(title) or None

    # Shrink the font until the widest line fits within _MAX_CONTENT_WIDTH,
    # rather than wrapping lines -- wrapping a table row would break the
    # exact alignment we're relying on to reproduce the table visually.
    font_size = _MAX_FONT_SIZE
    font = _load_font(_REGULAR_FONT_PATH, font_size)
    while font_size > _MIN_FONT_SIZE:
        widest = max(font.getlength(ln) for ln in lines)
        if widest + _PADDING * 2 <= _MAX_CONTENT_WIDTH:
            break
        font_size -= 2
        font = _load_font(_REGULAR_FONT_PATH, font_size)

    bold_font = _load_font(_BOLD_FONT_PATH, font_size + 2)

    ascent, descent = font.getmetrics()
    line_height = ascent + descent + _LINE_SPACING

    widest_line = max(font.getlength(ln) for ln in lines)
    img_width = int(widest_line) + _PADDING * 2

    title_height = 0
    if title:
        t_ascent, t_descent = bold_font.getmetrics()
        title_height = t_ascent + t_descent + _PADDING
        title_width = int(bold_font.getlength(title)) + _PADDING * 2
        img_width = max(img_width, title_width)

    img_width = max(img_width, 300)
    img_height = title_height + len(lines) * line_height + _PADDING * 2

    img = Image.new("RGB", (img_width, img_height), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    y = 0
    if title:
        draw.rectangle([0, 0, img_width, title_height], fill=_TITLE_BG_COLOR)
        draw.text((_PADDING, _PADDING // 2), title, font=bold_font, fill=_TITLE_TEXT_COLOR)
        y = title_height

    y += _PADDING // 2
    for ln in lines:
        draw.text((_PADDING, y), ln, font=font, fill=_BODY_TEXT_COLOR)
        y += line_height

    draw.rectangle([0, 0, img_width - 1, img_height - 1], outline=_BORDER_COLOR, width=2)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

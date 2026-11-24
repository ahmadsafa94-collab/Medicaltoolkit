"""
Renders a plain-text block (with light **bold** / *bold* / _italic_ markup,
"- " bullets and "# " headings) into a downloadable PDF -- drug references,
summaries and AI answers a student wants to keep.

Deliberately dumb: this is a formatting step over text that's already been
produced elsewhere -- it does not re-fetch, re-summarize, or otherwise touch
the underlying content, so nothing here can introduce a new factual error
into what gets exported.

Persian / Arabic: reportlab draws glyphs left to right and knows nothing
about joining Arabic-script letters, so text in those scripts came out as
disconnected, reversed letters (or empty boxes in the old base font). Each
line now gets the direction of its dominant script -- the same rule the
mini app uses (webapp/app.js richText) -- and a right-to-left line is
wrapped in LOGICAL order first, then each wrapped piece is shaped
(arabic_reshaper) and reordered for display (python-bidi). Wrapping after
reordering would put the end of a sentence on its first line. The font is
Vazirmatn (SIL Open Font License, Fonts/Vazirmatn-OFL.txt), which covers
Latin and Arabic script alike.
"""

import html
import io
import os
import re

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from table_image import _strip_emoji  # no emoji glyphs in the font -- would render as tofu boxes otherwise

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Fonts")
_FONT, _FONT_BOLD = "Vazirmatn", "Vazirmatn-Bold"
pdfmetrics.registerFont(TTFont(_FONT, os.path.join(_FONT_DIR, "Vazirmatn-Regular.ttf")))
pdfmetrics.registerFont(TTFont(_FONT_BOLD, os.path.join(_FONT_DIR, "Vazirmatn-Bold.ttf")))
pdfmetrics.registerFontFamily(_FONT, normal=_FONT, bold=_FONT_BOLD, italic=_FONT, boldItalic=_FONT_BOLD)

_RTL_RE = re.compile(r"[֐-ࣿיִ-﷿ﹰ-﻿]")
_LTR_RE = re.compile(r"[A-Za-zÀ-ɏЀ-ӿ]")
_BOLD2_RE = re.compile(r"\*\*(.+?)\*\*")
_BOLD_RE = re.compile(r"\*(.+?)\*")
_ITALIC_RE = re.compile(r"(?<!\w)_(.+?)_(?!\w)")
_HEADING_RE = re.compile(r"^\s*#{1,4}\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*•]\s+(.*)$")

_PAGE = letter
_MARGIN = 0.75 * inch
_BODY_SIZE = 10.5
_LEADING = 15


def is_rtl(text: str) -> bool:
    """True when right-to-left letters outnumber Latin/Cyrillic ones (ties go right-to-left)."""
    rtl = len(_RTL_RE.findall(text))
    return rtl > 0 and rtl >= len(_LTR_RE.findall(text))


def _ltr_markup(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = _BOLD2_RE.sub(r"<b>\1</b>", escaped)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC_RE.sub(r"<i>\1</i>", escaped)
    return escaped.replace("**", "")


def _wrap_logical(text: str, font: str, size: float, width: float, base_dir: str) -> list[str]:
    """Greedy word wrap on the LOGICAL (typed) order of the text, measured in the target font."""
    lines, current = [], ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if current and stringWidth(_shape(candidate, base_dir), font, size) > width:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _shape(text: str, base_dir: str) -> str:
    """
    Joined Arabic-script letters, in visual (display) order. base_dir is the
    line's direction ("R"/"L"), set explicitly: left to guess, the bidi
    algorithm takes it from the FIRST letter, so "• SSRI ها خط اول..."
    would be laid out as a left-to-right line.
    """
    return get_display(arabic_reshaper.reshape(text), base_dir=base_dir)


def _bidi_paragraphs(text: str, style: ParagraphStyle, width: float, bold: bool, base_dir: str) -> list:
    """A line containing Arabic-script text, wrapped logically, then shaped and reordered piece by piece."""
    font = _FONT_BOLD if bold else _FONT
    plain = text.replace("**", "")
    out = []
    for piece in _wrap_logical(plain, font, style.fontSize, width, base_dir):
        markup = html.escape(_shape(piece, base_dir), quote=False)
        out.append(Paragraph(f"<b>{markup}</b>" if bold else markup, style))
    return out


def generate_text_pdf(title: str, body_text: str) -> bytes:
    """
    Render `title` as a heading followed by `body_text` (one paragraph per
    non-blank line, blank lines become spacing) as a paginated PDF. Returns
    the PDF file content as bytes, ready to send as a document.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=_PAGE, topMargin=_MARGIN, bottomMargin=_MARGIN,
        leftMargin=_MARGIN, rightMargin=_MARGIN, title=_strip_emoji(title),
    )
    # The page frame keeps 6 pt of padding on each side, plus a little slack
    # so a wrapped line can never be a hair too wide and wrap again.
    width = _PAGE[0] - 2 * _MARGIN - 16
    base = getSampleStyleSheet()
    body_ltr = ParagraphStyle("BodyL", parent=base["BodyText"], fontName=_FONT, fontSize=_BODY_SIZE, leading=_LEADING, alignment=TA_LEFT)
    body_rtl = ParagraphStyle("BodyR", parent=body_ltr, alignment=TA_RIGHT)
    title_ltr = ParagraphStyle("TitleL", parent=base["Title"], fontName=_FONT_BOLD, fontSize=17, leading=22)
    title_rtl = ParagraphStyle("TitleR", parent=title_ltr)  # centred either way

    title = _strip_emoji(title).strip() or "Export"
    if _RTL_RE.search(title):
        story = _bidi_paragraphs(title, title_rtl, width, True, "R" if is_rtl(title) else "L")
    else:
        story = [Paragraph(html.escape(title, quote=False), title_ltr)]
    story.append(Spacer(1, 14))

    for raw in body_text.split("\n"):
        line = _strip_emoji(raw).rstrip()
        if not line.strip():
            story.append(Spacer(1, 6))
            continue
        bold = False
        heading = _HEADING_RE.match(line)
        bullet = _BULLET_RE.match(line)
        if heading:
            line, bold = heading.group(1), True
        elif bullet:
            line = "• " + bullet.group(2)
        stripped = line.strip()
        if stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
            line, bold = stripped[2:-2], True

        if is_rtl(line):
            story.extend(_bidi_paragraphs(line, body_rtl, width, bold, "R"))
        elif _RTL_RE.search(line):
            # Mostly English with some Persian/Arabic words: a left-to-right
            # line, but those words still need joining and reordering.
            story.extend(_bidi_paragraphs(line, body_ltr, width, bold, "L"))
        else:
            markup = _ltr_markup(line)
            story.append(Paragraph(f"<b>{markup}</b>" if bold else markup, body_ltr))

    doc.build(story)
    return buffer.getvalue()

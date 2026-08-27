"""
Renders a plain-text (with our own light *bold*/_italic_ markup) block into a
downloadable PDF, e.g. so a student can save a full drug reference for
offline reading instead of scrolling a long Telegram message.

Deliberately dumb: this is a formatting step over text that's already been
produced elsewhere (drug_lookup.py) -- it does not re-fetch, re-summarize,
or otherwise touch the underlying content, so nothing here can introduce a
new factual error into what gets exported.
"""

import html
import io
import re

from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

from table_image import _strip_emoji  # PDF base fonts have no emoji glyphs -- would render as tofu boxes otherwise

_BOLD_RE = re.compile(r"\*(.+?)\*")
_ITALIC_RE = re.compile(r"_(.+?)_")


def _line_to_markup(line: str) -> str:
    """Strip emoji (no glyphs in the PDF's base font), escape for reportlab's mini-HTML markup, then translate our own *bold*/_italic_ into <b>/<i>."""
    line = _strip_emoji(line)
    escaped = html.escape(line, quote=False)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC_RE.sub(r"<i>\1</i>", escaped)
    return escaped


def generate_text_pdf(title: str, body_text: str) -> bytes:
    """
    Render `title` as a heading followed by `body_text` (one paragraph per
    non-blank line, blank lines become spacing) as a paginated PDF. Returns
    the PDF file content as bytes, ready to send as a document.
    """
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        title=title,
    )
    styles = getSampleStyleSheet()

    story = [Paragraph(html.escape(_strip_emoji(title)), styles["Title"]), Spacer(1, 14)]
    for line in body_text.split("\n"):
        if not line.strip():
            story.append(Spacer(1, 6))
            continue
        markup = _line_to_markup(line)
        if not markup.strip():
            # the whole line was emoji (e.g. a standalone "📊" marker) -- nothing left to render
            story.append(Spacer(1, 6))
            continue
        story.append(Paragraph(markup, styles["BodyText"]))

    doc.build(story)
    return buffer.getvalue()

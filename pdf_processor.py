"""
Core logic for AI-assisted chapter splitting of a PDF.

Flow:
1. extract_page_previews()  -> pull a short text snippet from the TOP of each
   page (chapter headings are almost always near the top of the page they
   start on, so we don't need full-page text for detection -- this keeps the
   token count sane even for 400+ page textbooks).
2. detect_chapters()        -> send those previews to Claude, ask for a JSON
   list of {title, start_page}.
3. split_pdf_by_chapters()  -> use pypdf to physically cut the original PDF
   into one file per chapter based on the boundaries Claude found.
"""

import json
import os
import re

import anthropic
import pdfplumber
from pypdf import PdfReader, PdfWriter

from config import ANTHROPIC_API_KEY, CLAUDE_MODEL, MAX_PAGES_PER_PASS

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class ChapterDetectionError(Exception):
    pass


def extract_page_previews(pdf_path: str, chars_per_page: int = 350) -> list[str]:
    """Return a list where index i is a short text preview of page i+1."""
    previews = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            text = re.sub(r"\s+", " ", text).strip()
            previews.append(text[:chars_per_page])
    return previews


def detect_chapters(previews: list[str]) -> list[dict]:
    """
    Ask Claude to find chapter boundaries from page previews.
    Returns a list of dicts: [{"title": str, "start_page": int}, ...]
    start_page is 1-indexed and refers to the ORIGINAL pdf page numbers.
    """
    if len(previews) > MAX_PAGES_PER_PASS:
        raise ChapterDetectionError(
            f"PDF has {len(previews)} pages, which exceeds the "
            f"{MAX_PAGES_PER_PASS}-page single-pass limit. Split the file "
            f"manually first, or raise MAX_PAGES_PER_PASS in config.py."
        )

    numbered_text = "\n".join(
        f"[PAGE {i + 1}] {preview}" for i, preview in enumerate(previews)
    )

    system_prompt = (
        "You are analyzing the first ~350 characters of every page of a "
        "medical textbook PDF to find where each chapter begins. "
        "Identify real chapters (e.g. 'Chapter 1: Mood Disorders'), not "
        "front matter like table of contents, preface, or index, and not "
        "sub-sections within a chapter. "
        "Respond with ONLY a JSON array, no markdown fences, no preamble. "
        'Format: [{"title": "Chapter 1: Mood Disorders", "start_page": 12}, ...] '
        "start_page must be the page number shown in [PAGE N] where that "
        "chapter's heading first appears. If you cannot find clear chapter "
        "breaks, return a single entry covering the whole document starting "
        "at page 1."
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": numbered_text}],
    )

    raw = "".join(block.text for block in response.content if block.type == "text").strip()
    raw = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()

    try:
        chapters = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ChapterDetectionError(f"Could not parse Claude's response as JSON: {e}\nRaw: {raw[:500]}")

    if not isinstance(chapters, list) or not chapters:
        raise ChapterDetectionError("Claude returned no chapters.")

    for ch in chapters:
        if "title" not in ch or "start_page" not in ch:
            raise ChapterDetectionError(f"Malformed chapter entry: {ch}")

    chapters.sort(key=lambda c: c["start_page"])
    return chapters


def split_pdf_by_chapters(pdf_path: str, chapters: list[dict], output_dir: str) -> list[str]:
    """
    Physically split the PDF into one file per chapter.
    Returns list of output file paths, in chapter order.
    """
    os.makedirs(output_dir, exist_ok=True)
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    output_paths = []
    for idx, chapter in enumerate(chapters):
        start = chapter["start_page"] - 1  # to 0-indexed
        end = (
            chapters[idx + 1]["start_page"] - 1
            if idx + 1 < len(chapters)
            else total_pages
        )
        start = max(0, min(start, total_pages - 1))
        end = max(start + 1, min(end, total_pages))

        writer = PdfWriter()
        for page_num in range(start, end):
            writer.add_page(reader.pages[page_num])

        safe_title = re.sub(r"[^\w\s-]", "", chapter["title"]).strip().replace(" ", "_")[:60]
        filename = f"{idx + 1:02d}_{safe_title or 'chapter'}.pdf"
        out_path = os.path.join(output_dir, filename)

        with open(out_path, "wb") as f:
            writer.write(f)

        output_paths.append(out_path)

    return output_paths


def process_pdf(pdf_path: str, output_dir: str) -> tuple[list[dict], list[str]]:
    """Convenience wrapper: full pipeline from PDF path to split chapter files."""
    previews = extract_page_previews(pdf_path)
    chapters = detect_chapters(previews)
    output_paths = split_pdf_by_chapters(pdf_path, chapters, output_dir)
    return chapters, output_paths

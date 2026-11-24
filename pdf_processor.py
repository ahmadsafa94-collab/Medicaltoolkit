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
import logging
import os
import re

import anthropic
import pdfplumber
from pypdf import PdfReader, PdfWriter

import content_cache
import cost_ledger
from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_CLIENT_TIMEOUT_SECONDS,
    ANTHROPIC_WORKSPACE_ID,
    CLAUDE_MODEL,
    MAX_BOOK_PAGES,
    MAX_PAGES_PER_PASS,
)

# Explicit timeout -- the SDK's own default is ~10 minutes, which is far too
# long for a Telegram bot to sit silently on a stalled request. This is one
# half of the fix for uploads that appeared to hang forever; the other half
# is the overall asyncio.wait_for() around process_pdf() in bot.py.
client = anthropic.Anthropic(
    api_key=ANTHROPIC_API_KEY,
    timeout=ANTHROPIC_CLIENT_TIMEOUT_SECONDS,
    # An org-level key (not scoped to a workspace) is refused unless every
    # request names the workspace -- see config.ANTHROPIC_WORKSPACE_ID.
    default_headers={"anthropic-workspace-id": ANTHROPIC_WORKSPACE_ID} if ANTHROPIC_WORKSPACE_ID else None,
)

logger = logging.getLogger(__name__)


class ChapterDetectionError(Exception):
    pass


def count_pages(pdf_path: str) -> int:
    """Cheap page count via pypdf (no text extraction) for an early cap check."""
    return len(PdfReader(pdf_path).pages)


def open_check(pdf_path: str) -> int:
    """
    Page count, or a ChapterDetectionError a user can act on. pypdf's own
    errors for a password-protected or damaged file ("File has not been
    decrypted", "EOF marker not found") used to reach users verbatim.
    """
    locked = ChapterDetectionError(
        "This PDF is password-protected. Remove the password (e.g. print it to a new PDF) and send it again."
    )
    try:
        reader = PdfReader(pdf_path)
        if reader.is_encrypted:
            # Many PDFs are "encrypted" with an empty password only to block
            # editing; those open fine. A real password cannot be guessed.
            try:
                ok = reader.decrypt("")
            except Exception:
                raise locked
            if not ok:
                raise locked
        return len(reader.pages)
    except ChapterDetectionError:
        raise
    except Exception:
        raise ChapterDetectionError("This file couldn't be opened as a PDF -- it may be damaged. Try exporting it again.")


# A page "has text" with at least this many extracted characters. A scanned
# book (photos of pages) extracts as empty or a few stray glyphs per page.
_MIN_TEXT_CHARS_PER_PAGE = 40
_MIN_TEXT_PAGE_FRACTION = 0.10


def extract_page_previews(pdf_path: str, chars_per_page: int = 350, progress_cb=None) -> list[str]:
    """
    Return a list where index i is a short text preview of page i+1.

    progress_cb, if given, is a plain SYNCHRONOUS callable(pages_done, total_pages)
    invoked every 10 pages. This runs inside a worker thread (see bot.py's
    asyncio.to_thread call), so it must not touch asyncio/Telegram directly --
    callers relay it back to the event loop themselves (e.g. via
    loop.call_soon_threadsafe). pdfplumber's per-page extraction can be slow
    on complex/image-heavy pages, and without this a long book gives the user
    zero feedback for minutes at a time.
    """
    previews = []
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            text = re.sub(r"\s+", " ", text).strip()
            previews.append(text[:chars_per_page])
            page.close()  # release this page's parsed objects; see pdf_qa.extract_full_page_text
            if progress_cb and (i % 10 == 0 or i == total - 1):
                try:
                    progress_cb(i + 1, total)
                except Exception:
                    pass  # a broken progress callback should never abort extraction itself
    return previews


def _detect_window(previews: list[str], start: int, end: int, total: int) -> list[dict]:
    """One AI pass over pages start+1..end (0-based slice bounds). Returns raw, unvalidated entries."""
    numbered_text = "\n".join(f"[PAGE {i + 1}] {previews[i]}" for i in range(start, end))
    whole_book = start == 0 and end == total

    system_prompt = (
        "You are analyzing the first ~350 characters of every page of a "
        "medical textbook PDF to find where each chapter begins. "
        "Identify real chapters (e.g. 'Chapter 1: Mood Disorders'), not "
        "front matter like table of contents, preface, or index, and not "
        "sub-sections within a chapter. "
        "Respond with ONLY a JSON array, no markdown fences, no preamble. "
        'Format: [{"title": "Chapter 1: Mood Disorders", "start_page": 12}, ...] '
        "start_page must be the page number shown in [PAGE N] where that "
        "chapter's heading first appears. "
    )
    if whole_book:
        system_prompt += (
            "If you cannot find clear chapter breaks, return a single entry covering the whole "
            "document starting at page 1."
        )
    else:
        system_prompt += (
            f"You are seeing pages {start + 1}-{end} of a {total}-page book; the rest is analyzed "
            "separately. List only chapters whose heading appears on one of THESE pages, and return "
            "an empty array [] if none starts here."
        )

    # One retry: an unparseable reply is usually a one-off, and failing the
    # whole upload over it is worse than a second call.
    for attempt in range(2):
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=8000,
            thinking={"type": "disabled"},
            system=system_prompt,
            messages=[{"role": "user", "content": numbered_text}],
        )
        try:
            cost_ledger.record_claude_response("chapter_split", response)
        except Exception:
            pass  # cost logging must never break chapter detection itself

        raw = "".join(block.text for block in response.content if block.type == "text").strip()
        raw = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        try:
            chapters = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "Chapter split reply was not JSON (attempt %d, stop=%s): %r", attempt + 1, response.stop_reason, raw[:500]
            )
            continue
        if not isinstance(chapters, list) or (whole_book and not chapters):
            raise ChapterDetectionError("The AI returned no chapters. Please send the PDF again.")
        # A window may only name its own pages; anything else is a
        # hallucinated boundary that would collide with another window's.
        return [
            ch for ch in chapters
            if not (isinstance(ch, dict) and isinstance(ch.get("start_page"), int)
                    and not isinstance(ch.get("start_page"), bool)
                    and not (start < ch["start_page"] <= end))
        ]
    raise ChapterDetectionError("The AI's chapter list came back unreadable. Please send the PDF again.")


def detect_chapters(previews: list[str]) -> list[dict]:
    """
    Ask Claude to find chapter boundaries from page previews.
    Returns a list of dicts: [{"title": str, "start_page": int}, ...]
    start_page is 1-indexed and refers to the ORIGINAL pdf page numbers.

    Raises ChapterDetectionError if the page cap is exceeded, if Claude's
    response can't be parsed, or if it's structurally malformed (missing
    fields, non-integer/out-of-range start_page, or duplicate start_page
    values that would make the split logic silently produce a bogus
    near-empty chapter instead of a clear error).
    """
    if len(previews) > MAX_BOOK_PAGES:
        raise ChapterDetectionError(
            f"This PDF has {len(previews)} pages; the limit is {MAX_BOOK_PAGES}. "
            "Split it into parts first and send those."
        )
    with_text = sum(1 for p in previews if len(p) >= _MIN_TEXT_CHARS_PER_PAGE)
    if previews and with_text < max(1, len(previews) * _MIN_TEXT_PAGE_FRACTION):
        # Checked before the AI call: a scanned book has nothing to find
        # chapters in, and sending hundreds of empty "[PAGE n]" lines to the
        # model only produced a bogus one-chapter split at full cost.
        raise ChapterDetectionError(
            "This PDF looks like a scan (pictures of pages, no selectable text), so its text can't be "
            "read. Run it through an OCR tool first (e.g. Adobe Scan, or \"Make searchable\" in most "
            "PDF apps) and send the result."
        )

    # A long book is read in windows of MAX_PAGES_PER_PASS pages, one AI
    # call each, and the chapter lists are merged. Pages keep their real
    # numbers in every window, so the merged list needs no re-mapping. A
    # book that fits one window takes exactly the single call it always did.
    total = len(previews)
    chapters = []
    for start in range(0, total, MAX_PAGES_PER_PASS):
        chapters.extend(_detect_window(previews, start, min(start + MAX_PAGES_PER_PASS, total), total))
    if not chapters:
        chapters = [{"title": "Full document", "start_page": 1}]

    for ch in chapters:
        if not isinstance(ch, dict) or "title" not in ch or "start_page" not in ch:
            raise ChapterDetectionError("The AI's chapter list was malformed. Please send the PDF again.")
        # start_page must be a real page number, not a bool, string, float,
        # or anything else -- letting a bad type through here would only
        # surface later as a confusing TypeError from sort()/arithmetic in
        # split_pdf_by_chapters.
        if isinstance(ch["start_page"], bool) or not isinstance(ch["start_page"], int) or ch["start_page"] < 1:
            raise ChapterDetectionError("The AI's chapter list was malformed. Please send the PDF again.")
        if ch["start_page"] > len(previews):
            raise ChapterDetectionError(
                f"Chapter '{ch.get('title')}' has start_page {ch['start_page']}, "
                f"beyond the document's {len(previews)} pages."
            )

    chapters.sort(key=lambda c: c["start_page"])

    # Duplicate start_page values would make split_pdf_by_chapters silently
    # collapse the earlier chapter down to a bogus 1-page stub (its "end" is
    # clamped to its own start+1) instead of raising -- surface this clearly
    # instead of shipping a corrupted split.
    start_pages = [c["start_page"] for c in chapters]
    if len(set(start_pages)) != len(start_pages):
        raise ChapterDetectionError(
            f"The AI returned duplicate start_page values: {start_pages}. "
            "Try again, or report this PDF if it keeps happening."
        )

    return chapters


def compute_chapter_ranges(chapters: list[dict], total_pages: int) -> list[dict]:
    """
    Given Claude's raw chapter list ({"title","start_page"}, already sorted
    and validated by detect_chapters) and the document's total page count,
    return the same chapters with an added "end_page" (1-indexed, inclusive).

    This is the same boundary math split_pdf_by_chapters uses to decide
    where to cut each output file, but exposed standalone so a caller that
    only needs the METADATA -- e.g. the Book Shelf mini app, which keeps
    chapter boundaries in library.py rather than physically splitting the
    book into one file per chapter -- doesn't have to write any files just
    to know where each chapter ends.
    """
    result = []
    for idx, chapter in enumerate(chapters):
        start = chapter["start_page"]
        end = chapters[idx + 1]["start_page"] - 1 if idx + 1 < len(chapters) else total_pages
        end = max(start, min(end, total_pages))
        result.append({"title": chapter["title"], "start_page": start, "end_page": end})
    return result


def split_pdf_by_chapters(pdf_path: str, chapters: list[dict], output_dir: str) -> list[str]:
    """
    Physically split the PDF into one file per chapter.
    Returns list of output file paths, in chapter order.
    """
    os.makedirs(output_dir, exist_ok=True)
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)
    ranges = compute_chapter_ranges(chapters, total_pages)

    output_paths = []
    for idx, chapter in enumerate(ranges):
        start = chapter["start_page"] - 1  # to 0-indexed
        end = chapter["end_page"]  # end_page is inclusive/1-indexed, which is exactly the exclusive 0-indexed bound
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


def process_pdf(pdf_path: str, output_dir: str, progress_cb=None) -> tuple[list[dict], list[str]]:
    """
    Convenience wrapper: full pipeline from PDF path to split chapter files.
    progress_cb: see extract_page_previews -- forwarded through unchanged.
    """
    # Check the page cap with a cheap pypdf page count BEFORE running the
    # much more expensive full-text extraction over every page -- no point
    # paying that cost on a PDF we're about to reject anyway.
    page_count = open_check(pdf_path)
    if page_count > MAX_BOOK_PAGES:
        raise ChapterDetectionError(
            f"This PDF has {page_count} pages; the limit is {MAX_BOOK_PAGES}. "
            "Split it into parts first and send those."
        )

    chapters = detect_chapters_cached(pdf_path, progress_cb)
    output_paths = split_pdf_by_chapters(pdf_path, chapters, output_dir)
    return chapters, output_paths


def detect_chapters_cached(pdf_path: str, progress_cb=None) -> list[dict]:
    """
    detect_chapters for a file, reusing the result for a byte-identical file
    seen before (content_cache.py) -- a repeat upload of the same textbook
    then costs no AI call and skips the slow page-by-page read entirely.
    """
    digest = None
    try:
        digest = content_cache.file_sha256(pdf_path)
        cached = content_cache.get_chapters(digest)
        if cached:
            logger.info("Reused cached chapters %s for %s", digest[:12], os.path.basename(pdf_path))
            return cached
    except Exception:
        logger.exception("Chapter cache lookup failed (detecting normally)")
    chapters = detect_chapters(extract_page_previews(pdf_path, progress_cb=progress_cb))
    if digest:
        content_cache.put_chapters(digest, chapters)
    return chapters

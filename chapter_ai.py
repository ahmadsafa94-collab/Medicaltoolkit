"""
On-demand AI study aids for a split PDF chapter: a summary, or a short
self-test quiz -- generated only when the user taps a button, never
automatically for every chapter (that would spend API budget on chapters
nobody actually wants to review).

This is a deliberately different risk profile from drug_lookup.py, which
avoids AI entirely for dosing content because a hallucinated number could
cause real harm. A study summary/quiz of a chapter the student already has
in front of them (as the just-sent chapter PDF) is much lower-stakes and
self-correcting -- they can immediately check it against the source text --
but it's still AI-generated free text, so every output here is clearly
labeled as such and never presented as authoritative on its own.
"""

import logging
import re

import pdfplumber

from config import CLAUDE_MODEL
from pdf_processor import client  # reuse the same Anthropic client instance, not a second one

logger = logging.getLogger(__name__)

# Bounds how much chapter text gets sent to Claude per call -- keeps API cost
# and latency predictable even for an unusually long chapter, at the cost of
# the summary/quiz only covering the first ~15k tokens' worth of the chapter
# if it's truncated (flagged to the user when that happens).
MAX_CHARS_PER_CHAPTER = 60_000


class ChapterAIError(Exception):
    pass


def extract_chapter_text(pdf_path: str, max_chars: int = MAX_CHARS_PER_CHAPTER) -> tuple[str, bool]:
    """
    Extract plain text from an already-split chapter PDF (not the original
    whole-book file). Returns (text, was_truncated).
    """
    parts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                parts.append(text)
    except Exception as e:
        raise ChapterAIError(f"Couldn't read text from this chapter's PDF: {e}")

    full_text = re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()
    if not full_text:
        raise ChapterAIError(
            "No extractable text found in this chapter (it may be scanned images rather than real text)."
        )

    truncated = len(full_text) > max_chars
    return full_text[:max_chars], truncated


def extract_text_for_page_range(
    pdf_path: str, start_page: int, end_page: int, max_chars: int = MAX_CHARS_PER_CHAPTER
) -> tuple[str, bool]:
    """
    Like extract_chapter_text, but pulls a specific 1-indexed inclusive page
    range straight out of the ORIGINAL whole-book PDF, rather than reading
    an already-split chapter file. Used by the Book Shelf mini app, which
    only keeps chapter BOUNDARIES (library.set_chapters) instead of
    physically splitting the book into one file per chapter -- there's no
    split file to read, so this re-derives the same "one chapter's worth of
    text" input the chat flow's AI buttons use, on demand, from the
    boundaries plus the durable original PDF.
    """
    parts = []
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total = len(pdf.pages)
            start_idx = max(0, start_page - 1)
            end_idx = min(total, end_page)
            for page in pdf.pages[start_idx:end_idx]:
                text = page.extract_text() or ""
                parts.append(text)
    except Exception as e:
        raise ChapterAIError(f"Couldn't read text for this chapter: {e}")

    full_text = re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()
    if not full_text:
        raise ChapterAIError(
            "No extractable text found in this chapter (it may be scanned images rather than real text)."
        )

    truncated = len(full_text) > max_chars
    return full_text[:max_chars], truncated


def _call_claude(system_prompt: str, user_content: str, max_tokens: int) -> str:
    """Synchronous call (the anthropic SDK's default client is sync) -- callers run this via asyncio.to_thread."""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def summarize_chapter(title: str, text: str, truncated: bool) -> str:
    """Synchronous -- run via asyncio.to_thread from an async handler."""
    system_prompt = (
        "You are helping a medical student review a textbook chapter. Produce a concise, well-organized "
        "study summary of the chapter text the user provides. Structure it as: 1) a one-paragraph overview, "
        "2) key concepts/terms as short bullet points (plain text bullets using '-', no markdown headers), "
        "3) any especially high-yield facts, numbers, or classifications worth memorizing. "
        "Be faithful to the provided text -- do not add outside facts not supported by it. "
        "Keep the whole summary under ~500 words."
    )
    try:
        summary = _call_claude(system_prompt, f"Chapter title: {title}\n\n{text}", max_tokens=2000)
    except Exception as e:
        raise ChapterAIError(f"Claude request failed: {e}")

    note = "\n\n_(Note: this chapter was long, so the summary is based on its first portion only.)_" if truncated else ""
    return summary + note


def quiz_chapter(title: str, text: str, truncated: bool, num_questions: int = 5) -> str:
    """Synchronous -- run via asyncio.to_thread from an async handler."""
    system_prompt = (
        f"You are helping a medical student self-test on a textbook chapter. Write exactly {num_questions} "
        "multiple-choice questions (4 options: A-D) based ONLY on the chapter text the user provides -- do "
        "not introduce outside facts. After all the questions, include an 'Answers' section listing the "
        "correct letter and a one-sentence explanation for each, referencing the chapter content. "
        "Use plain text only (no markdown headers, '-' for any bullets)."
    )
    try:
        quiz = _call_claude(system_prompt, f"Chapter title: {title}\n\n{text}", max_tokens=2500)
    except Exception as e:
        raise ChapterAIError(f"Claude request failed: {e}")

    note = "\n\n_(Note: this chapter was long, so questions are based on its first portion only.)_" if truncated else ""
    return quiz + note


# Sanity cap on how many chapters a whole-book summary will walk -- a
# 1500-page book divided into reasonably-sized chapters is comfortably
# under this; it exists to bound worst-case runtime/cost rather than to be
# hit in normal use.
MAX_CHAPTERS_FOR_WHOLE_BOOK_SUMMARY = 80

# Combined chapter-summaries text handed to the final "reduce" call is
# capped the same way per-chapter extraction is capped, rather than risking
# one oversized final request for a book with many chapters.
MAX_COMBINED_SUMMARY_CHARS = MAX_CHARS_PER_CHAPTER * 2


def summarize_whole_book(title: str, pdf_path: str, chapters: list[dict], progress_cb=None) -> str:
    """
    Map-reduce summary for a whole book: summarize each chapter's text
    individually (map), then ask Claude to synthesize those chapter
    summaries into one cohesive overview (reduce). Necessary because a
    1500-page book's full text can't reliably fit in one prompt -- this
    keeps every individual Claude call bounded regardless of book length,
    at the cost of making one call per chapter plus one final call.

    chapters: the book's full {"title","start_page","end_page"} list (see
    library.set_chapters) -- run "Divide into chapters" first.
    progress_cb, if given, is a plain synchronous callable(done, total)
    reporting how many chapters have been summarized so far -- this can
    take a while for a book with many chapters, so callers should surface
    it (e.g. webapp_api's job-status polling) rather than leaving the user
    with no feedback for the whole duration.
    Synchronous -- run via asyncio.to_thread from an async handler.
    """
    if not chapters:
        raise ChapterAIError("This book hasn't been divided into chapters yet -- run 'Divide into chapters' first.")

    chapters = chapters[:MAX_CHAPTERS_FOR_WHOLE_BOOK_SUMMARY]
    chapter_summaries = []
    for i, chapter in enumerate(chapters):
        try:
            text, truncated = extract_text_for_page_range(pdf_path, chapter["start_page"], chapter["end_page"])
            summary = summarize_chapter(chapter["title"], text, truncated)
            chapter_summaries.append(f"## {chapter['title']}\n{summary}")
        except ChapterAIError as e:
            logger.warning("Skipping chapter '%s' in whole-book summary: %s", chapter["title"], e)
        if progress_cb:
            try:
                progress_cb(i + 1, len(chapters))
            except Exception:
                pass  # a broken progress callback should never abort the summary itself

    if not chapter_summaries:
        raise ChapterAIError("Couldn't extract readable text from any chapter to summarize.")

    combined = "\n\n".join(chapter_summaries)[:MAX_COMBINED_SUMMARY_CHARS]

    system_prompt = (
        f"You are helping a medical student review the book '{title}'. Below are AI-generated "
        "summaries of each of its chapters. Write ONE cohesive whole-book overview: "
        "1) a short paragraph on what the book covers overall, "
        "2) the major themes/sections and how they relate to each other, "
        "3) the highest-yield facts worth remembering across the whole book. "
        "Do not just concatenate the chapter summaries -- synthesize them. Plain text only "
        "(no markdown headers, '-' for any bullets), under ~700 words."
    )
    try:
        return _call_claude(system_prompt, combined, max_tokens=2500)
    except Exception as e:
        raise ChapterAIError(f"Claude request failed: {e}")

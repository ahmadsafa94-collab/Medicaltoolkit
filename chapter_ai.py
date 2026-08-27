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

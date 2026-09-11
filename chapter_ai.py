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

import json
import logging
import re

import pdfplumber

import cost_ledger
from config import CLAUDE_MODEL
from pdf_processor import client  # reuse the same Anthropic client instance, not a second one

logger = logging.getLogger(__name__)

# Bounds how much chapter text gets sent to Claude in ONE call. This used to
# also be the hard ceiling on how much of a chapter a summary/quiz could ever
# cover -- extract_chapter_text()/extract_text_for_page_range() would simply
# drop everything past this many characters, which is why a long chapter's
# summary used to come back flagged "based on its first portion only" even
# though the whole chapter was right there. It's still used that way by
# quiz_chapter() (a multi-question quiz doesn't need every last paragraph)
# and by quiz_ai.py's multi-chapter context budget, but summarize_chapter_full()
# below no longer treats it as a ceiling on the WHOLE chapter -- it's now just
# the chunk size for a map-reduce over chapters longer than this, so nothing
# gets silently dropped. See MAX_CHARS_HARD_CAP for the actual (much larger)
# ceiling summaries use.
MAX_CHARS_PER_CHAPTER = 60_000

# The real ceiling for "the whole chapter," used by summarize_chapter_full()
# (and summarize_whole_book()'s per-chapter step) instead of the smaller
# MAX_CHARS_PER_CHAPTER. Real textbook chapters -- even long ones -- are
# essentially never this long (at ~2,000 characters/page this is roughly 200
# pages), so this is a safety net against a mis-detected "chapter" that's
# actually most of the book turning one summary into an unbounded number of
# sequential Claude calls, not a limit anyone should expect to hit in normal
# use.
MAX_CHARS_HARD_CAP = 400_000


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


def _call_claude(system_prompt: str, user_content: str, max_tokens: int, feature: str = "chapter_ai") -> str:
    """Synchronous call (the anthropic SDK's default client is sync) -- callers run this via asyncio.to_thread."""
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    )
    try:
        cost_ledger.record_claude_response(feature, response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")
    return "".join(block.text for block in response.content if block.type == "text").strip()


def summarize_chapter(title: str, text: str, truncated: bool, language: str = "English") -> str:
    """Synchronous -- run via asyncio.to_thread from an async handler."""
    system_prompt = (
        "You are helping a medical student review a textbook chapter. Produce a concise, well-organized "
        "study summary of the chapter text the user provides. Structure it as: 1) a one-paragraph overview, "
        "2) key concepts/terms as short bullet points (plain text bullets using '-', no markdown headers), "
        "3) any especially high-yield facts, numbers, or classifications worth memorizing. "
        "Be faithful to the provided text -- do not add outside facts not supported by it. "
        "Keep the whole summary under ~500 words. "
        f"Respond in {language}."
    )
    try:
        summary = _call_claude(system_prompt, f"Chapter title: {title}\n\n{text}", max_tokens=2000, feature="chapter_summary")
    except Exception as e:
        raise ChapterAIError(f"Claude request failed: {e}")

    note = "\n\n_(Note: this chapter was long, so the summary is based on its first portion only.)_" if truncated else ""
    return summary + note


def summarize_chapter_full(title: str, text: str, hard_truncated: bool = False, language: str = "English") -> str:
    """
    Summarize an ENTIRE chapter regardless of length, via map-reduce when
    it's longer than one Claude call can comfortably take:
      1. Map: split into sequential MAX_CHARS_PER_CHAPTER-sized chunks,
         extract each chunk's key concepts/terms/high-yield facts as notes.
      2. Reduce: synthesize all those notes into ONE cohesive study summary
         in the same 3-part structure summarize_chapter() produces.
    A chapter that fits in a single call (the common case) just calls
    summarize_chapter() directly -- no map-reduce overhead for a normal-
    sized chapter. Same overall shape as summarize_whole_book(), one level
    down (chunks of a chapter instead of chapters of a book).

    `text` should already be extracted with a generous cap (see
    MAX_CHARS_HARD_CAP) rather than the smaller MAX_CHARS_PER_CHAPTER --
    this is what actually fixes the old "summary only covers the first
    portion" behavior for a real textbook chapter. `hard_truncated` flags
    the rare case where even THAT cap was hit (a mis-detected "chapter"
    that's actually most of the book), in which case the summary still
    only covers what was extracted, same as before but far less likely to
    ever trigger.
    Synchronous -- run via asyncio.to_thread from an async handler.
    """
    if len(text) <= MAX_CHARS_PER_CHAPTER:
        summary = summarize_chapter(title, text, truncated=False, language=language)
    else:
        chunks = [text[i:i + MAX_CHARS_PER_CHAPTER] for i in range(0, len(text), MAX_CHARS_PER_CHAPTER)]
        part_notes = []
        for i, chunk in enumerate(chunks):
            part_prompt = (
                f"You are helping a medical student review part {i + 1} of {len(chunks)} (in sequence) of the "
                f"textbook chapter '{title}'. Extract this portion's key concepts, terms, and high-yield facts "
                "as concise plain-text bullet points (using '-', no markdown headers). Be faithful to the text "
                f"-- do not add outside facts not supported by it. Respond in {language}."
            )
            try:
                part_notes.append(_call_claude(part_prompt, chunk, max_tokens=1200, feature="chapter_summary"))
            except Exception as e:
                raise ChapterAIError(f"Claude request failed summarizing part {i + 1}/{len(chunks)}: {e}")

        combined = "\n\n".join(f"[Part {i + 1} of {len(chunks)}]\n{notes}" for i, notes in enumerate(part_notes))
        reduce_prompt = (
            f"You are helping a medical student review the textbook chapter '{title}'. Below are notes "
            "extracted from each sequential part of this (long) chapter. Synthesize them into ONE cohesive "
            "study summary, structured as: 1) a one-paragraph overview, 2) key concepts/terms as short bullet "
            "points (plain text bullets using '-', no markdown headers), 3) any especially high-yield facts, "
            "numbers, or classifications worth memorizing. Merge duplicate or related points across parts "
            f"rather than just concatenating them. Keep the whole summary under ~700 words. Respond in {language}."
        )
        try:
            summary = _call_claude(reduce_prompt, combined, max_tokens=2200, feature="chapter_summary")
        except Exception as e:
            raise ChapterAIError(f"Claude request failed combining chapter parts: {e}")

    if hard_truncated:
        summary += (
            "\n\n_(Note: this chapter was extremely long (over "
            f"{MAX_CHARS_HARD_CAP:,} characters) -- the summary covers roughly its first "
            f"{MAX_CHARS_HARD_CAP:,} characters.)_"
        )
    return summary


def quiz_chapter(title: str, text: str, truncated: bool, num_questions: int = 5, language: str = "English") -> str:
    """Synchronous -- run via asyncio.to_thread from an async handler."""
    system_prompt = (
        f"You are helping a medical student self-test on a textbook chapter. Write exactly {num_questions} "
        "multiple-choice questions (4 options: A-D) based ONLY on the chapter text the user provides -- do "
        "not introduce outside facts. After all the questions, include an 'Answers' section listing the "
        "correct letter and a one-sentence explanation for each, referencing the chapter content. "
        f"Use plain text only (no markdown headers, '-' for any bullets). Respond in {language}."
    )
    try:
        quiz = _call_claude(system_prompt, f"Chapter title: {title}\n\n{text}", max_tokens=2500, feature="chapter_quiz")
    except Exception as e:
        raise ChapterAIError(f"Claude request failed: {e}")

    note = "\n\n_(Note: this chapter was long, so questions are based on its first portion only.)_" if truncated else ""
    return quiz + note


def generate_mnemonics(title: str, text: str, language: str = "English") -> str:
    """
    Ask Claude for a short set of memory aids (acronyms, rhymes, vivid short
    phrases) covering a chapter's highest-yield facts -- a lighter-weight
    sibling of summarize_chapter_full, same "faithful to the provided text"
    guardrail. Synchronous -- run via asyncio.to_thread from an async handler.
    """
    system_prompt = (
        "You are helping a medical student memorize a textbook chapter. Read the chapter text and produce "
        "3-6 mnemonics (acronyms, rhymes, or short vivid phrases) that help remember its most important, "
        "easily-confused, or list-heavy facts (e.g. classifications, drug lists, diagnostic criteria, steps "
        "in a process). For each mnemonic: state the mnemonic itself, then one line spelling out exactly what "
        "each letter/part stands for, then the specific fact(s) it's for. Only use facts actually present in "
        "the chapter text -- do not invent outside facts. Plain text only (no markdown headers, '-' for any "
        f"bullets). Respond in {language}."
    )
    try:
        return _call_claude(system_prompt, f"Chapter title: {title}\n\n{text}", max_tokens=1500, feature="mnemonics")
    except Exception as e:
        raise ChapterAIError(f"Claude request failed: {e}")


MAX_CARDS_PER_GENERATION = 50
# Same shape as quiz_ai.MAX_TOTAL_QUIZ_CONTEXT_CHARS -- the total chapter-text
# budget split across every chapter selected for one flashcard-generation run.
MAX_TOTAL_FLASHCARD_CONTEXT_CHARS = 100_000


def generate_flashcards(title: str, text: str, num_cards: int = 12, language: str = "English") -> list[dict]:
    """
    Ask Claude for a set of front/back spaced-repetition flashcards covering
    a chapter's key facts -- returns STRUCTURED JSON (same
    validate-then-use pattern as quiz_ai.generate_quiz) so flashcards.py can
    store each card individually for SM-2 scheduling, rather than one big
    text blob. Synchronous -- run via asyncio.to_thread from an async handler.

    `text` may already be the concatenation of several chapters (see
    generate_flashcards_multi below), in which case `title` is a combined
    label -- this function itself doesn't care, it just needs one block of
    source text and a display label for the prompt.
    """
    system_prompt = (
        f"You are writing {num_cards} spaced-repetition flashcards for a medical student reviewing a "
        "textbook chapter. Each card should test ONE specific, well-defined fact -- prefer many focused "
        "cards over few broad ones. Base every card ONLY on the chapter text provided -- do not add outside "
        "facts. If the text covers multiple chapters (marked with '[Chapter: ...]' headers), spread the "
        "cards across all of them rather than clustering on just one. Respond with ONLY a JSON array, no "
        'markdown fences, no preamble. Format: [{"front": "question or prompt", "back": "concise answer"}, ...] '
        f"Write the front/back text in {language}."
    )
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            # Raised from a 4000 ceiling (which silently truncated/broke JSON
            # past ~24 cards) to 8000 -- comfortably covers
            # MAX_CARDS_PER_GENERATION, same headroom quiz_ai.generate_quiz
            # uses for its own MAX_QUESTIONS.
            max_tokens=min(150 * num_cards + 400, 8000),
            system=system_prompt,
            messages=[{"role": "user", "content": f"Chapter title: {title}\n\n{text}"}],
        )
    except Exception as e:
        raise ChapterAIError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response("flashcards", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    raw = "".join(block.text for block in response.content if block.type == "text").strip()
    raw = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()

    try:
        cards = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ChapterAIError(f"Could not parse Claude's flashcard response as JSON: {e}\nRaw: {raw[:500]}")

    if not isinstance(cards, list) or not cards:
        raise ChapterAIError("Claude returned no flashcards.")

    validated = []
    for c in cards:
        if not isinstance(c, dict) or not c.get("front") or not c.get("back"):
            continue  # skip a malformed single card rather than failing the whole batch
        validated.append({"front": str(c["front"]), "back": str(c["back"])})

    if not validated:
        raise ChapterAIError("Claude's flashcard response had no usable cards.")
    return validated


def generate_flashcards_multi(
    book_title: str, pdf_path: str, chapters: list[dict], num_cards: int, language: str = "English"
) -> list[dict]:
    """
    Like generate_flashcards() above, but draws from potentially MULTIPLE
    selected chapters in one generation run -- same per-chapter text-budget
    split as quiz_ai.generate_quiz() uses for its own multi-chapter context
    (MAX_TOTAL_FLASHCARD_CONTEXT_CHARS divided across the selected chapters,
    each excerpt tagged with a "[Chapter: ...]" header so Claude can spread
    coverage instead of clustering all cards in whichever chapter happens to
    come first). `chapters` is the SELECTED subset of the book's own
    {"title","start_page","end_page"} entries (see library.set_chapters).
    """
    if not isinstance(num_cards, int) or isinstance(num_cards, bool) or not (1 <= num_cards <= MAX_CARDS_PER_GENERATION):
        raise ChapterAIError(f"Number of flashcards must be a whole number between 1 and {MAX_CARDS_PER_GENERATION}.")
    if not chapters:
        raise ChapterAIError("Select at least one chapter to generate flashcards from.")

    parts = []
    remaining = MAX_TOTAL_FLASHCARD_CONTEXT_CHARS
    per_chapter_budget = max(2000, MAX_TOTAL_FLASHCARD_CONTEXT_CHARS // len(chapters))
    for chapter in chapters:
        if remaining <= 0:
            break
        budget = min(per_chapter_budget, remaining, MAX_CHARS_PER_CHAPTER)
        try:
            text, _truncated = extract_text_for_page_range(
                pdf_path, chapter["start_page"], chapter["end_page"], max_chars=budget
            )
        except ChapterAIError as e:
            logger.warning("Skipping chapter '%s' in flashcard generation: %s", chapter["title"], e)
            continue
        parts.append(f"[Chapter: {chapter['title']}]\n{text}")
        remaining -= len(text)

    if not parts:
        raise ChapterAIError("Couldn't extract readable text from the selected chapters.")

    context = "\n\n".join(parts)
    chapter_label = ", ".join(c["title"] for c in chapters)[:200] or book_title
    return generate_flashcards(chapter_label, context, num_cards, language)


# Sanity cap on how many chapters a whole-book summary will walk -- a
# 1500-page book divided into reasonably-sized chapters is comfortably
# under this; it exists to bound worst-case runtime/cost rather than to be
# hit in normal use.
MAX_CHAPTERS_FOR_WHOLE_BOOK_SUMMARY = 80

# Combined chapter-summaries text handed to the final "reduce" call is
# capped the same way per-chapter extraction is capped, rather than risking
# one oversized final request for a book with many chapters.
MAX_COMBINED_SUMMARY_CHARS = MAX_CHARS_PER_CHAPTER * 2


def summarize_whole_book(title: str, pdf_path: str, chapters: list[dict], progress_cb=None, language: str = "English") -> str:
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
            # Extracted with the generous MAX_CHARS_HARD_CAP (not the smaller
            # MAX_CHARS_PER_CHAPTER) and summarized via summarize_chapter_full's
            # map-reduce, same fix as the single-chapter summary path -- a
            # long chapter inside a whole-book summary no longer gets quietly
            # cut short before the rest of the book even gets synthesized in.
            text, hard_truncated = extract_text_for_page_range(
                pdf_path, chapter["start_page"], chapter["end_page"], MAX_CHARS_HARD_CAP
            )
            summary = summarize_chapter_full(chapter["title"], text, hard_truncated, language=language)
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
        f"(no markdown headers, '-' for any bullets), under ~700 words. Respond in {language}."
    )
    try:
        return _call_claude(system_prompt, combined, max_tokens=2500, feature="book_summary")
    except Exception as e:
        raise ChapterAIError(f"Claude request failed: {e}")

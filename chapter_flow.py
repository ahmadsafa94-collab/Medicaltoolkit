"""
Callback handlers for the "📝 Summarize" / "❓ Quiz me" buttons attached to
each split chapter document (see bot.py's handle_pdf_upload and
telegram_helpers.send_documents_safely).

The chapter's extracted text is cached (via session_cache, same TTL-limited
in-memory cache /dose uses for section buttons) at send time, BEFORE the
split chapter PDF file gets deleted from disk in bot.py's cleanup step --
these handlers only ever read from that cache, never from the filesystem,
since the file is long gone by the time a button is actually tapped.
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.types import CallbackQuery

import chapter_ai
import session_cache
import subscriptions
from keyboards import chapter_ai_kb
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="chapter_flow")

# Generous enough to cover a long chapter's map-reduce summary (several
# sequential Claude calls -- see chapter_ai.summarize_chapter_full), not
# just a single quick call the way this used to only need to.
_GENERATION_TIMEOUT_SECONDS = 240


async def build_chapter_ai_kb(chapter: dict, path: str):
    """
    Extracts this chapter's text from its just-created split PDF and caches
    it (via session_cache) BEFORE bot.py's cleanup step deletes that file --
    the button handlers below only ever read from this cache, never from
    disk, since the file is gone by the time a button is actually tapped.
    Returns the "Summarize"/"Quiz me" keyboard, or None if text extraction
    fails (e.g. a scanned/image-only chapter) so that chapter's document
    still sends successfully, just without those buttons.

    Lives here (rather than in bot.py, where it used to) so that
    webapp_api.py -- which cannot import bot.py, see bot_instance.py's
    docstring -- can reuse it too, for the Book Shelf mini app's own
    "send chapter files to my chat" feature.

    Extracts with MAX_CHARS_HARD_CAP rather than the smaller
    MAX_CHARS_PER_CHAPTER default: that's what lets the "Summarize" button
    below cover the WHOLE chapter (via chapter_ai.summarize_chapter_full's
    map-reduce) instead of silently stopping after the first ~60,000
    characters the way it used to. The "Quiz me" button still trims back
    down to MAX_CHARS_PER_CHAPTER itself before calling Claude -- a
    multi-question quiz doesn't need the same fix, and keeping its prompt
    small keeps its cost/latency exactly what it was before.
    """
    try:
        text, hard_truncated = await asyncio.to_thread(
            chapter_ai.extract_chapter_text, path, chapter_ai.MAX_CHARS_HARD_CAP
        )
    except chapter_ai.ChapterAIError:
        logger.info("No extractable text for chapter '%s' -- sending without AI buttons", chapter.get("title"))
        return None

    cache_id = session_cache.put({"title": chapter["title"], "text": text, "hard_truncated": hard_truncated})
    return chapter_ai_kb(cache_id)

_AI_DISCLAIMER = (
    "\n\n⚠️ AI-generated study aid -- may contain errors or omissions. "
    "Cross-check against the actual chapter text (the PDF you were just sent)."
)


async def _get_chapter_entry(callback: CallbackQuery, cache_id: str) -> dict | None:
    entry = session_cache.get(cache_id)
    if entry is None:
        await callback.answer(
            "This chapter's cached text has expired (or the bot restarted). "
            "Re-upload the PDF to generate a new summary/quiz.",
            show_alert=True,
        )
        return None
    return entry


@router.callback_query(F.data.startswith("chai:sum:"))
async def handle_chapter_summarize(callback: CallbackQuery):
    cache_id = callback.data.split(":", 2)[2]
    entry = await _get_chapter_entry(callback, cache_id)
    if entry is None:
        return

    try:
        subscriptions.check_and_consume(callback.from_user.id, "summaries")
    except subscriptions.QuotaExceeded as e:
        await callback.answer()
        await callback.message.answer(str(e))
        return

    await callback.answer("Generating summary...")
    title = entry["title"]

    try:
        summary = await asyncio.wait_for(
            asyncio.to_thread(chapter_ai.summarize_chapter_full, title, entry["text"], entry["hard_truncated"]),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await callback.message.answer("Generating that summary took too long. Please try again.")
        return
    except chapter_ai.ChapterAIError as e:
        await callback.message.answer(f"Couldn't generate a summary: {e}")
        return
    except Exception:
        logger.exception("Unexpected error summarizing chapter '%s'", title)
        await callback.message.answer("Something went wrong generating that summary. Please try again.")
        return

    text = f"📝 *Summary -- {title}*\n\n{summary}{_AI_DISCLAIMER}"
    ok = await send_long_text(callback.message.answer, text)
    if not ok:
        await callback.message.answer("Couldn't send the summary (Telegram rejected the message).")


@router.callback_query(F.data.startswith("chai:quiz:"))
async def handle_chapter_quiz(callback: CallbackQuery):
    cache_id = callback.data.split(":", 2)[2]
    entry = await _get_chapter_entry(callback, cache_id)
    if entry is None:
        return

    try:
        subscriptions.check_and_consume(callback.from_user.id, "quizzes")
    except subscriptions.QuotaExceeded as e:
        await callback.answer()
        await callback.message.answer(str(e))
        return

    await callback.answer("Generating quiz...")
    title = entry["title"]

    # The cached text is extracted up to the generous MAX_CHARS_HARD_CAP (so
    # "Summarize" above can cover the whole chapter) -- trim it back down to
    # the smaller MAX_CHARS_PER_CHAPTER here so a multi-question quiz keeps
    # its original, smaller cost/latency profile rather than inheriting
    # summarize's much larger budget.
    full_text = entry["text"]
    quiz_text = full_text[: chapter_ai.MAX_CHARS_PER_CHAPTER]
    quiz_truncated = len(full_text) > chapter_ai.MAX_CHARS_PER_CHAPTER

    try:
        quiz = await asyncio.wait_for(
            asyncio.to_thread(chapter_ai.quiz_chapter, title, quiz_text, quiz_truncated),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await callback.message.answer("Generating that quiz took too long. Please try again.")
        return
    except chapter_ai.ChapterAIError as e:
        await callback.message.answer(f"Couldn't generate a quiz: {e}")
        return
    except Exception:
        logger.exception("Unexpected error generating quiz for chapter '%s'", title)
        await callback.message.answer("Something went wrong generating that quiz. Please try again.")
        return

    text = f"❓ *Quiz -- {title}*\n\n{quiz}{_AI_DISCLAIMER}"
    ok = await send_long_text(callback.message.answer, text)
    if not ok:
        await callback.message.answer("Couldn't send the quiz (Telegram rejected the message).")


@router.callback_query(F.data.startswith("chai:mnem:"))
async def handle_chapter_mnemonics(callback: CallbackQuery):
    cache_id = callback.data.split(":", 2)[2]
    entry = await _get_chapter_entry(callback, cache_id)
    if entry is None:
        return

    try:
        subscriptions.check_and_consume(callback.from_user.id, "summaries")
    except subscriptions.QuotaExceeded as e:
        await callback.answer()
        await callback.message.answer(str(e))
        return

    await callback.answer("Generating mnemonics...")
    title = entry["title"]
    language = subscriptions.get_language(callback.from_user.id)

    # Trimmed to MAX_CHARS_PER_CHAPTER, same reasoning as "Quiz me" above --
    # a mnemonic list only needs the chapter's key facts, not its full text.
    full_text = entry["text"]
    mnem_text = full_text[: chapter_ai.MAX_CHARS_PER_CHAPTER]

    try:
        mnemonics = await asyncio.wait_for(
            asyncio.to_thread(chapter_ai.generate_mnemonics, title, mnem_text, language),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await callback.message.answer("Generating mnemonics took too long. Please try again.")
        return
    except chapter_ai.ChapterAIError as e:
        await callback.message.answer(f"Couldn't generate mnemonics: {e}")
        return
    except Exception:
        logger.exception("Unexpected error generating mnemonics for chapter '%s'", title)
        await callback.message.answer("Something went wrong generating mnemonics. Please try again.")
        return

    text = f"🧠 *Mnemonics -- {title}*\n\n{mnemonics}{_AI_DISCLAIMER}"
    ok = await send_long_text(callback.message.answer, text)
    if not ok:
        await callback.message.answer("Couldn't send the mnemonics (Telegram rejected the message).")


def register_chapter_handlers(dp) -> None:
    dp.include_router(router)

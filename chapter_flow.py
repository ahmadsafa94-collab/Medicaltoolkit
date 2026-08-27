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
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="chapter_flow")

_GENERATION_TIMEOUT_SECONDS = 60

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

    await callback.answer("Generating summary...")
    title = entry["title"]

    try:
        summary = await asyncio.wait_for(
            asyncio.to_thread(chapter_ai.summarize_chapter, title, entry["text"], entry["truncated"]),
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

    await callback.answer("Generating quiz...")
    title = entry["title"]

    try:
        quiz = await asyncio.wait_for(
            asyncio.to_thread(chapter_ai.quiz_chapter, title, entry["text"], entry["truncated"]),
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


def register_chapter_handlers(dp) -> None:
    dp.include_router(router)

"""
Generic Telegram-sending helpers shared across bot.py and renal_flow.py.

Pulled out of bot.py so the renal-calculator conversation flow (renal_flow.py)
can reuse the same careful flood-control/fallback handling for text, photos,
and document sends without creating a circular import between the two
route-registration modules.
"""

import asyncio
import logging

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramAPIError
from aiogram.types import Message, FSInputFile, BufferedInputFile

from table_image import render_table_image

logger = logging.getLogger(__name__)


async def send_long_text(answer_fn, text: str) -> bool:
    """
    Send `text` in <=4000-char chunks via `answer_fn` (e.g. message.answer or
    callback.message.answer). Handles the two ways Telegram can reject a send:
    - TelegramBadRequest (e.g. an unmatched '*'/'_' slipped through from
      source data) -> retries that chunk as plain text.
    - TelegramRetryAfter (flood control -- triggered by sending many chunks
      back-to-back, which "Show everything" on a long section can do) ->
      waits the time Telegram asks for, then retries that chunk.
    A small delay between chunks avoids hitting flood control in the first
    place. Returns True if every chunk sent successfully, False otherwise,
    so the caller can tell the user something went wrong instead of the
    message just silently never arriving.
    """
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)]

    for idx, chunk in enumerate(chunks):
        sent = False
        for _retry in range(3):
            try:
                await answer_fn(chunk, parse_mode="Markdown")
                sent = True
                break
            except TelegramBadRequest:
                logger.warning("Markdown parse failed for a chunk, resending as plain text")
                try:
                    await answer_fn(chunk)
                    sent = True
                except Exception:
                    logger.exception("Plain-text fallback send also failed")
                break
            except TelegramRetryAfter as e:
                logger.warning("Flood control hit, waiting %s seconds", e.retry_after)
                await asyncio.sleep(e.retry_after + 0.5)
                continue
            except TelegramAPIError:
                logger.exception("Telegram API error sending chunk %d", idx)
                break

        if not sent:
            return False

        if idx < len(chunks) - 1:
            await asyncio.sleep(0.4)  # stay well under flood-control thresholds

    return True


async def send_documents_safely(
    message: Message, chapters: list[dict], output_paths: list[str], get_reply_markup=None
) -> tuple[int, int]:
    """
    Send one document per chapter, tolerating the same kinds of failures
    send_long_text already guards against for text: flood control between
    back-to-back sends, and a single bad send that shouldn't take the whole
    delivery down silently. Returns (sent_count, total_count) so the caller
    can report a partial failure instead of the user just getting fewer
    files than expected with no explanation.

    get_reply_markup, if given, is an async callable (chapter_dict, path) ->
    InlineKeyboardMarkup | None, called once per chapter to attach buttons
    (e.g. "Summarize" / "Quiz me") to that chapter's document message. A
    failure building the markup for one chapter just means that chapter's
    document is sent without buttons rather than failing the whole upload.
    """
    sent = 0
    for idx, (chapter, path) in enumerate(zip(chapters, output_paths)):
        caption = f"{chapter['title']} (from page {chapter['start_page']})"

        reply_markup = None
        if get_reply_markup is not None:
            try:
                reply_markup = await get_reply_markup(chapter, path)
            except Exception:
                logger.exception("Failed to build reply_markup for chapter %d (%s)", idx, path)

        for _retry in range(3):
            try:
                await message.answer_document(FSInputFile(path), caption=caption[:1024], reply_markup=reply_markup)
                sent += 1
                break
            except TelegramRetryAfter as e:
                logger.warning("Flood control hit sending chapter %d, waiting %s seconds", idx, e.retry_after)
                await asyncio.sleep(e.retry_after + 0.5)
                continue
            except TelegramAPIError:
                # Covers things like "file too large" for a single oversized
                # chapter -- log it and move on to the rest instead of
                # aborting the whole delivery.
                logger.exception("Failed to send chapter %d (%s)", idx, path)
                break

        if idx < len(output_paths) - 1:
            await asyncio.sleep(0.5)  # stay well under flood-control thresholds for document sends

    return sent, len(output_paths)


async def send_photo_safely(answer_photo_fn, png_bytes: bytes, caption: str) -> bool:
    """Send one photo (e.g. a rendered table image), tolerating flood control the same way the helpers above do."""
    for _retry in range(3):
        try:
            await answer_photo_fn(BufferedInputFile(png_bytes, filename="table.png"), caption=caption[:1024])
            return True
        except TelegramRetryAfter as e:
            logger.warning("Flood control hit sending table image, waiting %s seconds", e.retry_after)
            await asyncio.sleep(e.retry_after + 0.5)
            continue
        except TelegramAPIError:
            logger.exception("Failed to send table image")
            return False
    return False


async def send_table_entries(message: Message, drug_name: str, table_entries: list[dict]) -> None:
    """
    Render and send each detected table as a photo. Falls back to sending
    the raw text (via send_long_text) if rendering itself fails for some
    reason, so a rendering bug never just silently drops dosing
    information the user asked for.
    """
    for idx, entry in enumerate(table_entries):
        caption = f"{drug_name} — {entry['title']}"
        try:
            png_bytes = render_table_image(entry["text"], title=caption)
        except Exception:
            logger.exception("Failed to render table image for %s", entry.get("title"))
            await message.answer(f"Couldn't render this as an image ({caption}) -- here's the raw text instead:")
            await send_long_text(message.answer, entry["text"])
            continue

        ok = await send_photo_safely(message.answer_photo, png_bytes, caption)
        if not ok:
            await message.answer(f"Couldn't send the table image for {caption}.")

        if idx < len(table_entries) - 1:
            await asyncio.sleep(0.5)

"""
Medical Student Toolkit Bot -- Step 1: AI-powered PDF chapter splitting.

Run with:
    python bot.py

Requires a .env file (see .env.example) with:
    TELEGRAM_BOT_TOKEN=...
    ANTHROPIC_API_KEY=...
"""

import asyncio
import logging
import os
import re
import shutil

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.types import (
    Message,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    CallbackQuery,
)

from config import TELEGRAM_BOT_TOKEN, STORAGE_DIR, MAX_UPLOAD_BYTES
from pdf_processor import process_pdf, ChapterDetectionError
from drug_lookup import (
    lookup_drug,
    format_drug_info,
    format_section,
    available_sections,
    search_drug_names,
    DrugNotFoundError,
    DrugLookupRateLimitedError,
)
from keyboards import main_menu_kb, drug_search_inline_kb, drug_sections_kb, BTN_DOSE, BTN_UPLOAD, BTN_HELP
from telegram_helpers import send_long_text, send_documents_safely, send_table_entries
from renal_flow import register_renal_handlers
import session_cache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# The "Calculate dose by renal function" conversation (multi-step eGFR/CrCl
# calculator) lives in its own module since it's a self-contained FSM flow --
# see renal_flow.py for why it never asserts a specific dose itself.
register_renal_handlers(dp)


@dp.errors()
async def global_error_handler(event, exception):
    """
    Last-resort safety net: logs the FULL traceback for any exception that
    escapes an individual handler, so a bug never just silently disappears
    with 'nothing happens' and no trace in the logs.
    """
    logger.exception("Unhandled exception in update %s: %s", event, exception)
    return True  # mark as handled so aiogram doesn't re-raise


def user_dir(user_id: int) -> str:
    path = os.path.join(STORAGE_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def safe_pdf_filename(raw_name: str | None) -> str:
    """
    Turn a Telegram-supplied filename into something safe to join onto a
    server-side path.

    `doc.file_name` is client-supplied metadata -- a sender using the raw Bot
    API (not just the official Telegram app) can set it to anything,
    including things like "../../../etc/whatever.pdf". Passing that straight
    into os.path.join() is a path-traversal bug: the download could land
    outside the per-user storage folder entirely. Strip any directory
    components and keep only a safe character set, mirroring the sanitizing
    already done for chapter titles in pdf_processor.py.
    """
    name = os.path.basename(raw_name or "")
    name = re.sub(r"[^\w.-]", "_", name).strip(". ") or "upload"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:200]  # keep well under filesystem filename limits


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Welcome to the Medical Student Toolkit bot.\n\n"
        "Send me a textbook PDF and I'll split it into chapters using AI.\n\n"
        "Commands:\n"
        "/start - this message\n"
        "/help - how to use the bot\n"
        "/dose <drug> - FDA label dosing & reference info",
        reply_markup=main_menu_kb,
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Just send a .pdf file as a document (not a photo) and I'll:\n"
        "1. Read it\n"
        "2. Ask Claude to find the chapter boundaries\n"
        "3. Send you back one PDF per chapter\n\n"
        "Note: very large PDFs (400+ pages) aren't supported in this first "
        "version.\n\n"
        "/dose <drug name> - looks up a drug in the FDA label database, "
        "then shows buttons so you can pick exactly which section you want "
        "(Dosage, Contraindications, Interactions, etc.) instead of one huge "
        "wall of text. Reference only, not a substitute for a current "
        "formulary."
    )


@dp.message(F.text == BTN_HELP)
async def btn_help(message: Message):
    await cmd_help(message)


@dp.message(F.text == BTN_UPLOAD)
async def btn_upload(message: Message):
    await message.answer("Send me a .pdf file as a document (attach → file) and I'll split it into chapters.")


@dp.message(F.text == BTN_DOSE)
async def btn_dose(message: Message):
    bot_info = await bot.get_me()
    await message.answer(
        "Tap the button below, then start typing a drug name — "
        "you'll see live suggestions to pick from.",
        reply_markup=drug_search_inline_kb(bot_info.username),
    )


@dp.inline_query()
async def handle_inline_drug_search(inline_query: InlineQuery):
    prefix = inline_query.query.strip()

    if len(prefix) < 2:
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    try:
        names = await search_drug_names(prefix)
    except Exception:
        logger.exception("Inline drug search failed")
        names = []

    results = [
        InlineQueryResultArticle(
            id=str(i),
            title=name,
            description="Tap to look up dosing & label info",
            input_message_content=InputTextMessageContent(message_text=f"/dose {name}"),
        )
        for i, name in enumerate(names)
    ]

    await inline_query.answer(results, cache_time=30, is_personal=True)


@dp.message(Command("dose"))
async def cmd_dose(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Usage: /dose <drug name>\nExample: /dose sertraline"
        )
        return

    drug_name = args[1].strip()
    status_msg = await message.answer(f"Looking up {drug_name}...")

    try:
        sections = await asyncio.wait_for(lookup_drug(drug_name), timeout=25)
    except asyncio.TimeoutError:
        await status_msg.edit_text(
            "The FDA database took too long to respond. Please try again in a moment."
        )
        return
    except DrugLookupRateLimitedError as e:
        await status_msg.edit_text(str(e))
        return
    except DrugNotFoundError as e:
        await status_msg.edit_text(str(e))
        return
    except Exception as e:
        logger.exception("Drug lookup failed")
        await status_msg.edit_text(f"Lookup failed: {e}")
        return

    sections_present = available_sections(sections)
    cache_id = session_cache.put(sections)
    name = sections.get("_name", drug_name)

    menu_text = f"💊 *{name}* — found {len(sections_present)} section(s). Tap what you need:"
    try:
        await status_msg.edit_text(
            menu_text, parse_mode="Markdown", reply_markup=drug_sections_kb(cache_id, sections_present)
        )
    except TelegramBadRequest:
        await status_msg.edit_text(
            menu_text.replace("*", ""), reply_markup=drug_sections_kb(cache_id, sections_present)
        )


@dp.callback_query(F.data.startswith("sec:"))
async def handle_section_tap(callback: CallbackQuery):
    try:
        _, cache_id, concept = callback.data.split(":", 2)
    except ValueError:
        await callback.answer("Something went wrong with that button.", show_alert=True)
        return

    sections = session_cache.get(cache_id)
    if sections is None:
        await callback.answer(
            "This lookup expired. Please run /dose again.", show_alert=True
        )
        return

    # Acknowledge the tap FIRST, before any formatting/sending. If those
    # steps throw for any reason, the spinner still clears and we still
    # have a chance to tell the user something went wrong below, instead
    # of the button just doing nothing with an exception logged server-side
    # and never surfaced.
    await callback.answer()

    try:
        if concept == "_all":
            reply, table_entries = format_drug_info(sections)
        else:
            reply, table_entries = format_section(sections, concept)
    except Exception:
        logger.exception("Failed to format section '%s' for cache_id=%s", concept, cache_id)
        await callback.message.answer(
            "Couldn't display that section due to an internal error. Please try /dose again."
        )
        return

    ok = await send_long_text(callback.message.answer, reply)
    if not ok:
        await callback.message.answer(
            "Couldn't send that section (Telegram rejected the message). "
            "Try again, or use /dose again if the problem continues."
        )

    if table_entries:
        drug_name = sections.get("_name", "Drug")
        await send_table_entries(callback.message, drug_name, table_entries)


@dp.message(F.document)
async def handle_pdf_upload(message: Message):
    doc = message.document
    file_name = doc.file_name or ""

    if doc.mime_type != "application/pdf" and not file_name.lower().endswith(".pdf"):
        await message.answer("That doesn't look like a PDF. Please send a .pdf file.")
        return

    # Telegram's Bot API cannot download files over 20MB at all -- check
    # this up front so a large textbook fails with a clear message instead
    # of getting stuck on "Downloading..." forever with no explanation.
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await message.answer(
            f"That file is {doc.file_size / 1024 / 1024:.1f}MB, which is over "
            f"Telegram's {MAX_UPLOAD_BYTES // 1024 // 1024}MB limit for bot downloads. "
            "Please split it yourself first, or send a smaller file."
        )
        return

    status_msg = await message.answer("Got it. Downloading...")

    workdir = user_dir(message.from_user.id)
    # Sanitize the filename Telegram gives us -- it's client-supplied and,
    # sent via the raw Bot API, could contain path-traversal sequences
    # (e.g. "../../evil.pdf") that would otherwise let a download land
    # outside this user's storage folder.
    local_pdf_path = os.path.join(workdir, safe_pdf_filename(file_name))
    output_dir = os.path.join(workdir, "chapters")

    try:
        file = await bot.get_file(doc.file_id)
        await bot.download_file(file.file_path, destination=local_pdf_path)
    except TelegramAPIError as e:
        logger.exception("Failed to download uploaded PDF")
        await status_msg.edit_text(f"Couldn't download that file from Telegram: {e}")
        return

    await status_msg.edit_text("Downloaded. Reading pages and detecting chapters with AI...")

    # Clear out any chapter files left over from a previous upload by this
    # user before writing new ones, so stale files don't pile up on disk
    # indefinitely (they've already been delivered to the user via Telegram
    # and serve no purpose sitting on the server).
    shutil.rmtree(output_dir, ignore_errors=True)

    try:
        try:
            chapters, output_paths = await asyncio.to_thread(
                process_pdf, local_pdf_path, output_dir
            )
        except ChapterDetectionError as e:
            await status_msg.edit_text(f"Couldn't split this PDF: {e}")
            return
        except Exception as e:
            logger.exception("Unexpected error processing PDF")
            await status_msg.edit_text(f"Something went wrong: {e}")
            return

        await status_msg.edit_text(
            f"Found {len(chapters)} chapter(s). Sending them now..."
        )

        sent, total = await send_documents_safely(message, chapters, output_paths)

        if sent < total:
            await message.answer(
                f"Sent {sent} of {total} chapter(s) -- the rest failed to send "
                "(they may be too large for Telegram). Check the logs, or try "
                "re-uploading the PDF."
            )
        else:
            await message.answer("Done. Send another PDF anytime.")
    finally:
        # The source PDF and its split chapters have either been delivered
        # to the user or failed permanently -- either way, keeping them on
        # disk forever just accumulates storage with no benefit. Clean up
        # unconditionally so a stream of uploads (especially large ones)
        # can't fill the disk.
        try:
            os.remove(local_pdf_path)
        except OSError:
            pass
        shutil.rmtree(output_dir, ignore_errors=True)


async def main():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

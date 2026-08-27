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
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError
from aiogram.types import (
    Message,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    CallbackQuery,
    BufferedInputFile,
)

from config import TELEGRAM_BOT_TOKEN, STORAGE_DIR, MAX_UPLOAD_BYTES
from pdf_processor import process_pdf, ChapterDetectionError
from drug_lookup import (
    lookup_drug,
    format_drug_info,
    format_section,
    format_pregnancy_lactation,
    available_sections,
    search_drug_names,
    DrugNotFoundError,
    DrugLookupRateLimitedError,
)
from keyboards import (
    main_menu_kb,
    drug_search_inline_kb,
    drug_sections_kb,
    recent_list_kb,
    chapter_ai_kb,
    BTN_DOSE,
    BTN_UPLOAD,
    BTN_HELP,
    BTN_CALC,
    BTN_INTERACTIONS,
)
from telegram_helpers import send_long_text, send_documents_safely, send_table_entries
from renal_flow import register_renal_handlers
from calc_flow import register_calc_handlers, cmd_calculators
from interaction_flow import register_interaction_handlers, cmd_interactions
from chapter_flow import register_chapter_handlers
import chapter_ai
import glossary
import pdf_export
import session_cache
import user_history

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# The "Calculate dose by renal function" conversation (multi-step eGFR/CrCl
# calculator) lives in its own module since it's a self-contained FSM flow --
# see renal_flow.py for why it never asserts a specific dose itself.
register_renal_handlers(dp)

# The general clinical-calculator menu (BMI/BSA, corrected labs, MELD, etc.)
# -- see calc_flow.py for the generic declarative FSM engine driving all of them.
register_calc_handlers(dp)

# The multi-drug interaction checker -- see interaction_flow.py.
register_interaction_handlers(dp)

# On-demand "Summarize" / "Quiz me" buttons attached to split chapter PDFs --
# see chapter_flow.py and chapter_ai.py.
register_chapter_handlers(dp)


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
        "/dose <drug> - FDA label dosing & reference info\n"
        "/calculators - BMI/BSA, corrected labs, MELD, CHA₂DS₂-VASc, Wells' criteria, and more\n"
        "/interactions - add multiple drugs and cross-check their FDA labels for mentions of each other\n"
        "/pregnancy <drug> - just the Pregnancy & Nursing/Lactation sections of a drug's FDA label\n"
        "/glossary <term> - common medical abbreviations & lab reference ranges\n"
        "/recent - your last few /dose lookups, tap to look up again\n"
        "/bookmarks - drugs you've bookmarked (via the 🔖 button after /dose)",
        reply_markup=main_menu_kb,
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Just send a .pdf file as a document (not a photo) and I'll:\n"
        "1. Read it\n"
        "2. Ask Claude to find the chapter boundaries\n"
        "3. Send you back one PDF per chapter, each with '📝 Summarize' and "
        "'❓ Quiz me' buttons for an on-demand AI study aid on that chapter "
        "(generated only if you tap the button -- always check it against "
        "the actual chapter text, it can contain errors)\n\n"
        "Note: very large PDFs (400+ pages) aren't supported in this first "
        "version.\n\n"
        "/dose <drug name> - looks up a drug in the FDA label database, "
        "then shows buttons so you can pick exactly which section you want "
        "(Dosage, Contraindications, Interactions, etc.) instead of one huge "
        "wall of text. Reference only, not a substitute for a current "
        "formulary.\n\n"
        "/calculators - a menu of standard clinical calculators (BMI/BSA, "
        "corrected calcium & sodium, anion gap, maintenance IV fluids, QTc, "
        "MELD/MELD-Na/MELD 3.0, CHA₂DS₂-VASc, Wells' criteria for PE and DVT). "
        "Every one uses an exact published formula -- the bot never applies "
        "clinical judgment on top of the number it calculates.\n\n"
        "/interactions - add as many drugs as you want, then cross-check each "
        "one's FDA label for mentions of the others. This is a text search "
        "over each label, not a curated interaction database -- it can miss "
        "interactions described by drug class, and an absence of a mention "
        "never rules one out.\n\n"
        "/pregnancy <drug name> - shortcut straight to a drug's Pregnancy and "
        "Nursing/Lactation label sections, skipping the section menu.\n\n"
        "/glossary <term> - look up a medical abbreviation or a typical adult "
        "lab reference range. Run /glossary with no term to see what's covered.\n\n"
        "/recent - re-open one of your last few /dose lookups.\n\n"
        "/bookmarks - drugs you've saved with the 🔖 button after a /dose lookup "
        "(remove one with /unbookmark <name>)."
    )


@dp.message(F.text == BTN_HELP)
async def btn_help(message: Message):
    await cmd_help(message)


@dp.message(F.text == BTN_UPLOAD)
async def btn_upload(message: Message):
    await message.answer("Send me a .pdf file as a document (attach → file) and I'll split it into chapters.")


@dp.message(F.text == BTN_CALC)
async def btn_calc(message: Message):
    await cmd_calculators(message)


@dp.message(F.text == BTN_INTERACTIONS)
async def btn_interactions(message: Message, state: FSMContext):
    await cmd_interactions(message, state)


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


async def _dose_lookup_and_show(answer_fn, drug_name: str, user_id: int | None) -> None:
    """
    Shared core of /dose: look up a drug and show the section-picker menu.
    Factored out so /recent and /bookmarks 'tap to look up again' buttons
    (and /dose itself) all go through the same lookup/error-handling/history
    logic instead of three copies drifting apart over time.
    answer_fn is message.answer or callback.message.answer.
    """
    status_msg = await answer_fn(f"Looking up {drug_name}...")

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

    if user_id is not None:
        try:
            await user_history.record_recent(user_id, name)
        except Exception:
            # Recording history is a nice-to-have -- a disk/IO hiccup here
            # should never take down the lookup the user actually asked for.
            logger.exception("Failed to record recent lookup for user %s", user_id)

    menu_text = f"💊 *{name}* — found {len(sections_present)} section(s). Tap what you need:"
    try:
        await status_msg.edit_text(
            menu_text, parse_mode="Markdown", reply_markup=drug_sections_kb(cache_id, sections_present)
        )
    except TelegramBadRequest:
        await status_msg.edit_text(
            menu_text.replace("*", ""), reply_markup=drug_sections_kb(cache_id, sections_present)
        )


@dp.message(Command("dose"))
async def cmd_dose(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Usage: /dose <drug name>\nExample: /dose sertraline"
        )
        return

    await _dose_lookup_and_show(message.answer, args[1].strip(), user_id=message.from_user.id)


@dp.callback_query(F.data.startswith("bm:add:"))
async def handle_bookmark_add(callback: CallbackQuery):
    cache_id = callback.data.split(":", 2)[2]
    sections = session_cache.get(cache_id)
    if sections is None:
        await callback.answer("This lookup expired. Please run /dose again.", show_alert=True)
        return

    name = sections.get("_name", "this drug")
    try:
        result = await user_history.add_bookmark(callback.from_user.id, name)
    except Exception:
        logger.exception("Failed to add bookmark for user %s", callback.from_user.id)
        await callback.answer("Couldn't save that bookmark right now.", show_alert=True)
        return

    if result == "added":
        await callback.answer(f"🔖 Bookmarked {name}.")
    elif result == "duplicate":
        await callback.answer(f"{name} is already bookmarked.")
    else:
        await callback.answer(
            f"Bookmark list is full ({user_history.MAX_BOOKMARKS}) -- remove one with /unbookmark first.",
            show_alert=True,
        )


@dp.callback_query(F.data.startswith("pdfexp:"))
async def handle_pdf_export(callback: CallbackQuery):
    cache_id = callback.data.split(":", 1)[1]
    sections = session_cache.get(cache_id)
    if sections is None:
        await callback.answer("This lookup expired. Please run /dose again.", show_alert=True)
        return

    await callback.answer("Generating PDF...")
    name = sections.get("_name", "drug")

    try:
        text, _table_entries = format_drug_info(sections)
        # PDF generation is CPU-bound (reportlab layout) -- run off the event
        # loop so it can't stall other users' updates while it renders.
        pdf_bytes = await asyncio.to_thread(
            pdf_export.generate_text_pdf, f"{name} — FDA Label Reference", text
        )
    except Exception:
        logger.exception("PDF export failed for %s", name)
        await callback.message.answer("Couldn't generate the PDF export right now.")
        return

    filename = (re.sub(r"[^\w.-]", "_", name).strip("._") or "drug")[:50] + ".pdf"
    try:
        await callback.message.answer_document(
            BufferedInputFile(pdf_bytes, filename=filename),
            caption=f"{name} — FDA label reference (exported). Note: any tables in the label are not "
                    "included here -- see the section buttons above for those as images.",
        )
    except TelegramAPIError:
        logger.exception("Failed to send exported PDF for %s", name)
        await callback.message.answer("Generated the PDF but couldn't send it (Telegram rejected the file).")


@dp.message(Command("recent"))
async def cmd_recent(message: Message):
    names = await user_history.get_recent(message.from_user.id)
    if not names:
        await message.answer("No recent lookups yet -- try /dose <drug name> first.")
        return
    await message.answer(
        "🕘 *Recent lookups* — tap to look up again:",
        parse_mode="Markdown",
        reply_markup=recent_list_kb(names, "recent"),
    )


@dp.message(Command("bookmarks"))
async def cmd_bookmarks(message: Message):
    names = await user_history.get_bookmarks(message.from_user.id)
    if not names:
        await message.answer(
            "No bookmarks yet -- after a /dose lookup, tap '🔖 Bookmark this drug' to save it here."
        )
        return
    await message.answer(
        "🔖 *Bookmarked drugs* — tap to look up again (or /unbookmark <name> to remove one):",
        parse_mode="Markdown",
        reply_markup=recent_list_kb(names, "bookmark"),
    )


@dp.message(Command("unbookmark"))
async def cmd_unbookmark(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /unbookmark <drug name> (must match a name shown in /bookmarks)")
        return

    removed = await user_history.remove_bookmark(message.from_user.id, args[1].strip())
    if removed:
        await message.answer(f"Removed {args[1].strip()} from your bookmarks.")
    else:
        await message.answer("That name wasn't found in your bookmarks -- check /bookmarks for the exact name.")


@dp.callback_query(F.data.startswith("redo:"))
async def handle_redo_lookup(callback: CallbackQuery):
    parts = callback.data.split(":", 2)
    if len(parts) != 3:
        await callback.answer("Something went wrong with that button.", show_alert=True)
        return
    _, source, idx_str = parts

    try:
        idx = int(idx_str)
    except ValueError:
        await callback.answer("Something went wrong with that button.", show_alert=True)
        return

    names = await (
        user_history.get_recent(callback.from_user.id)
        if source == "recent"
        else user_history.get_bookmarks(callback.from_user.id)
    )
    if idx < 0 or idx >= len(names):
        await callback.answer(
            "That entry isn't in the list anymore (it may have changed since this menu was shown).",
            show_alert=True,
        )
        return

    await callback.answer()
    await _dose_lookup_and_show(callback.message.answer, names[idx], user_id=callback.from_user.id)


@dp.message(Command("glossary"))
async def cmd_glossary(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        terms = glossary.all_terms()
        preview = ", ".join(terms[:20]) + (", ..." if len(terms) > 20 else "")
        await message.answer(
            "📖 *Glossary*\n\n"
            "Usage: /glossary <term or abbreviation>\n"
            "Example: /glossary eGFR\n\n"
            f"{len(terms)} terms available, including: {preview}\n\n"
            "_Lab reference ranges shown are typical adult ranges -- always check the reporting "
            "lab's own reference interval, since assay/units/population shift the exact cutoffs._",
            parse_mode="Markdown",
        )
        return

    term = args[1].strip()
    try:
        t, c, d = glossary.lookup_term(term)
    except glossary.GlossaryNotFoundError as e:
        matches = glossary.search_glossary(term, limit=8)
        suggestion = f"\n\nDid you mean: {', '.join(matches)}?" if matches else ""
        await message.answer(str(e) + suggestion)
        return

    await message.answer(glossary.format_entry(t, c, d), parse_mode="Markdown")


@dp.message(Command("pregnancy"))
async def cmd_pregnancy(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Usage: /pregnancy <drug name>\nExample: /pregnancy sertraline")
        return

    drug_name = args[1].strip()
    status_msg = await message.answer(f"Looking up {drug_name}...")

    try:
        sections = await asyncio.wait_for(lookup_drug(drug_name), timeout=25)
    except asyncio.TimeoutError:
        await status_msg.edit_text("The FDA database took too long to respond. Please try again in a moment.")
        return
    except DrugLookupRateLimitedError as e:
        await status_msg.edit_text(str(e))
        return
    except DrugNotFoundError as e:
        await status_msg.edit_text(str(e))
        return
    except Exception as e:
        logger.exception("Drug lookup failed (pregnancy shortcut)")
        await status_msg.edit_text(f"Lookup failed: {e}")
        return

    await status_msg.edit_text(f"Found {sections.get('_name', drug_name)}.")

    try:
        reply, table_entries = format_pregnancy_lactation(sections)
    except Exception:
        logger.exception("Failed to format pregnancy/lactation view")
        await message.answer("Couldn't display that due to an internal error. Please try /dose instead.")
        return

    ok = await send_long_text(message.answer, reply)
    if not ok:
        await message.answer("Couldn't send that (Telegram rejected the message).")
    if table_entries:
        await send_table_entries(message, sections.get("_name", drug_name), table_entries)


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


async def _build_chapter_ai_kb(chapter: dict, path: str):
    """
    Extracts this chapter's text from its just-created split PDF and caches
    it (via session_cache) BEFORE bot.py's cleanup step deletes that file --
    chapter_flow.py's button handlers only ever read from this cache, never
    from disk, since the file is gone by the time a button is actually
    tapped. Returns the "Summarize"/"Quiz me" keyboard, or None if text
    extraction fails (e.g. a scanned/image-only chapter) so that chapter's
    document still sends successfully, just without those buttons.
    """
    try:
        text, truncated = await asyncio.to_thread(chapter_ai.extract_chapter_text, path)
    except chapter_ai.ChapterAIError:
        logger.info("No extractable text for chapter '%s' -- sending without AI buttons", chapter.get("title"))
        return None

    cache_id = session_cache.put({"title": chapter["title"], "text": text, "truncated": truncated})
    return chapter_ai_kb(cache_id)


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

        sent, total = await send_documents_safely(
            message, chapters, output_paths, get_reply_markup=_build_chapter_ai_kb
        )

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

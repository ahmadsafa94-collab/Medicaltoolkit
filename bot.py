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

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramAPIError
from aiogram.types import (
    Message,
    FSInputFile,
    BufferedInputFile,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    CallbackQuery,
    ErrorEvent,
)

from config import TELEGRAM_BOT_TOKEN, STORAGE_DIR
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
from keyboards import (
    main_menu_kb,
    drug_search_inline_kb,
    drug_sections_kb,
    make_searchable_kb,
    book_picker_kb,
    BTN_DOSE,
    BTN_UPLOAD,
    BTN_ASK,
    BTN_HELP,
)
import session_cache
import library
import pdf_qa

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

# user_id -> book_id, set when a user taps a book from /ask's picker and
# clears once they send their question (or pick a different book). Simple
# in-memory state; fine for a single-process bot, lost on restart like the
# session_cache.
awaiting_question: dict[int, str] = {}


@dp.errors()
async def global_error_handler(event: ErrorEvent):
    """
    Last-resort safety net: logs the FULL traceback for any exception that
    escapes an individual handler, so a bug never just silently disappears
    with 'nothing happens' and no trace in the logs.
    """
    logger.exception("Unhandled exception while processing update: %s", event.exception, exc_info=event.exception)
    return True  # mark as handled so aiogram doesn't re-raise


def user_dir(user_id: int) -> str:
    path = os.path.join(STORAGE_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


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


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Welcome to the Medical Student Toolkit bot.\n\n"
        "Send me a textbook PDF and I'll split it into chapters using AI. "
        "You can also make a book searchable and ask it questions in plain "
        "language — like NotebookLM.\n\n"
        "Commands:\n"
        "/start - this message\n"
        "/help - how to use the bot\n"
        "/dose <drug> - FDA label dosing & reference info\n"
        "/ask - ask a question about a book you've made searchable",
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
        "formulary.\n\n"
        "After uploading a PDF, tap '🔍 Make this book searchable' to ask it "
        "questions in plain language — I'll find the relevant passages and "
        "answer citing the exact page numbers, like NotebookLM."
    )


@dp.message(F.text == BTN_HELP)
async def btn_help(message: Message):
    await cmd_help(message)


@dp.message(F.text == BTN_UPLOAD)
async def btn_upload(message: Message):
    await message.answer("Send me a .pdf file as a document (attach → file) and I'll split it into chapters.")


async def show_book_picker(message: Message):
    books = library.list_books(message.from_user.id)
    if not books:
        await message.answer(
            "You don't have any searchable books yet. Upload a PDF, then tap "
            "'🔍 Make this book searchable' on the result to enable Q&A for it."
        )
        return
    await message.answer("Which book do you want to ask about?", reply_markup=book_picker_kb(books))


@dp.message(F.text == BTN_ASK)
async def btn_ask(message: Message):
    await show_book_picker(message)


@dp.message(Command("ask"))
async def cmd_ask(message: Message):
    await show_book_picker(message)


@dp.callback_query(F.data.startswith("askbook:"))
async def handle_book_pick(callback: CallbackQuery):
    book_id = callback.data.split(":", 1)[1]
    book = library.get_book(callback.from_user.id, book_id)
    if book is None:
        await callback.answer("That book isn't available anymore.", show_alert=True)
        return

    awaiting_question[callback.from_user.id] = book_id
    await callback.answer()
    await callback.message.answer(f"📖 Ask a question about *{book['title']}*:", parse_mode="Markdown")


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
            reply = format_drug_info(sections)
        else:
            reply = format_section(sections, concept)
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


@dp.message(F.document)
async def handle_pdf_upload(message: Message):
    doc = message.document

    if doc.mime_type != "application/pdf" and not doc.file_name.lower().endswith(".pdf"):
        await message.answer("That doesn't look like a PDF. Please send a .pdf file.")
        return

    status_msg = await message.answer("Got it. Downloading...")

    workdir = user_dir(message.from_user.id)
    local_pdf_path = os.path.join(workdir, doc.file_name)

    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, destination=local_pdf_path)

    await status_msg.edit_text("Downloaded. Reading pages and detecting chapters with AI...")

    output_dir = os.path.join(workdir, "chapters")

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

    for chapter, path in zip(chapters, output_paths):
        caption = f"{chapter['title']} (from page {chapter['start_page']})"
        await message.answer_document(FSInputFile(path), caption=caption[:1024])

    # Offer to make the whole book semantically searchable ("Ask my book").
    # Store what the indexing step needs (path + a title) behind a short id --
    # reusing session_cache since it's already exactly this kind of "remember
    # a bit of data for a future button tap" store.
    book_title = os.path.splitext(doc.file_name)[0]
    pending_id = session_cache.put({"pdf_path": local_pdf_path, "title": book_title})

    await message.answer(
        "Done. Send another PDF anytime.\n\n"
        "Want to ask this book questions in plain language? I'll index it "
        "for semantic search (may take a minute or two for a long book).",
        reply_markup=make_searchable_kb(pending_id),
    )


@dp.callback_query(F.data.startswith("index:"))
async def handle_index_request(callback: CallbackQuery):
    pending_id = callback.data.split(":", 1)[1]
    pending = session_cache.get(pending_id)
    if pending is None:
        await callback.answer("This request expired. Please upload the PDF again.", show_alert=True)
        return

    await callback.answer()
    status_msg = await callback.message.answer("Starting indexing...")

    async def progress(text: str):
        try:
            await status_msg.edit_text(text)
        except TelegramBadRequest:
            pass  # message content unchanged or too soon after last edit -- harmless

    book_id = library.add_book(callback.from_user.id, pending["title"], num_chunks=0)  # placeholder, updated below

    try:
        num_chunks = await asyncio.wait_for(
            pdf_qa.build_index(pending["pdf_path"], book_id, pending["title"], progress_cb=progress),
            timeout=600,  # long books with many chunks can take a while to embed
        )
    except asyncio.TimeoutError:
        await status_msg.edit_text("Indexing took too long and timed out. Try again, or use a shorter book.")
        return
    except pdf_qa.IndexingError as e:
        await status_msg.edit_text(f"Couldn't index this book: {e}")
        return
    except Exception as e:
        logger.exception("Indexing failed")
        await status_msg.edit_text(f"Indexing failed: {e}")
        return

    # update the registry entry now that we know the real chunk count
    library.update_chunk_count(callback.from_user.id, book_id, num_chunks)

    await status_msg.edit_text(
        f"✅ *{pending['title']}* is ready ({num_chunks} passages indexed). "
        f"Use /ask or 💬 Ask My Books to ask it a question.",
        parse_mode="Markdown",
    )


@dp.message(lambda m: m.from_user and m.from_user.id in awaiting_question)
async def handle_book_question(message: Message):
    book_id = awaiting_question.pop(message.from_user.id)
    book = library.get_book(message.from_user.id, book_id)
    if book is None:
        await message.answer("That book isn't available anymore. Use /ask to pick another.")
        return

    question = message.text
    if not question:
        await message.answer("Please send your question as text.")
        awaiting_question[message.from_user.id] = book_id  # let them try again
        return

    status_msg = await message.answer("Searching the book...")

    try:
        result = await asyncio.wait_for(pdf_qa.answer_question(book_id, question), timeout=30)
    except asyncio.TimeoutError:
        await status_msg.edit_text("That took too long. Please try again.")
        return
    except pdf_qa.IndexingError as e:
        await status_msg.edit_text(str(e))
        return
    except Exception as e:
        logger.exception("Book Q&A failed")
        await status_msg.edit_text(f"Something went wrong answering that: {e}")
        return

    await status_msg.delete()

    pages_cited = sorted({s["page"] for s in result["sources"]})
    reply = f"{result['answer']}\n\n📄 Source pages: {', '.join(str(p) for p in pages_cited)}"
    await send_long_text(message.answer, reply)

    # Let them keep asking about the same book without re-picking it
    awaiting_question[message.from_user.id] = book_id


async def main():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

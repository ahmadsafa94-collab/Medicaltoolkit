"""
"Ask my book" conversation flow: pick a previously-indexed book, then ask it
questions in plain language (NotebookLM-style), repeatedly, until /cancel.

Built as a proper FSM router (State/StatesGroup/FSMContext), the same
pattern as renal_flow.py / calc_flow.py / interaction_flow.py -- NOT as a
bare global dict keyed by user_id, which is how this feature first arrived
from another session. That version's "is this user awaiting a question"
check had no exclusion for slash commands, so a user who tapped a book and
then typed e.g. "/cancel" (meaning to cancel a DIFFERENT in-progress flow,
like a calculator) would have that text swallowed and sent to Claude as a
literal book question instead of reaching /cancel's real handler. Using
aiogram's FSM state instead of an ad-hoc dict fixes that for free -- a user
can only be in one FSM state at a time, so this can never collide with
calc_flow/interaction_flow/renal_flow's own in-progress states either.

Also owns handle_index_request: the "make this book searchable" callback
shown after a PDF is split into chapters. That button's pending session_cache
entry is created by bot.py's handle_pdf_upload right after it registers the
upload into library.py's shared book registry -- the same registry backing
the Book Shelf mini app now, so a book indexed from chat (or from the mini
app's own "Ask questions using AI" button) shows up as searchable in BOTH
places. Indexing no longer deletes the source PDF afterward: the Book
Shelf's reader, chapter division, and summarizer all need the original file
to keep working, so once a book is uploaded its PDF is kept indefinitely
(see library.py's module docstring).
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import library
import pdf_qa
import session_cache
import subscriptions
from config import QA_INDEXING_TIMEOUT_SECONDS
from keyboards import book_picker_kb
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="book_qa_flow")


class BookQAStates(StatesGroup):
    awaiting_question = State()


_NOT_A_COMMAND = F.text & ~F.text.startswith("/")

_QA_ANSWER_TIMEOUT_SECONDS = 30


async def show_book_picker(answer_fn, user_id: int) -> None:
    """
    answer_fn/user_id rather than a Message: a callback_query-triggered
    caller (e.g. bot.py's "study:askbooks" handler) must pass
    callback.from_user.id, NOT callback.message.from_user.id -- the latter
    is the BOT (sender of the message the inline keyboard is attached to),
    not the tapping user.
    """
    all_books = library.list_books(user_id)
    # Every uploaded book has a registry entry now (see library.py), but only
    # ones that have actually been indexed are answerable -- filter here
    # rather than in library.list_books itself, since the Book Shelf mini
    # app needs the FULL list (including not-yet-indexed books) from that
    # same function.
    books = {bid: info for bid, info in all_books.items() if info.get("qa_indexed")}
    if not books:
        await answer_fn(
            "You don't have any searchable books yet. Open 📚 Book Shelf, pick a book, and tap "
            "'Ask questions using AI' to index it -- or upload one via chat and tap "
            "'🔍 Make this book searchable' on the result."
        )
        return
    await answer_fn("Which book do you want to ask about?", reply_markup=book_picker_kb(books))


@router.message(Command("ask"))
async def cmd_ask(message: Message):
    await show_book_picker(message.answer, message.from_user.id)


@router.message(Command("cancel"), BookQAStates.awaiting_question)
async def cmd_cancel_ask(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.callback_query(F.data.startswith("askbook:"))
async def handle_book_pick(callback: CallbackQuery, state: FSMContext):
    book_id = callback.data.split(":", 1)[1]
    book = library.get_book(callback.from_user.id, book_id)
    if book is None:
        await callback.answer("That book isn't available anymore.", show_alert=True)
        return

    await state.set_state(BookQAStates.awaiting_question)
    await state.update_data(book_id=book_id, title=book["title"])
    await callback.answer()
    await callback.message.answer(
        f"📖 Ask a question about *{book['title']}* (or /cancel to stop):", parse_mode="Markdown"
    )


@router.message(BookQAStates.awaiting_question, _NOT_A_COMMAND)
async def handle_book_question(message: Message, state: FSMContext):
    data = await state.get_data()
    book_id = data.get("book_id")
    title = data.get("title", "this book")
    if not book_id:
        await state.clear()
        await message.answer("This session expired. Use /ask to pick a book again.")
        return

    question = (message.text or "").strip()
    if not question:
        await message.answer("Please send your question as text.")
        return

    try:
        subscriptions.check_and_consume(message.from_user.id, "questions")
    except subscriptions.QuotaExceeded as e:
        await message.answer(str(e))
        return

    status_msg = await message.answer("Searching the book...")

    try:
        result = await asyncio.wait_for(pdf_qa.answer_question(book_id, question), timeout=_QA_ANSWER_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await status_msg.edit_text("That took too long. Please try again.")
        return
    except pdf_qa.IndexingError as e:
        await status_msg.edit_text(str(e))
        return
    except Exception:
        logger.exception("Book Q&A failed for book_id=%s", book_id)
        await status_msg.edit_text("Something went wrong answering that. Please try again.")
        return

    try:
        await status_msg.delete()
    except Exception:
        pass  # not critical if the "Searching..." message can't be deleted (e.g. already gone)

    pages_cited = sorted({s["page"] for s in result["sources"]})
    reply = result["answer"]
    if pages_cited:
        reply += f"\n\n📄 Source pages: {', '.join(str(p) for p in pages_cited)}"
    ok = await send_long_text(message.answer, reply)
    if not ok:
        await message.answer("Couldn't send that answer (Telegram rejected the message).")

    # Stay in the same state so they can keep asking about this book without
    # re-picking it from /ask every time.
    await state.update_data(book_id=book_id, title=title)


@router.callback_query(F.data.startswith("index:"))
async def handle_index_request(callback: CallbackQuery):
    pending_id = callback.data.split(":", 1)[1]
    pending = session_cache.get(pending_id)
    if pending is None:
        await callback.answer("This request expired. Open the book from 📚 Book Shelf and try again.", show_alert=True)
        return

    user_id = callback.from_user.id
    book_id = pending["book_id"]
    book = library.get_book(user_id, book_id)
    if book is None:
        await callback.answer("This book is no longer available.", show_alert=True)
        return

    await callback.answer()
    status_msg = await callback.message.answer("Starting indexing...")

    async def progress(text: str):
        try:
            await status_msg.edit_text(text)
        except TelegramBadRequest:
            pass  # message content unchanged or too soon after last edit -- harmless

    try:
        num_chunks = await asyncio.wait_for(
            pdf_qa.build_index(book["pdf_path"], book_id, book["title"], progress_cb=progress),
            timeout=QA_INDEXING_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await status_msg.edit_text("Indexing took too long and timed out. Try again, or use a shorter book.")
        return
    except pdf_qa.IndexingError as e:
        await status_msg.edit_text(f"Couldn't index this book: {e}")
        return
    except Exception:
        logger.exception("Indexing failed for book_id=%s", book_id)
        await status_msg.edit_text("Indexing failed unexpectedly. Please try again.")
        return

    # Note: unlike the earlier version of this feature, the raw PDF is NOT
    # deleted after indexing -- the Book Shelf mini app's reader, chapter
    # division, and summarizer all need the original file to keep working,
    # and this book already has a permanent registry entry from the moment
    # it was uploaded (see library.py). A failed/timed-out attempt above
    # also intentionally does NOT remove the book from the registry: the
    # book itself is still real and still on the shelf, it just isn't
    # indexed for Q&A yet, and the user can retry later.
    library.mark_indexed(user_id, book_id, num_chunks)

    await status_msg.edit_text(
        f"✅ *{book['title']}* is ready ({num_chunks} passages indexed). "
        f"Use /ask or 💬 Ask My Books to ask it a question.",
        parse_mode="Markdown",
    )


def register_book_qa_handlers(dp) -> None:
    dp.include_router(router)

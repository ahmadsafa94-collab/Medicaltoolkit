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
entry is created by bot.py's handle_pdf_upload after moving the original PDF
into a per-user library folder -- see that function's docstring for why the
move (not a plain delete) matters here.
"""

import asyncio
import logging
import os

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import library
import pdf_qa
import session_cache
from config import QA_INDEXING_TIMEOUT_SECONDS
from keyboards import book_picker_kb
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="book_qa_flow")


class BookQAStates(StatesGroup):
    awaiting_question = State()


_NOT_A_COMMAND = F.text & ~F.text.startswith("/")

_QA_ANSWER_TIMEOUT_SECONDS = 30


async def show_book_picker(message: Message) -> None:
    books = library.list_books(message.from_user.id)
    if not books:
        await message.answer(
            "You don't have any searchable books yet. Upload a PDF, then tap "
            "'🔍 Make this book searchable' on the result to enable Q&A for it."
        )
        return
    await message.answer("Which book do you want to ask about?", reply_markup=book_picker_kb(books))


@router.message(Command("ask"))
async def cmd_ask(message: Message):
    await show_book_picker(message)


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
            timeout=QA_INDEXING_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await status_msg.edit_text("Indexing took too long and timed out. Try again, or use a shorter book.")
        library.remove_book(callback.from_user.id, book_id)
        return
    except pdf_qa.IndexingError as e:
        await status_msg.edit_text(f"Couldn't index this book: {e}")
        library.remove_book(callback.from_user.id, book_id)
        return
    except Exception:
        logger.exception("Indexing failed for book_id=%s", book_id)
        await status_msg.edit_text("Indexing failed unexpectedly. Please try again.")
        library.remove_book(callback.from_user.id, book_id)
        return

    library.update_chunk_count(callback.from_user.id, book_id, num_chunks)

    # The raw PDF has done its job (its text is now embedded in the index) --
    # remove it from the per-user library folder so successfully-indexed
    # books don't sit around taking up disk space forever. Books the user
    # uploads but never indexes are NOT cleaned up here; bot.py sweeps those
    # periodically instead (see _cleanup_stale_library_files).
    try:
        os.remove(pending["pdf_path"])
    except OSError:
        pass

    await status_msg.edit_text(
        f"✅ *{pending['title']}* is ready ({num_chunks} passages indexed). "
        f"Use /ask or 💬 Ask My Books to ask it a question.",
        parse_mode="Markdown",
    )


def register_book_qa_handlers(dp) -> None:
    dp.include_router(router)

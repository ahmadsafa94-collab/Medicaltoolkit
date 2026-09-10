"""
Chat flow for Study Tools -> 📓 My Notes. Notes are never AI-generated and
never gated by any quota -- this is plain user-authored text, no Claude
call involved anywhere in this module.
"""

import logging
import re

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import library
import notes

logger = logging.getLogger(__name__)

router = Router(name="notes_flow")

_NOT_A_COMMAND = F.text & ~F.text.startswith("/")
_LEADING_PAGE_RE = re.compile(r"^\s*(\d+)\s*[:\-\.\)]\s*(.+)$", re.DOTALL)


class NotesStates(StatesGroup):
    awaiting_note_text = State()


def _book_picker_kb(books: dict) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"📖 {info['title']}", callback_data=f"notes:book:{book_id}")] for book_id, info in books.items()]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _notes_list_kb(book_id: str, note_ids: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Add note", callback_data=f"notes:add:{book_id}")]]
    for note_id in note_ids:
        rows.append([InlineKeyboardButton(text="🗑 Delete", callback_data=f"notes:del:{book_id}:{note_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "study:notes")
async def handle_study_notes(callback: CallbackQuery):
    await callback.answer()
    books = library.list_books(callback.from_user.id)
    if not books:
        await callback.message.answer("Upload a book first, then come back here.")
        return
    await callback.message.answer("📓 Pick a book:", reply_markup=_book_picker_kb(books))


@router.callback_query(F.data.startswith("notes:book:"))
async def handle_pick_book(callback: CallbackQuery):
    await callback.answer()
    book_id = callback.data.split(":", 2)[2]
    user_id = callback.from_user.id
    book = library.get_book(user_id, book_id)
    if book is None:
        await callback.message.answer("That book isn't available anymore.")
        return

    book_notes = notes.list_notes(user_id, book_id)
    if not book_notes:
        text = f"📓 *{book['title']}*\n\nNo notes yet."
    else:
        lines = [f"📓 *{book['title']}*", ""]
        for n in book_notes[:20]:
            page_label = f"p.{n['page']}" if n.get("page") else "(no page)"
            preview = n["text"][:200]
            lines.append(f"*{page_label}*: {preview}")
            lines.append("")
        text = "\n".join(lines)

    await callback.message.answer(
        text, parse_mode="Markdown", reply_markup=_notes_list_kb(book_id, [n["id"] for n in book_notes[:20]])
    )


@router.callback_query(F.data.startswith("notes:add:"))
async def handle_add_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    book_id = callback.data.split(":", 2)[2]
    user_id = callback.from_user.id
    book = library.get_book(user_id, book_id)
    if book is None:
        await callback.message.answer("That book isn't available anymore.")
        return

    await state.set_state(NotesStates.awaiting_note_text)
    await state.update_data(book_id=book_id, default_page=book.get("bookmark_page"))

    hint = f" (defaults to your bookmarked page {book['bookmark_page']} if you don't include one)" if book.get("bookmark_page") else ""
    await callback.message.answer(
        f"Type your note{hint}. Start with a page number and a colon/dash to set the page explicitly, "
        "e.g. '42: remember this mechanism'. /cancel to abort."
    )


@router.message(Command("cancel"), NotesStates.awaiting_note_text)
async def handle_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.message(NotesStates.awaiting_note_text, _NOT_A_COMMAND)
async def handle_note_text(message: Message, state: FSMContext):
    data = await state.get_data()
    book_id = data.get("book_id")
    default_page = data.get("default_page")
    await state.clear()
    if not book_id:
        await message.answer("This session expired. Tap 📓 My Notes again.")
        return

    raw = message.text.strip()
    match = _LEADING_PAGE_RE.match(raw)
    if match:
        page = int(match.group(1))
        text = match.group(2).strip()
    else:
        page = default_page
        text = raw

    if not text:
        await message.answer("Empty note -- not saved.")
        return

    notes.add_note(message.from_user.id, book_id, page, text)
    page_label = f"page {page}" if page else "no page"
    await message.answer(f"✅ Note saved ({page_label}).")


@router.callback_query(F.data.startswith("notes:del:"))
async def handle_delete(callback: CallbackQuery):
    _, _, book_id, note_id = callback.data.split(":", 3)
    deleted = notes.delete_note(callback.from_user.id, book_id, note_id)
    await callback.answer("Deleted." if deleted else "Already gone.")
    if deleted:
        await callback.message.answer("Note deleted.")


def register_notes_handlers(dp) -> None:
    dp.include_router(router)

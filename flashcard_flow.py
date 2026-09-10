"""
Chat flow for Study Tools -> 🗂 Flashcards: pick a book, generate cards from
one of its chapters, review due cards (SM-2 via flashcards.py), or export
the deck to Anki (.apkg, via anki_export.py).

Card GENERATION is quota-gated the same way chapter summaries are (it's a
comparable single Claude call) -- reviewing already-generated cards costs
nothing and is never gated.
"""

import asyncio
import logging
import os

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import anki_export
import chapter_ai
import flashcards
import library
import subscriptions
from keyboards import (
    flashcard_book_picker_kb,
    flashcard_book_menu_kb,
    flashcard_chapter_picker_kb,
    flashcard_reveal_kb,
    flashcard_rate_kb,
)
from paths import user_dir

logger = logging.getLogger(__name__)

router = Router(name="flashcard_flow")

_GENERATION_TIMEOUT_SECONDS = 90


class FlashcardStates(StatesGroup):
    reviewing = State()


@router.callback_query(F.data == "study:flashcards")
async def handle_study_flashcards(callback: CallbackQuery):
    await callback.answer()
    books = library.list_books(callback.from_user.id)
    if not books:
        await callback.message.answer("Upload a book first (📄 Upload PDF or 📚 Book Shelf), then come back here.")
        return
    await callback.message.answer("🗂 Pick a book:", reply_markup=flashcard_book_picker_kb(books))


@router.callback_query(F.data.startswith("flash:book:"))
async def handle_pick_book(callback: CallbackQuery):
    await callback.answer()
    book_id = callback.data.split(":", 2)[2]
    user_id = callback.from_user.id
    book = library.get_book(user_id, book_id)
    if book is None:
        await callback.message.answer("That book isn't available anymore.")
        return

    due = flashcards.count_due(user_id, book_id)
    has_deck = bool(flashcards.get_deck(user_id, book_id))
    await callback.message.answer(
        f"📖 *{book['title']}*\n\nCards in deck: {len(flashcards.get_deck(user_id, book_id))}",
        parse_mode="Markdown",
        reply_markup=flashcard_book_menu_kb(book_id, due, has_deck),
    )


@router.callback_query(F.data.startswith("flash:genpick:"))
async def handle_gen_pick(callback: CallbackQuery):
    await callback.answer()
    book_id = callback.data.split(":", 2)[2]
    book = library.get_book(callback.from_user.id, book_id)
    if book is None:
        await callback.message.answer("That book isn't available anymore.")
        return
    chapters = book.get("chapters") or []
    if not chapters:
        await callback.message.answer("Divide this book into chapters first (📚 Book Shelf -> Divide into chapters).")
        return
    await callback.message.answer("Pick a chapter to generate cards from:", reply_markup=flashcard_chapter_picker_kb(book_id, chapters))


@router.callback_query(F.data.startswith("flash:gen:"))
async def handle_generate(callback: CallbackQuery):
    await callback.answer("Generating...")
    _, _, book_id, chapter_index_str = callback.data.split(":", 3)
    chapter_index = int(chapter_index_str)
    user_id = callback.from_user.id

    book = library.get_book(user_id, book_id)
    if book is None:
        await callback.message.answer("That book isn't available anymore.")
        return
    chapters = book.get("chapters") or []
    if not (0 <= chapter_index < len(chapters)):
        await callback.message.answer("That chapter isn't available anymore.")
        return
    chapter = chapters[chapter_index]

    try:
        subscriptions.check_and_consume(user_id, "summaries")
    except subscriptions.QuotaExceeded as e:
        await callback.message.answer(str(e))
        return

    language = subscriptions.get_language(user_id)
    try:
        text, _truncated = await asyncio.to_thread(
            chapter_ai.extract_text_for_page_range,
            book["pdf_path"], chapter["start_page"], chapter["end_page"], chapter_ai.MAX_CHARS_PER_CHAPTER,
        )
        raw_cards = await asyncio.wait_for(
            asyncio.to_thread(chapter_ai.generate_flashcards, chapter["title"], text, 12, language),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.exception("Flashcard generation failed for book=%s chapter=%s", book_id, chapter_index)
        await callback.message.answer(f"Couldn't generate flashcards: {e}")
        return

    added = flashcards.add_cards(user_id, book_id, book["title"], chapter["title"], raw_cards)
    await callback.message.answer(f"✅ Added {len(added)} card(s) from '{chapter['title']}'.")


@router.callback_query(F.data.startswith("flash:review:"))
async def handle_start_review(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    book_id = callback.data.split(":", 2)[2]
    user_id = callback.from_user.id
    queue = flashcards.due_cards(user_id, book_id, limit=50)
    if not queue:
        await callback.message.answer("Nothing due right now -- nice work staying on top of it.")
        return
    await state.set_state(FlashcardStates.reviewing)
    await state.update_data(book_id=book_id, queue=[c["id"] for c in queue], index=0)
    await _show_current_card(callback.message.answer, user_id, book_id, queue[0])


async def _show_current_card(answer_fn, user_id: int, book_id: str, card: dict):
    await answer_fn(f"🗂 *{card['front']}*", parse_mode="Markdown", reply_markup=flashcard_reveal_kb())


@router.callback_query(F.data == "flash:reveal", FlashcardStates.reviewing)
async def handle_reveal(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    book_id = data.get("book_id")
    queue = data.get("queue") or []
    index = data.get("index", 0)
    if book_id is None or index >= len(queue):
        await state.clear()
        await callback.message.answer("This review session expired. Tap ▶️ Review due cards again.")
        return

    deck = flashcards.get_deck(callback.from_user.id, book_id)
    card = next((c for c in deck if c["id"] == queue[index]), None)
    if card is None:
        await callback.message.answer("This card was removed. Skipping.")
        await _advance(callback, state)
        return

    await callback.message.answer(card["back"], reply_markup=flashcard_rate_kb())


@router.callback_query(F.data.startswith("flash:rate:"), FlashcardStates.reviewing)
async def handle_rate(callback: CallbackQuery, state: FSMContext):
    quality = int(callback.data.split(":", 2)[2])
    data = await state.get_data()
    book_id = data.get("book_id")
    queue = data.get("queue") or []
    index = data.get("index", 0)
    if book_id is None or index >= len(queue):
        await callback.answer()
        await state.clear()
        return

    flashcards.review_card(callback.from_user.id, book_id, queue[index], quality)
    await callback.answer("Recorded.")
    await _advance(callback, state)


async def _advance(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    book_id = data.get("book_id")
    queue = data.get("queue") or []
    index = data.get("index", 0) + 1

    if book_id is None or index >= len(queue):
        await state.clear()
        await callback.message.answer("🎉 All done for now!")
        return

    await state.update_data(index=index)
    deck = flashcards.get_deck(callback.from_user.id, book_id)
    card = next((c for c in deck if c["id"] == queue[index]), None)
    if card is None:
        await _advance(callback, state)  # skip a since-deleted card
        return
    await _show_current_card(callback.message.answer, callback.from_user.id, book_id, card)


@router.callback_query(F.data.startswith("flash:export:"))
async def handle_export(callback: CallbackQuery):
    await callback.answer("Building .apkg...")
    book_id = callback.data.split(":", 2)[2]
    user_id = callback.from_user.id
    book = library.get_book(user_id, book_id)
    if book is None:
        await callback.message.answer("That book isn't available anymore.")
        return

    cards = flashcards.get_deck(user_id, book_id)
    workdir = user_dir(user_id)
    output_path = os.path.join(workdir, f"_tmp_anki_{book_id}.apkg")
    try:
        await asyncio.to_thread(anki_export.export_deck_to_apkg, book["title"], book_id, cards, output_path)
        with open(output_path, "rb") as f:
            apkg_bytes = f.read()
    except anki_export.AnkiExportError as e:
        await callback.message.answer(f"Couldn't export: {e}")
        return
    finally:
        try:
            os.remove(output_path)
        except OSError:
            pass

    filename = f"{book['title'][:50].strip() or 'deck'}.apkg"
    try:
        await callback.message.answer_document(BufferedInputFile(apkg_bytes, filename=filename), caption=f"🗂 Anki deck -- {book['title']}")
    except TelegramAPIError:
        logger.exception("Failed to send .apkg export")
        await callback.message.answer("Built the deck but couldn't send it (Telegram rejected the file).")


@router.callback_query(F.data.startswith("flash:delete:"))
async def handle_delete(callback: CallbackQuery):
    await callback.answer("Deleted.")
    book_id = callback.data.split(":", 2)[2]
    flashcards.delete_deck(callback.from_user.id, book_id)
    await callback.message.answer("Deck deleted.")


def register_flashcard_handlers(dp) -> None:
    dp.include_router(router)

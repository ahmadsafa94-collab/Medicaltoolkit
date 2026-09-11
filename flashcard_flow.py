"""
Chat flow for Study Tools -> 🗂 Flashcards, and the same entry point the
📚 Book Shelf mini app's own Flashcards section drives through (this module
covers the chat side; webapp_api.py's /api/books/{book_id}/flashcards/*
endpoints cover the mini app side -- both read/write the exact same
flashcards.py-backed deck per book, so cards generated or reviewed in one
place show up in the other).

Pick a book, select any number of its chapters and how many cards to
generate (AI, via chapter_ai.generate_flashcards_multi), review them (SM-2
via flashcards.py -- due/all/hard modes), or export the deck to Anki
(.apkg, via anki_export.py).

Card GENERATION is quota-gated the same way chapter summaries are (it's a
comparable single Claude call) -- reviewing already-generated cards costs
nothing and is never gated.
"""

import asyncio
import logging
import os

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message

import anki_export
import chapter_ai
import flashcards
import library
import subscriptions
from config import FLASHCARD_GENERATION_TIMEOUT_SECONDS
from keyboards import (
    flashcard_book_picker_kb,
    flashcard_book_menu_kb,
    flashcard_chapter_multiselect_kb,
    flashcard_chapter_filter_kb,
    flashcard_review_mode_kb,
    flashcard_count_kb,
    flashcard_reveal_kb,
    flashcard_rate_kb,
)
from paths import user_dir

logger = logging.getLogger(__name__)

router = Router(name="flashcard_flow")


class FlashcardStates(StatesGroup):
    selecting_chapters = State()
    awaiting_custom_count = State()
    reviewing = State()


@router.callback_query(F.data == "study:flashcards")
async def handle_study_flashcards(callback: CallbackQuery):
    await callback.answer()
    books = library.list_books(callback.from_user.id)
    if not books:
        await callback.message.answer("Upload a book first (✂️ PDF Splitter or 📚 Book Shelf), then come back here.")
        return
    await callback.message.answer("🗂 Pick a book:", reply_markup=flashcard_book_picker_kb(books))


async def _send_book_menu(answer_fn, user_id: int, book_id: str, book_title: str):
    deck = flashcards.get_deck(user_id, book_id)
    chapter_breakdown = flashcards.chapter_breakdown(user_id, book_id)
    await answer_fn(
        f"📖 *{book_title}*\n\nCards in deck: {len(deck)}",
        parse_mode="Markdown",
        reply_markup=flashcard_book_menu_kb(
            book_id,
            flashcards.count_due(user_id, book_id),
            flashcards.count_all(user_id, book_id),
            flashcards.count_hard(user_id, book_id),
            bool(deck),
            len(chapter_breakdown) > 1,
        ),
    )


@router.callback_query(F.data.startswith("flash:book:"))
async def handle_pick_book(callback: CallbackQuery):
    await callback.answer()
    book_id = callback.data.split(":", 2)[2]
    user_id = callback.from_user.id
    book = library.get_book(user_id, book_id)
    if book is None:
        await callback.message.answer("That book isn't available anymore.")
        return
    await _send_book_menu(callback.message.answer, user_id, book_id, book["title"])


@router.callback_query(F.data.startswith("flash:genpick:"))
async def handle_gen_pick(callback: CallbackQuery, state: FSMContext):
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
    await state.set_state(FlashcardStates.selecting_chapters)
    await state.update_data(book_id=book_id, selected=[])
    await callback.message.answer(
        "Pick the chapter(s) to generate cards from (tap to toggle):",
        reply_markup=flashcard_chapter_multiselect_kb(book_id, chapters, set()),
    )


@router.callback_query(F.data.startswith("flash:chtoggle:"), FlashcardStates.selecting_chapters)
async def handle_chapter_toggle(callback: CallbackQuery, state: FSMContext):
    _, _, book_id, index_str = callback.data.split(":", 3)
    index = int(index_str)
    data = await state.get_data()
    if data.get("book_id") != book_id:
        await callback.answer()
        return

    book = library.get_book(callback.from_user.id, book_id)
    chapters = (book or {}).get("chapters") or []
    if book is None or not (0 <= index < len(chapters)):
        await callback.answer("That chapter isn't available anymore.", show_alert=True)
        return

    selected = set(data.get("selected") or [])
    if index in selected:
        selected.discard(index)
    else:
        selected.add(index)
    await state.update_data(selected=list(selected))
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=flashcard_chapter_multiselect_kb(book_id, chapters, selected))
    except TelegramAPIError:
        pass  # unchanged markup (re-tapping the same toggle twice fast) -- harmless


@router.callback_query(F.data.startswith("flash:chcancel:"), FlashcardStates.selecting_chapters)
async def handle_chapter_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    await callback.message.answer("Cancelled.")


@router.callback_query(F.data.startswith("flash:chdone:"), FlashcardStates.selecting_chapters)
async def handle_chapter_done(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    book_id = data.get("book_id")
    selected = data.get("selected") or []
    if not book_id or not selected:
        await callback.message.answer("Select at least one chapter first.")
        return
    await callback.message.answer(
        f"How many flashcards? (max {chapter_ai.MAX_CARDS_PER_GENERATION})", reply_markup=flashcard_count_kb(book_id)
    )


@router.message(Command("cancel"), FlashcardStates.selecting_chapters)
@router.message(Command("cancel"), FlashcardStates.awaiting_custom_count)
async def handle_generation_cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.callback_query(F.data.startswith("flash:countcustom:"), FlashcardStates.selecting_chapters)
async def handle_count_custom_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(FlashcardStates.awaiting_custom_count)
    await callback.message.answer(
        f"Send the number of flashcards to generate (1-{chapter_ai.MAX_CARDS_PER_GENERATION}). /cancel to abort."
    )


@router.message(FlashcardStates.awaiting_custom_count, F.text & ~F.text.startswith("/"))
async def handle_count_custom_input(message: Message, state: FSMContext):
    try:
        num_cards = int(message.text.strip())
        if not (1 <= num_cards <= chapter_ai.MAX_CARDS_PER_GENERATION):
            raise ValueError
    except ValueError:
        await message.answer(
            f"Please send a whole number between 1 and {chapter_ai.MAX_CARDS_PER_GENERATION}, or /cancel to abort."
        )
        return

    data = await state.get_data()
    book_id = data.get("book_id")
    selected = data.get("selected") or []
    await state.clear()
    if not book_id or not selected:
        await message.answer("This session expired. Open 🗂 Flashcards and try again.")
        return
    await _generate(message.answer, message.from_user.id, book_id, selected, num_cards)


@router.callback_query(F.data.startswith("flash:count:"))
async def handle_count_preset(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Generating...")
    _, _, book_id, num_cards_str = callback.data.split(":", 3)
    num_cards = int(num_cards_str)
    data = await state.get_data()
    selected = data.get("selected") or []
    await state.clear()
    if data.get("book_id") != book_id or not selected:
        await callback.message.answer("This session expired. Open 🗂 Flashcards and try again.")
        return
    await _generate(callback.message.answer, callback.from_user.id, book_id, selected, num_cards)


async def _generate(answer_fn, user_id: int, book_id: str, chapter_indices: list[int], num_cards: int):
    book = library.get_book(user_id, book_id)
    if book is None:
        await answer_fn("That book isn't available anymore.")
        return
    all_chapters = book.get("chapters") or []
    try:
        selected_chapters = [all_chapters[i] for i in chapter_indices]
    except IndexError:
        await answer_fn("One of those chapters isn't available anymore. Please try again.")
        return

    try:
        subscriptions.check_and_consume(user_id, "summaries")
    except subscriptions.QuotaExceeded as e:
        await answer_fn(str(e))
        return

    # Look up cards already generated from any of these same chapters so the
    # prompt can steer away from repeating them (see chapter_ai.generate_flashcards's
    # existing_fronts docstring).
    existing = flashcards.cards_for_chapters(user_id, book_id, chapter_indices)
    existing_fronts = [c["front"] for c in existing]

    status = await answer_fn(f"Generating {num_cards} flashcard(s) from {len(selected_chapters)} chapter(s)...")
    language = subscriptions.get_language(user_id)
    try:
        raw_cards = await asyncio.wait_for(
            asyncio.to_thread(
                chapter_ai.generate_flashcards_multi,
                book["title"],
                book["pdf_path"],
                selected_chapters,
                num_cards,
                language,
                existing_fronts,
            ),
            timeout=FLASHCARD_GENERATION_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.exception("Flashcard generation failed for book=%s chapters=%s", book_id, chapter_indices)
        try:
            await status.edit_text(f"Couldn't generate flashcards: {e}")
        except TelegramAPIError:
            await answer_fn(f"Couldn't generate flashcards: {e}")
        return

    chapter_label = ", ".join(c["title"] for c in selected_chapters)[:200]
    added = flashcards.add_cards(user_id, book_id, book["title"], chapter_label, chapter_indices, raw_cards)
    try:
        await status.edit_text(f"✅ Added {len(added)} new card(s) from {len(selected_chapters)} chapter(s).")
    except TelegramAPIError:
        await answer_fn(f"✅ Added {len(added)} new card(s) from {len(selected_chapters)} chapter(s).")


_REVIEW_QUEUE_FN = {
    "due": lambda user_id, book_id, chap: flashcards.due_cards(user_id, book_id, limit=50, chapter_index=chap),
    "all": lambda user_id, book_id, chap: flashcards.all_cards(user_id, book_id, limit=100, chapter_index=chap),
    "hard": lambda user_id, book_id, chap: flashcards.hard_cards(user_id, book_id, limit=100, chapter_index=chap),
}
_REVIEW_EMPTY_MESSAGE = {
    "due": "Nothing due right now -- nice work staying on top of it.",
    "all": "No cards in this deck (or this chapter) yet -- generate some first.",
    "hard": "No hard cards right now -- nice work.",
}


def _parse_chapter_token(token: str) -> int | None:
    return None if token == "all" else int(token)


@router.callback_query(F.data.startswith("flash:review:"))
async def handle_start_review(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    _, _, book_id, mode, chap_token = callback.data.split(":", 4)
    chapter_index = _parse_chapter_token(chap_token)
    user_id = callback.from_user.id
    queue = _REVIEW_QUEUE_FN.get(mode, _REVIEW_QUEUE_FN["due"])(user_id, book_id, chapter_index)
    if not queue:
        await callback.message.answer(_REVIEW_EMPTY_MESSAGE.get(mode, _REVIEW_EMPTY_MESSAGE["due"]))
        return
    await state.set_state(FlashcardStates.reviewing)
    await state.update_data(book_id=book_id, queue=[c["id"] for c in queue], index=0)
    await _show_current_card(callback.message.answer, user_id, book_id, queue[0])


@router.callback_query(F.data.startswith("flash:chapfilter:"))
async def handle_chapter_filter_menu(callback: CallbackQuery):
    await callback.answer()
    book_id = callback.data.split(":", 2)[2]
    user_id = callback.from_user.id
    book = library.get_book(user_id, book_id)
    if book is None:
        await callback.message.answer("That book isn't available anymore.")
        return

    breakdown = flashcards.chapter_breakdown(user_id, book_id)
    if not breakdown:
        await callback.message.answer("No cards in this deck yet -- generate some first.")
        return

    chapters = book.get("chapters") or []
    chapter_counts = {}
    for idx, count in breakdown.items():
        title = chapters[idx]["title"] if 0 <= idx < len(chapters) else f"Chapter {idx + 1}"
        chapter_counts[idx] = (title, count)

    await callback.message.answer("📂 Pick a chapter to review:", reply_markup=flashcard_chapter_filter_kb(book_id, chapter_counts))


@router.callback_query(F.data.startswith("flash:chapfilterpick:"))
async def handle_chapter_filter_pick(callback: CallbackQuery):
    await callback.answer()
    _, _, book_id, chapter_index_str = callback.data.split(":", 3)
    chapter_index = int(chapter_index_str)
    user_id = callback.from_user.id

    await callback.message.answer(
        "Review mode:",
        reply_markup=flashcard_review_mode_kb(
            book_id,
            chapter_index,
            flashcards.count_due(user_id, book_id, chapter_index=chapter_index),
            flashcards.count_all(user_id, book_id, chapter_index=chapter_index),
            flashcards.count_hard(user_id, book_id, chapter_index=chapter_index),
        ),
    )


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
        await callback.message.answer("This review session expired. Open 🗂 Flashcards and start a new review.")
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

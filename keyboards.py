"""
Fixed (persistent) keyboard shown at the bottom of the chat, plus the
inline-search trigger button used for drug-name autocomplete.
"""

from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

BTN_DOSE = "💊 Dose Lookup"
BTN_UPLOAD = "📄 Upload PDF"
BTN_ASK = "💬 Ask My Books"
BTN_HELP = "ℹ️ Help"

main_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_DOSE)],
        [KeyboardButton(text=BTN_UPLOAD), KeyboardButton(text=BTN_ASK)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


def drug_sections_kb(cache_id: str, sections_available: list[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """
    Buttons for each available label section (Dosage, Contraindications, etc.)
    plus a 'show everything' option. Tapping one sends just that section.
    sections_available: list of (concept_key, field_label, emoji) tuples.
    """
    rows = []
    row = []
    for concept, field_label, emoji in sections_available:
        row.append(
            InlineKeyboardButton(
                text=f"{emoji} {field_label}",
                callback_data=f"sec:{cache_id}:{concept}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton(text="📋 Show everything", callback_data=f"sec:{cache_id}:_all")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def make_searchable_kb(pending_id: str) -> InlineKeyboardMarkup:
    """Shown after chapter-splitting finishes, offering to index the book for Q&A."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Make this book searchable (Ask AI)", callback_data=f"index:{pending_id}")]
        ]
    )


def book_picker_kb(books: dict) -> InlineKeyboardMarkup:
    """
    One button per indexed book. books: {book_id: {"title": ..., "num_chunks": ...}}
    """
    rows = [
        [InlineKeyboardButton(text=f"📖 {info['title']}", callback_data=f"askbook:{book_id}")]
        for book_id, info in books.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def drug_search_inline_kb(bot_username: str) -> InlineKeyboardMarkup:
    """
    A button that, when tapped, switches the chat's text input into inline
    mode pre-filled with '@yourbot '. Typing after that triggers Telegram's
    live inline-query suggestions (see bot.py's inline_query handler).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Start typing a drug name...",
                    switch_inline_query_current_chat="",
                )
            ]
        ]
    )

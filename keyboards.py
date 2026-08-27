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
BTN_HELP = "ℹ️ Help"

main_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_DOSE)],
        [KeyboardButton(text=BTN_UPLOAD), KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


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

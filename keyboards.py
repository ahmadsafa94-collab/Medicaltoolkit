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
    rows.append([InlineKeyboardButton(text="🧮 Calculate dose by renal function", callback_data=f"rc:start:{cache_id}")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def renal_mode_kb() -> InlineKeyboardMarkup:
    """
    First step of the renal-calculator flow: either the user already has a
    lab value (eGFR or CrCl -- these are NOT the same measurement, so which
    one matters), or wants the bot to calculate one from patient parameters.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💧 I know the eGFR (mL/min/1.73m²)", callback_data="rc:mode:egfr_direct")],
            [InlineKeyboardButton(text="💧 I know the CrCl (mL/min)", callback_data="rc:mode:crcl_direct")],
            [InlineKeyboardButton(text="🧮 Calculate eGFR (CKD-EPI)", callback_data="rc:mode:egfr_calc")],
            [InlineKeyboardButton(text="🧮 Calculate CrCl (Cockcroft-Gault)", callback_data="rc:mode:crcl_calc")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="rc:cancel")],
        ]
    )


def renal_sex_kb() -> InlineKeyboardMarkup:
    """Sex selection -- required as a direct input by both the CKD-EPI and Cockcroft-Gault equations."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Male", callback_data="rc:sex:M"),
                InlineKeyboardButton(text="Female", callback_data="rc:sex:F"),
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="rc:cancel")],
        ]
    )


def renal_unit_kb() -> InlineKeyboardMarkup:
    """
    Serum creatinine unit selection. US labs typically report mg/dL; many
    other countries (including Lebanon) report umol/L -- these differ by a
    factor of ~88.4, so getting this wrong would badly skew the result.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="mg/dL (US-style)", callback_data="rc:unit:mgdl"),
                InlineKeyboardButton(text="µmol/L (SI/most other countries)", callback_data="rc:unit:umol"),
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="rc:cancel")],
        ]
    )


def renal_cancel_kb() -> InlineKeyboardMarkup:
    """Just a cancel button, shown alongside plain-text entry prompts (age, weight, creatinine, direct value)."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="rc:cancel")]])


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

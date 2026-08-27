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
BTN_CALC = "🧮 Calculators"
BTN_INTERACTIONS = "🔀 Interactions"

main_menu_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_DOSE), KeyboardButton(text=BTN_CALC)],
        [KeyboardButton(text=BTN_INTERACTIONS), KeyboardButton(text=BTN_UPLOAD)],
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
    rows.append([InlineKeyboardButton(text="🧮 Calculate dose by renal function", callback_data=f"rc:start:{cache_id}")])
    rows.append([InlineKeyboardButton(text="🔖 Bookmark this drug", callback_data=f"bm:add:{cache_id}")])
    rows.append([InlineKeyboardButton(text="📄 Export as PDF", callback_data=f"pdfexp:{cache_id}")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def recent_list_kb(names: list[str], source: str) -> InlineKeyboardMarkup:
    """
    Tap-to-relookup buttons for /recent and /bookmarks. source is "recent" or
    "bookmark" -- the handler re-fetches that list by index at tap time
    (rather than encoding the drug name itself in callback_data), so a long
    combination-drug name can never blow past Telegram's 64-byte
    callback_data limit.
    """
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"redo:{source}:{i}")]
        for i, name in enumerate(names)
    ]
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


def calc_menu_kb(calculators: list[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """
    Top-level /calculators menu. calculators: list of (calc_id, title, emoji)
    tuples, 2 buttons per row, in whatever order the caller passes.
    """
    rows = []
    row = []
    for calc_id, title, emoji in calculators:
        row.append(InlineKeyboardButton(text=f"{emoji} {title}", callback_data=f"cf:start:{calc_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calc_cancel_kb() -> InlineKeyboardMarkup:
    """Cancel button shown alongside a calculator's plain-text number-entry prompts."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cf:cancel")]])


def interaction_menu_kb(drug_count: int) -> InlineKeyboardMarkup:
    """
    Buttons shown alongside the interaction checker's free-text 'type a drug
    name to add it' prompt. 'Check interactions' only appears once there are
    at least 2 drugs (nothing to cross-check with just 1).
    """
    rows = []
    if drug_count > 0:
        rows.append([InlineKeyboardButton(text="🗑 Remove last", callback_data="ix:remove_last")])
    if drug_count >= 2:
        rows.append([InlineKeyboardButton(text="✅ Check Interactions", callback_data="ix:check")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="ix:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chapter_ai_kb(cache_id: str) -> InlineKeyboardMarkup:
    """Buttons attached to a just-sent chapter PDF: on-demand AI summary / self-test quiz for that chapter."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Summarize", callback_data=f"chai:sum:{cache_id}"),
                InlineKeyboardButton(text="❓ Quiz me", callback_data=f"chai:quiz:{cache_id}"),
            ]
        ]
    )


def calc_choice_kb(field_index: int, options: list[tuple[str, object]]) -> InlineKeyboardMarkup:
    """
    Buttons for a calculator's choice/yes-no field. options: list of
    (label, value) tuples -- the value itself never goes in callback_data
    (only its index does), so it can be any type (bool, str, etc.).
    field_index is included so a stale tap from an earlier step (or an
    earlier calculator run) can be detected and rejected.
    """
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"cf:ans:{field_index}:{i}")]
        for i, (label, _value) in enumerate(options)
    ]
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cf:cancel")])
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

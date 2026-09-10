"""
Supported response languages for AI-generated content (chapter summaries,
quizzes, Ask-AI answers, mnemonics, ECG/lab interpretation, OSCE cases).

This is deliberately the ONLY piece of multi-language support in this round:
every AI generation call already threads a `language` parameter through to
its system prompt (see chapter_ai.py, quiz_ai.py, pdf_qa.py, ecg_lab_ai.py)
asking Claude to respond in the user's chosen language -- which covers the
high-value content with no translation data to maintain. Fixed UI strings
(button labels, static bot messages) are NOT translated in this round; they
stay in English regardless of the user's chosen language. Hand-translating
dozens of UI strings without a native speaker to check them risks shipping
confidently-wrong medical terminology, which is a worse outcome than a
consistent English UI around correctly-localized AI content. Expanding this
later just means adding a UI_STRINGS dict and threading a lookup through
each hardcoded string -- nothing here needs to change to support that.
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# A deliberately short, high-coverage list rather than every language Claude
# can technically produce -- these cover the large majority of medical
# students worldwide who study in a language other than English. Add more
# by just adding an entry here; nothing else needs to change.
SUPPORTED_LANGUAGES = [
    "English",
    "Arabic",
    "French",
    "Spanish",
    "Portuguese",
    "German",
    "Turkish",
    "Urdu",
    "Hindi",
    "Indonesian",
]


def language_picker_kb() -> InlineKeyboardMarkup:
    rows = []
    row = []
    for lang in SUPPORTED_LANGUAGES:
        row.append(InlineKeyboardButton(text=lang, callback_data=f"lang:set:{lang}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)

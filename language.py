"""
Supported response languages for AI-generated content (chapter summaries,
quizzes, Ask-AI answers, mnemonics, ECG/lab interpretation).

The chosen language reaches the user in three ways:

  1. AI-generated content -- every generation call threads a `language`
     parameter through to its system prompt (chapter_ai.py, quiz_ai.py,
     pdf_qa.py, ecg_lab_ai.py, drug_qa.py).
  2. Menu and button labels -- translated by Claude and cached per language;
     see ui_text.py.
  3. What the user TYPES -- a drug or glossary name written in their own
     script is resolved back to the English the FDA/glossary data uses;
     see name_resolver.py.

Hand-translating medical UI without a native speaker to check it risks
shipping confidently-wrong terminology, which is why (2) is done by Claude
against the real term rather than by a hardcoded string table, and why
every lookup in ui_text.py falls back to English rather than guessing.
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
    "Persian",
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

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

# Every language the bot offers. Short on purpose: each one carries a real
# cost beyond the picker entry -- the whole fixed-message set is translated
# and cached per language (ui_text.warm_all_messages), and each is a
# wording nobody on the team can proofread. Add one by adding it here;
# nothing else needs to change, though an RTL language must also be listed
# in bidi_text.RTL_LANGUAGES or its mixed English will render out of order.
SUPPORTED_LANGUAGES = [
    "English",
    "Arabic",
    "Persian",
    "Russian",
]


def normalize(language: str | None) -> str:
    """
    The language to actually use, falling back to English for anything not
    on the list above.

    Applied on READ (subscriptions.get_language) rather than by migrating
    stored records: a user whose language was dropped from the list simply
    gets English from then on, with no migration pass to write and nothing
    to go wrong if the list changes again. Their stored value is left
    alone, so re-adding a language restores whoever had picked it.
    """
    if not language:
        return "English"
    for supported in SUPPORTED_LANGUAGES:
        if supported.lower() == language.strip().lower():
            return supported
    return "English"


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

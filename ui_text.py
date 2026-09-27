"""
AI-translated UI labels -- the menu titles and button captions, shown in
whatever language the user picked (see language.py).

Until now only AI-GENERATED content followed the user's language; the
buttons around it stayed English, because hand-translating medical UI
without a native speaker to check it risks shipping confidently-wrong
terminology. Claude does the translating instead, once per language, and
the result is cached to disk -- so the cost is one call the first time
anybody selects a language, and zero from then on for every user of it.

Two rules make this safe to add to an already-working bot:

  1. English is never translated and never cached -- it IS the canonical
     text. An English user's bot is byte-for-byte what it was before.
  2. Every lookup falls back to the English string. A translation that is
     missing, half-written or failed shows English rather than a blank
     button, so the bot is never unusable because a Claude call failed.

The subtle part is that a reply-keyboard button IS its text: aiogram
matches "🧠 Study Tools" to a handler by string equality. Translate the
label and every one of those handlers stops matching. canonical() solves
that by mapping any known translation of a label back to its English
original, and keyboards.ButtonText() is the filter built on it -- so a
Persian user tapping "🧠 ابزار مطالعه" reaches the same handler.
"""

import json
import logging
import os
import re
import threading

import cost_ledger
from config import CLAUDE_MODEL, STORAGE_DIR
from pdf_processor import client  # reuse the one Anthropic client instance

logger = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(STORAGE_DIR, "_admin", "ui_translations")

# Every string this module translates. Deliberately a fixed, explicit list
# rather than "whatever gets passed in": it's what makes the one-call-per-
# language batch possible, and it keeps the translated surface reviewable.
# Adding a string here is all that's needed to have it translated.
UI_STRINGS = [
    # Main menu (reply keyboard) -- these double as handler filters, see canonical()
    "📚 BOOK SHELF",
    "🩺 Clinical Tools",
    "🧠 Study Tools",
    "⭐ My Plan",
    "🌐 Language",
    "🛠 Admin Panel",
    "🐞 Report a problem / Give Feedback",
    "🆘 Support",
    # Clinical Tools submenu
    "🫀 ECG Interpretation",
    "🧪 Lab Interpretation",
    "🧮 Calculators",
    "💊 Ask About Drugs",
    "🔀 Drug Interactions",
    "💊 Drug Lookup",
    # Study Tools submenu
    "🗂 Flashcards",
    "✂️ PDF Splitter",
    "📓 My Notes",
    "💬 Ask My Books",
    "🔖 Bookmarks",
    # Submenu headers
    "🩺 Clinical Tools",
    "🧠 Study Tools",
    "Pick one:",
]

# language -> {english: translated}. Loaded lazily from disk, then kept in
# memory: every keyboard render hits this, so re-reading JSON each time
# would be pure waste.
_cache: dict[str, dict[str, str]] = {}
# translated label -> english label, across ALL languages, for canonical()
_reverse: dict[str, str] = {}
_lock = threading.Lock()


def _cache_path(lang: str) -> str:
    # Language names come from language.SUPPORTED_LANGUAGES, but this builds
    # a filesystem path, so it is sanitized rather than trusted.
    safe = re.sub(r"[^\w-]", "_", lang)[:40] or "unknown"
    return os.path.join(_CACHE_DIR, f"{safe}.json")


def is_english(language: str | None) -> bool:
    return not language or language.strip().lower() == "english"


def _load(lang: str) -> dict[str, str]:
    """Translations for `lang` from memory, else disk, else empty."""
    if lang in _cache:
        return _cache[lang]
    data = {}
    path = _cache_path(lang)
    if os.path.exists(path):
        try:
            with open(path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = {k: v for k, v in loaded.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
        except (json.JSONDecodeError, OSError):
            logger.exception("Unreadable UI translation cache for %s -- treating as empty", lang)
    with _lock:
        _cache[lang] = data
        for english, translated in data.items():
            _reverse.setdefault(translated, english)
    return data


def _save(lang: str, data: dict[str, str]) -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    path = _cache_path(lang)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    with _lock:
        _cache[lang] = data
        for english, translated in data.items():
            _reverse.setdefault(translated, english)


def t(language: str | None, english: str) -> str:
    """The label in `language`, or the English original if it isn't translated."""
    if is_english(language):
        return english
    return _load(language).get(english) or english


def canonical(text: str) -> str:
    """
    Map a label in any language back to its English original.

    This is what keeps handlers working once buttons are translated: an
    unknown string is returned unchanged, so non-button text (a question, a
    drug name) passes straight through untouched.
    """
    if text in _reverse:
        return _reverse[text]
    # A language nobody has loaded in this process yet: fault its cache in,
    # then retry once. Cheap because _load memoizes.
    for lang in _known_languages():
        if lang not in _cache:
            _load(lang)
            if text in _reverse:
                return _reverse[text]
    return text


def _known_languages() -> list[str]:
    try:
        return [f[:-5] for f in os.listdir(_CACHE_DIR) if f.endswith(".json")]
    except OSError:
        return []


def ensure_language(language: str) -> bool:
    """
    Make sure `language` is translated and cached, translating it now if not.

    Returns True if translations are available afterwards. Blocking and
    synchronous on purpose -- it's called when a user picks a language, the
    one moment a short wait is both expected and explainable, and it means
    the very next keyboard they see is already translated.
    """
    if is_english(language):
        return True
    existing = _load(language)
    missing = [s for s in dict.fromkeys(UI_STRINGS) if s not in existing]
    if not missing:
        return True

    try:
        translated = _translate(language, missing)
    except Exception:
        logger.exception("UI translation failed for %s -- labels stay in English", language)
        return bool(existing)

    if not translated:
        return bool(existing)
    merged = {**existing, **translated}
    _save(language, merged)
    return True


def _translate(language: str, strings: list[str]) -> dict[str, str]:
    """One Claude call for the whole batch; returns {english: translated} for what came back usable."""
    system_prompt = (
        f"Translate each UI label of a medical study app from English into {language}.\n\n"
        "Rules:\n"
        "- Reply with ONLY a JSON object mapping each original English string to its translation. No "
        "markdown fences, no commentary.\n"
        "- Keep every emoji exactly as it is and in the same position as the English.\n"
        "- These are BUTTON labels: keep them short, ideally no longer than the English.\n"
        "- Use the wording a clinician or medical student actually uses in that language for medical "
        "terms (ECG, lab, dose); do not invent literal word-for-word translations of technical terms.\n"
        "- If a term is normally left in English in that language, leave it in English."
    )
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": json.dumps(strings, ensure_ascii=False)}],
    )
    try:
        cost_ledger.record_claude_response("ui_translation", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("UI translation for %s was not valid JSON -- discarding", language)
        return {}
    if not isinstance(parsed, dict):
        return {}

    # Only keep entries for strings actually asked about, with a non-empty
    # translation: a partial reply should give partial translation, not a
    # cache full of junk keys that never match anything.
    wanted = set(strings)
    return {
        k: v.strip()
        for k, v in parsed.items()
        if isinstance(k, str) and isinstance(v, str) and k in wanted and v.strip()
    }

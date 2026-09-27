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

import contextlib
import contextvars
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

# Fixed messages common enough to be worth translating up front, in the
# same batch as the labels, rather than one Claude call each the first time
# a user happens to hit them. Everything NOT listed here still gets
# translated on demand (see translate_message) -- this list only decides
# what is already warm the moment someone switches language. Only strings
# with no interpolated values belong here; a message with a drug name or a
# number in it would never match the cache key anyway.
COMMON_MESSAGES = [
    "Changing the Language",
    "Cancelled.",
    "Loading...",
    "Please send your question as text.",
    "That took too long. Please try again.",
    "Something went wrong answering that. Please try again.",
    "The FDA database took too long to respond. Please try again in a moment.",
    "That doesn't look like a PDF. Please send a .pdf file.",
    "This isn't available to you.",
    "Okay -- try /dose again with a different spelling, or the generic name.",
    "Okay -- type the name again, spelled differently, or try the generic name. /cancel to stop.",
    "Please send an image (as a photo or an image file), or /cancel.",
    "Searching the label...",
    "Searching the book...",
    "Analyzing the ECG, then double-checking the read...",
    "Done.",
]

# language -> {english: translated}. Loaded lazily from disk, then kept in
# memory: every keyboard render hits this, so re-reading JSON each time
# would be pure waste.
_cache: dict[str, dict[str, str]] = {}
# translated label -> english label, across ALL languages, for canonical()
_reverse: dict[str, str] = {}
_lock = threading.Lock()


def _safe_name(lang: str) -> str:
    # Language names come from language.SUPPORTED_LANGUAGES, but these build
    # filesystem paths, so they're sanitized rather than trusted.
    return re.sub(r"[^\w-]", "_", lang)[:40] or "unknown"


def _cache_path(lang: str) -> str:
    return os.path.join(_CACHE_DIR, f"{_safe_name(lang)}.json")


# --- messages -------------------------------------------------------------
#
# Kept in a SEPARATE cache from the UI labels above. Labels are a fixed,
# curated set translated in one batch and reverse-indexed for button
# routing; messages are open-ended -- every error, prompt and status line
# the bot ever sends -- and accumulate as they're encountered. Mixing them
# would put unbounded content into the set that button matching scans.
_MSG_CACHE_DIR = os.path.join(STORAGE_DIR, "_admin", "ui_messages")
_MAX_CACHED_MESSAGES = 4000    # per language; a ceiling on unbounded growth
# A backstop against pathological input, NOT the way AI content is
# excluded -- that's what untranslated() below is for. It started as a
# 700-char cutoff standing in for "this must be AI output", which was
# wrong in both directions: it skipped long static messages (the drug
# interaction checker's intro came out in English) while a short AI answer
# would still have been re-translated.
MAX_TRANSLATABLE_CHARS = 3000

# Content that must NOT be translated on its way out, marked at the point
# it's sent rather than guessed at from its shape. Two kinds qualify:
# AI-generated text, which the call that produced it already wrote in the
# user's language, and verbatim FDA label text, which is medical source
# material that should not be machine-translated at all.
_skip_translation: contextvars.ContextVar[bool] = contextvars.ContextVar("skip_translation", default=False)


@contextlib.contextmanager
def untranslated():
    """
    Mark everything sent inside this block as already-final: the outgoing
    middleware will leave it exactly as written.

        with ui_text.untranslated():
            await send_long_text(message.answer, ai_answer)

    A ContextVar rather than a flag threaded through every send, so it
    survives the awaits between here and the middleware and stays isolated
    to this task -- one user's AI answer can't suppress translation of
    another user's error message being sent at the same moment.
    """
    token = _skip_translation.set(True)
    try:
        yield
    finally:
        _skip_translation.reset(token)


def should_skip() -> bool:
    return _skip_translation.get()

_msg_cache: dict[str, dict[str, str]] = {}


def _msg_cache_path(lang: str) -> str:
    return os.path.join(_MSG_CACHE_DIR, f"{_safe_name(lang)}.json")


def _load_messages(lang: str) -> dict[str, str]:
    if lang in _msg_cache:
        return _msg_cache[lang]
    data = {}
    path = _msg_cache_path(lang)
    if os.path.exists(path):
        try:
            with open(path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = {k: v for k, v in loaded.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
        except (json.JSONDecodeError, OSError):
            logger.exception("Unreadable message cache for %s -- treating as empty", lang)
    with _lock:
        _msg_cache[lang] = data
    return data


def _remember_message(lang: str, english: str, translated: str) -> None:
    data = _load_messages(lang)
    if len(data) >= _MAX_CACHED_MESSAGES:
        return  # keep serving from what's cached; just stop growing the file
    with _lock:
        data[english] = translated
    os.makedirs(_MSG_CACHE_DIR, exist_ok=True)
    path = _msg_cache_path(lang)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        logger.exception("Could not persist message cache for %s (kept in memory)", lang)


def cached_message(language: str | None, text: str) -> str | None:
    """
    An already-cached translation, or None. Never calls Claude, so callers
    on the event loop can take the common path without a thread hop --
    see message_translation.py.
    """
    if is_english(language) or not text:
        return None
    return _load_messages(language).get(text)


def translate_message(language: str | None, text: str) -> str | None:
    """
    Translate one outgoing bot message. Returns None to mean "send the
    original unchanged" -- English, nothing worth translating, or a failure.

    Cache-first: a given message costs one Claude call the first time any
    user in that language sees it, and a dict lookup forever after. Most of
    the bot's messages are fixed strings, so the hit rate climbs quickly.

    AI-generated content and verbatim FDA label text are excluded at their
    send sites with untranslated(), not by any property of the text itself.
    MAX_TRANSLATABLE_CHARS remains only as a backstop against something
    pathologically large reaching here.
    """
    if is_english(language) or not text or not text.strip():
        return None
    if len(text) > MAX_TRANSLATABLE_CHARS:
        return None

    cached = cached_message(language, text)
    if cached:
        return cached

    try:
        translated = _translate_one(language, text)
    except Exception:
        logger.exception("Message translation failed for %s -- sending English", language)
        return None
    if not translated or translated == text:
        return None

    _remember_message(language, text, translated)
    return translated


def _translate_one(language: str, text: str) -> str:
    system_prompt = (
        f"Translate this message from a medical study Telegram bot into {language}.\n\n"
        "Rules:\n"
        "- Reply with ONLY the translation. No quotes, no commentary, no explanation.\n"
        "- Keep every emoji, line break and blank line exactly where they are.\n"
        "- Keep Telegram Markdown markers (*bold*, _italic_, `code`) around the same words, and keep "
        "the number of * and _ characters balanced exactly as in the original.\n"
        "- Do NOT translate: bot commands starting with / (/dose, /cancel), drug names, medical "
        "abbreviations that are normally left in English, numbers, units, file names, or anything "
        "inside backticks. Copy those through unchanged.\n"
        "- Use the wording a clinician or medical student in that language actually uses.\n"
        "- Keep it about as short as the original; this is UI text, not prose."
    )
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=800,
        system=system_prompt,
        messages=[{"role": "user", "content": text}],
    )
    try:
        cost_ledger.record_claude_response("message_translation", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")
    return "".join(b.text for b in response.content if b.type == "text").strip()


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
    _warm_common_messages(language)
    return True


def _warm_common_messages(language: str) -> None:
    """
    Translate the common fixed messages in one batch, so the first errors
    and prompts a user sees after switching are instant instead of each
    costing its own call. Best-effort: on-demand translation still covers
    anything this misses.
    """
    cached = _load_messages(language)
    missing = [m for m in dict.fromkeys(COMMON_MESSAGES) if m not in cached]
    if not missing:
        return
    try:
        translated = _translate(language, missing)
    except Exception:
        logger.exception("Could not pre-warm common messages for %s (they'll translate on demand)", language)
        return
    for english, value in translated.items():
        _remember_message(language, english, value)


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

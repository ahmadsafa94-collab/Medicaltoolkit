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
import ecg_qtc
import language
import ui_strings
from config import STORAGE_DIR, TRANSLATION_MODEL
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
] + ecg_qtc.TRANSLATABLE_FRAGMENTS

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


# Whose update is being handled. Set once per update by
# message_translation.CurrentUserLanguageMiddleware, and read when an
# outgoing request names no chat of its own -- a callback-query toast
# carries only the query id, so there is nothing else to key off.
_current_language: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_language", default=None)


def set_current_language(language: str | None):
    return _current_language.set(language)


def reset_current_language(token) -> None:
    _current_language.reset(token)


def current_language() -> str | None:
    return _current_language.get()

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

    # Deliberately the same JSON batch path a keyboard uses, even for one
    # string. The old single-string path sent the text as a plain chat
    # message, and a short input read as an instruction rather than as
    # content: asked to translate the single word "normal", the model
    # replied "I'm ready to translate... please share the message", and
    # that got cached as the translation and shipped inside an ECG read.
    # A JSON object keyed by the exact source string cannot be answered
    # conversationally -- a reply like that fails to parse and is dropped.
    return translate_batch(language, [text]).get(text)


def translate_batch(language: str | None, texts: list[str]) -> dict[str, str]:
    """
    Translate several strings at once: {original: translated} for whatever
    could be translated, omitting the rest.

    One Claude call for everything not already cached. This exists for
    keyboards -- a menu with a dozen buttons would otherwise be a dozen
    sequential calls the first time anyone in a language opened it, which
    is slow enough to feel broken.
    """
    if is_english(language):
        return {}

    out: dict[str, str] = {}
    missing: list[str] = []
    for text in dict.fromkeys(texts):
        if not text or not text.strip() or len(text) > MAX_TRANSLATABLE_CHARS:
            continue
        cached = cached_message(language, text)
        if cached:
            out[text] = cached
        else:
            missing.append(text)

    if missing:
        try:
            fresh = _translate(language, missing)
        except Exception:
            logger.exception("Batch translation failed for %s -- those strings stay English", language)
            fresh = {}
        for english, value in fresh.items():
            _remember_message(language, english, value)
            out[english] = value
    return out


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


WARM_BATCH_SIZE = 40  # strings per translation call when pre-warming


def warm_all_messages(language: str) -> int:
    """
    Translate every fixed message and button caption in the bot that this
    language doesn't have yet, and cache them. Returns how many were added.

    This is what makes static text feel instant rather than "slow the
    first time each one appears". Translating only on demand meant a user
    exploring the bot kept being the first to reach some string and kept
    waiting for it; doing the whole set up front means the cache is
    already complete by the time they get there.

    Blocking and batched -- call it from a thread, off the event loop.
    Batches are committed as they complete, so an interruption or a
    failure part-way keeps everything already translated instead of
    discarding the lot.
    """
    if is_english(language):
        return 0

    cached = _load_messages(language)
    wanted = list(dict.fromkeys(list(UI_STRINGS) + list(COMMON_MESSAGES) + ui_strings.extract_static_messages()))
    missing = [s for s in wanted if s not in cached]
    if not missing:
        return 0

    added = 0
    for i in range(0, len(missing), WARM_BATCH_SIZE):
        batch = missing[i : i + WARM_BATCH_SIZE]
        try:
            translated = _translate(language, batch)
        except Exception:
            logger.exception("Pre-warm batch failed for %s -- those strings translate on demand instead", language)
            continue
        for english, value in translated.items():
            _remember_message(language, english, value)
            added += 1
    logger.info("Pre-translated %d/%d fixed strings into %s", added, len(missing), language)
    return added


def _prune_implausible(lang: str) -> None:
    """
    Drop cached entries that clearly aren't translations, so they get
    fetched again properly.

    A bad value used to be able to enter the cache and then stay there
    forever: asked to translate the single word "normal", the model once
    replied "I'm ready to translate... please share the message", and that
    was cached and shipped inside an ECG read. The path that allowed it is
    fixed, but a cache written before the fix still holds the damage, and
    nothing else would ever evict it.
    """
    cached = _load_messages(lang)
    bad = [k for k, v in cached.items() if len(v) > max(80, len(k) * 6)]
    if not bad:
        return
    logger.warning("Dropping %d implausible cached translation(s) for %s: %s", len(bad), lang, bad[:5])
    with _lock:
        for key in bad:
            cached.pop(key, None)
    os.makedirs(_MSG_CACHE_DIR, exist_ok=True)
    path = _msg_cache_path(lang)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cached, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        logger.exception("Could not rewrite the pruned message cache for %s", lang)


def warm_known_languages() -> None:
    """
    Top up every offered language, and any already cached on disk. Run at
    startup, off the main thread: a deploy that adds new messages would
    otherwise leave them untranslated until a user happened to trigger each
    one. Languages with nothing missing cost nothing.

    A cache file for a language no longer offered is skipped rather than
    maintained -- otherwise dropping a language from the picker would
    leave the bot paying to keep translating into it forever. The file is
    left on disk, so re-adding the language picks up where it left off.
    """
    for lang in _known_languages():
        try:
            _prune_implausible(lang)
        except Exception:
            logger.exception("Could not prune the message cache for %s (non-fatal)", lang)

    offered = {lang.lower() for lang in language.SUPPORTED_LANGUAGES}
    candidates = list(language.SUPPORTED_LANGUAGES) + [
        lang for lang in _known_languages() if lang.lower() in offered
    ]
    for lang in dict.fromkeys(candidates):
        try:
            warm_all_messages(lang)
        except Exception:
            logger.exception("Startup pre-warm failed for %s (non-fatal)", lang)


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
        model=TRANSLATION_MODEL,
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
    kept = {}
    for k, v in parsed.items():
        if not (isinstance(k, str) and isinstance(v, str) and k in wanted and v.strip()):
            continue
        value = v.strip()
        # A translation runs roughly as long as its source. Something many
        # times longer is not a translation -- it's the model having
        # answered the request instead of performing it, which is how
        # "I'm ready to translate, please share the message" once ended up
        # cached as the word "normal". Cheap to check, and the cost of a
        # false reject is one string staying English.
        if len(value) > max(80, len(k) * 6):
            logger.warning("Discarding implausible translation for %r into %s: %r", k[:40], language, value[:60])
            continue
        kept[k] = value
    return kept

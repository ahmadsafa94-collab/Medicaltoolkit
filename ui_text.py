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

import collections
import contextlib
import contextvars
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata

import cost_ledger
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
    "💊 Drugs Info",
    "🧮 Calculators",
    "🧠 Study Tools",
    "⭐ My Plan",
    "🌐 Language",
    "🛠 Admin Panel",
    "🐞 Report a problem / Give Feedback",
    "🆘 Support",
    # Drugs Info submenu
    "🔀 Drug Interactions",
    "💊 Drug LookUp",
    # Study Tools submenu
    "🗂 Flashcards",
    "✂️ PDF Splitter",
    "📓 My Notes",
    "💬 Ask My Books",
    "🔖 Bookmarks",
    # Submenu headers
    "💊 Drugs Info",
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
    "Done.",
]

# language -> {english: translated}. Loaded lazily from disk, then kept in
# memory: every keyboard render hits this, so re-reading JSON each time
# would be pure waste.
_cache: dict[str, dict[str, str]] = {}
# translated label -> english label, across ALL languages, for canonical()
_reverse: dict[str, str] = {}
# Guards every in-memory mutation of the caches above and below. Held only
# for dict work, never across disk I/O or a Claude call: cached_message()
# takes it on the event loop, so holding it while writing a file would
# stall every user's updates.
_lock = threading.Lock()
# Serializes cache-file WRITES. Separate from _lock so the event loop never
# waits on the disk, and held across snapshot-then-write so two writers
# can't land out of order -- without it, a thread holding an older snapshot
# could os.replace() it over a newer file and silently drop entries. Lock
# order is always _write_lock, then _lock.
_write_lock = threading.Lock()


def _atomic_write_json(path: str, payload: str) -> None:
    """
    Replace `path` with `payload` so a reader sees the old file or the new
    one, never half of either.

    The temp file is unique per call (NamedTemporaryFile, same directory so
    os.replace stays a same-filesystem rename). A fixed "<path>.tmp" was
    shared by every worker thread translating at once: two of them would
    write into the same file and one would rename the other's half-written
    JSON into place.
    """
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp", delete=False
    )
    try:
        with tmp as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp.name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)
        raise


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
_MAX_CACHED_MESSAGES = 4000    # per language; least-recently-used entries are evicted past this
# What is worth a cache slot. A message the bot built at runtime -- a
# calculator result, "Found 12 chapters", a page count -- almost never
# recurs byte-for-byte, so caching it only spends a slot. Before this rule
# those filled the cap within weeks and the cache then refused everything
# new, so every later message cost a Claude call forever. Fixed strings
# found in the source (see _static_strings) are always cacheable whatever
# they contain; anything else is cacheable only with no digits in it and
# within this length.
_MAX_UNLISTED_DIGITS = 0
_MAX_CACHEABLE_CHARS = 1000
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
    cached = _msg_cache.get(lang)
    if cached is not None:
        return cached
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
        # setdefault, not assignment: two threads can both miss and both
        # read the file. Whichever lands second must adopt the first one's
        # dict, or entries the first thread already remembered would go
        # into a dict nothing references any more.
        return _msg_cache.setdefault(lang, data)


_static: frozenset[str] | None = None


def _static_strings() -> frozenset[str]:
    """Every fixed string the bot can send, for _is_cacheable. Built once; the source can't change under us."""
    global _static
    if _static is None:
        # A benign race: two threads may both build it, and both get the same set.
        _static = frozenset(UI_STRINGS) | frozenset(COMMON_MESSAGES) | frozenset(ui_strings.extract_static_messages())
    return _static


def _is_cacheable(text: str) -> bool:
    """Whether `text` deserves a cache slot -- see _MAX_UNLISTED_DIGITS above for why most dynamic text doesn't."""
    if text in _static_strings():
        return True
    if sum(ch.isdigit() for ch in text) > _MAX_UNLISTED_DIGITS:
        return False
    return len(text) <= _MAX_CACHEABLE_CHARS


def _worth_translating(text: str) -> bool:
    """
    False for text that is mostly numbers -- "✅ 12/40", "120/80", a bare
    result. There are no words in it to translate, and every one of them
    would otherwise be a Claude call (uncacheable, see _is_cacheable) whose
    best possible outcome is the same string back.
    """
    letters = sum(ch.isalpha() for ch in text)
    return letters >= 2 and letters > sum(ch.isdigit() for ch in text)


def _persist_messages(lang: str) -> None:
    """Write the language's message cache to disk. Safe to call from any number of threads at once."""
    path = _msg_cache_path(lang)
    with _write_lock:
        with _lock:
            data = _msg_cache.get(lang)
            if data is None:
                return
            # Copy under the lock, serialize outside it: json.dump straight
            # from the live dict raced worker threads adding to it ("dict
            # changed size during iteration"), and serializing a 4000-entry
            # dict while holding _lock would block the event loop on it.
            snapshot = dict(data)
        try:
            _atomic_write_json(path, json.dumps(snapshot, ensure_ascii=False))
        except OSError:
            logger.exception("Could not persist message cache for %s (kept in memory)", lang)


def _remember_messages(lang: str, translations: dict[str, str]) -> None:
    """Cache a batch of fresh translations and write the file once for the batch, not once per string."""
    keep = {english: value for english, value in translations.items() if _is_cacheable(english)}
    if not keep:
        return
    data = _load_messages(lang)
    with _lock:
        for english, value in keep.items():
            # Re-insert at the end: dict order is the recency order the
            # eviction below relies on.
            data.pop(english, None)
            data[english] = value
        # At the cap, evict the least recently used rather than refusing
        # new entries -- refusing meant a full cache froze in whatever it
        # happened to hold, and every newer message cost a call forever.
        while len(data) > _MAX_CACHED_MESSAGES:
            del data[next(iter(data))]
    _persist_messages(lang)


def _remember_message(lang: str, english: str, translated: str) -> None:
    _remember_messages(lang, {english: translated})


def cached_message(language: str | None, text: str) -> str | None:
    """
    An already-cached translation, or None. Never calls Claude, so callers
    on the event loop can take the common path without a thread hop --
    see message_translation.py.
    """
    if is_english(language) or not text:
        return None
    data = _load_messages(language)
    value = data.get(text)
    if value is not None:
        with _lock:
            # Mark it recently used, so eviction takes the strings nobody
            # sees rather than the ones everybody does. Only reordered in
            # memory; the file picks the order up on its next write, which
            # is close enough for a cache.
            if text in data:
                data[text] = data.pop(text)
    return value


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
    if len(text) > MAX_TRANSLATABLE_CHARS or not _worth_translating(text):
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
        if not text or not text.strip() or len(text) > MAX_TRANSLATABLE_CHARS or not _worth_translating(text):
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
        # Dynamic strings are still translated for this send, just not
        # cached -- _remember_messages decides which are worth keeping.
        _remember_messages(language, fresh)
        out.update(fresh)
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
        # As in _load_messages: a concurrent loader (or _save) may have got
        # here first, and its dict is the one to keep.
        data = _cache.setdefault(lang, data)
        for english, translated in data.items():
            _reverse.setdefault(translated, english)
    return data


def _save(lang: str, data: dict[str, str]) -> None:
    # Two users picking the same language at once both reach here from
    # their own threads -- hence the unique temp file and the write lock.
    with _write_lock:
        _atomic_write_json(_cache_path(lang), json.dumps(data, ensure_ascii=False))
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


def _offered(name) -> bool:
    """Only the languages the bot offers are ever translated or cached."""
    return isinstance(name, str) and name in language.SUPPORTED_LANGUAGES


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
    if not _offered(language):
        return False
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


# Strings per commit when pre-warming. _translate splits each batch further
# by size (_CHUNK_MAX_STRINGS / _CHUNK_MAX_CHARS), so this only decides how
# much is kept if the warm is interrupted, not how big one Claude call is.
WARM_BATCH_SIZE = 40


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
    if not _offered(language):
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
        _remember_messages(language, translated)
        added += len(translated)
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

    The same goes for two kinds of entry written before their rules
    existed: translations whose numbers don't match the English (see
    _numbers_match), and dynamic one-off strings (see _is_cacheable) that
    filled older caches to the cap.
    """
    cached = _load_messages(lang)
    _static_strings()  # build it (parses the source) BEFORE taking _lock, which the event loop also needs
    with _lock:
        bad = [
            k for k, v in cached.items()
            if len(v) > max(80, len(k) * 6) or not _numbers_match(k, v) or not _is_cacheable(k)
        ]
        for key in bad:
            cached.pop(key, None)
    if not bad:
        return
    logger.warning("Dropping %d implausible or uncacheable cached translation(s) for %s: %s", len(bad), lang, bad[:5])
    _persist_messages(lang)


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
    _remember_messages(language, translated)


# A number in the text: 5, 12.50, 1,000, 1.73. Separators are part of the
# token, so "1.5" coming back as "1,5" (or "15") counts as a change.
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def _numbers(text: str) -> collections.Counter:
    # \d also matches other scripts' decimal digits (Persian ۱۲, Devanagari
    # १२). Those are mapped to ASCII before comparing: "۱۲" is the same
    # number as "12", written the way that language writes it. Only the
    # digit glyphs are normalized -- separators are compared as written.
    return collections.Counter(
        "".join(str(unicodedata.decimal(ch)) if ch.isdigit() else ch for ch in token)
        for token in _NUMBER_RE.findall(text)
    )


def _numbers_match(english: str, translated: str) -> bool:
    """
    Whether the translation carries exactly the English text's numbers.

    These strings include calculator results, doses and lab thresholds, and
    the translation is an LLM rewrite: a dropped digit, a rounded value or a
    swapped decimal separator would be a wrong dose shown with full
    confidence. Any difference in the multiset of numbers rejects the
    translation and that string is shown in English instead -- a mismatch
    costs readability, a wrong number could cost far more.
    """
    return _numbers(english) == _numbers(translated)


# Output budget per call. The reply repeats every English key alongside its
# translation, and non-Latin scripts take several tokens per word, so the
# old 2000 cut long messages and 40-string batches off mid-JSON -- which
# failed to parse, and those strings silently stayed English.
_MAX_TOKENS = 8000
# Per-call input limits, chosen so a reply comfortably fits _MAX_TOKENS
# even in a token-hungry script. A single string longer than this (up to
# MAX_TRANSLATABLE_CHARS) still goes, alone.
_CHUNK_MAX_STRINGS = 25
_CHUNK_MAX_CHARS = 4000


def _chunks(strings: list[str]) -> list[list[str]]:
    out: list[list[str]] = []
    current: list[str] = []
    size = 0
    for s in strings:
        if current and (len(current) >= _CHUNK_MAX_STRINGS or size + len(s) > _CHUNK_MAX_CHARS):
            out.append(current)
            current, size = [], 0
        current.append(s)
        size += len(s)
    if current:
        out.append(current)
    return out


def _translate(language: str, strings: list[str]) -> dict[str, str]:
    """
    Translate `strings` in as few Claude calls as fit the output budget;
    returns {english: translated} for what came back usable.

    One failing chunk doesn't discard the others' results. If nothing at
    all came back and a call raised, that error is re-raised, so callers
    still see -- and log -- a real outage the way they always have.
    """
    chunks = _chunks(strings)
    if len(chunks) == 1:
        return _translate_chunk(language, chunks[0])  # callers log its failure; no need to here too
    out: dict[str, str] = {}
    error: Exception | None = None
    for chunk in chunks:
        try:
            out.update(_translate_chunk(language, chunk))
        except Exception as e:
            logger.exception("Translation call for %d string(s) into %s failed", len(chunk), language)
            error = e
    if not out and error is not None:
        raise error
    return out


def _translate_chunk(language: str, strings: list[str]) -> dict[str, str]:
    """One Claude call for the chunk; returns {english: translated} for what came back usable."""
    system_prompt = (
        f"Translate each UI label of a medical study app from English into {language}.\n\n"
        "Rules:\n"
        "- Reply with ONLY a JSON object mapping each original English string to its translation. No "
        "markdown fences, no commentary.\n"
        "- Keep every emoji exactly as it is and in the same position as the English.\n"
        "- These are BUTTON labels: keep them short, ideally no longer than the English.\n"
        "- Use the wording a clinician or medical student actually uses in that language for medical "
        "terms (ECG, lab, dose); do not invent literal word-for-word translations of technical terms.\n"
        "- If a term is normally left in English in that language, leave it in English.\n"
        "- Copy every number exactly as written: the same digits, decimal separator and thousands "
        "separator, nothing added, dropped, rounded or converted. These include doses and lab values."
    )
    response = client.messages.create(
        model=TRANSLATION_MODEL,
        max_tokens=_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": json.dumps(strings, ensure_ascii=False)}],
    )
    try:
        cost_ledger.record_claude_response("ui_translation", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    if getattr(response, "stop_reason", None) == "max_tokens":
        # The reply was cut off, so its JSON is unterminated. Never try to
        # parse or salvage half an object -- the last value in it may be a
        # truncated translation. Split and retry the halves instead: each
        # needs about half the output, and a single string that still
        # doesn't fit just stays English.
        if len(strings) > 1:
            logger.warning(
                "UI translation into %s hit max_tokens for %d strings -- retrying in halves", language, len(strings)
            )
            mid = len(strings) // 2
            return {**_translate_chunk(language, strings[:mid]), **_translate_chunk(language, strings[mid:])}
        logger.warning("UI translation into %s hit max_tokens for one %d-char string -- left in English",
                       language, len(strings[0]))
        return {}

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
        if not _numbers_match(k, value):
            logger.warning("Discarding translation for %r into %s: its numbers differ from the English (%r)",
                           k[:40], language, value[:60])
            continue
        kept[k] = value
    return kept

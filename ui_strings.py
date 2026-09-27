"""
Finds every fixed message and button caption the bot can send.

ui_text caches a translation the first time a string is actually sent,
which means a user exploring the bot keeps meeting strings nobody has hit
yet and waiting for each one. The fix is to translate them all when a
language is chosen -- but that needs the list, and the strings live as
literals across two dozen modules, so a hand-maintained list would be out
of date by the next commit.

So the list is read from the source itself. Each module is parsed and the
string constants in the positions that reach a user are collected:

    message.answer("Cancelled.")            <- first argument of a send
    status.edit_text("Processing...")
    InlineKeyboardButton(text="📊 Stats")    <- a button caption
    KeyboardButton(text="🆘 Support")

Only genuine constants qualify. An f-string is skipped on purpose: it has
a value interpolated into it, so every send produces a different string
that could never be cached anyway -- those still translate on demand.

Deliberately reads the source rather than importing anything: this runs
inside the bot process, and importing modules to inspect them would run
their side effects a second time.
"""

import ast
import logging
import os

logger = logging.getLogger(__name__)

_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# Methods whose FIRST positional argument is text shown to the user.
_SEND_METHODS = {"answer", "edit_text", "reply", "edit_caption", "answer_callback_query", "send_message"}
# Constructors whose `text=` is a caption the user reads.
_BUTTON_TYPES = {"InlineKeyboardButton", "KeyboardButton"}

# Modules with no user-facing strings, or whose strings are prompts rather
# than messages. Skipping them keeps multi-thousand-character system
# prompts out of the translation set -- they are instructions to a model,
# never shown to anyone, and translating them would be both expensive and
# actively harmful.
_SKIP_FILES = {"ui_strings.py", "ui_text.py", "bidi_text.py", "message_translation.py"}

_cache: list[str] | None = None


def _is_translatable(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) < 2 or len(text) > 700:
        return False
    return any(ch.isalpha() for ch in text)


def _from_tree(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _SEND_METHODS:
            if node.args and isinstance(node.args[0], ast.Constant) and _is_translatable(node.args[0].value):
                found.append(node.args[0].value.strip())

        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in _BUTTON_TYPES:
            for kw in node.keywords:
                if kw.arg == "text" and isinstance(kw.value, ast.Constant) and _is_translatable(kw.value.value):
                    found.append(kw.value.value.strip())
    return found


def extract_static_messages() -> list[str]:
    """
    Every fixed user-facing string in the codebase, de-duplicated.

    Parsed once per process and memoized -- the source cannot change under
    a running bot, so there is nothing to invalidate.
    """
    global _cache
    if _cache is not None:
        return _cache

    seen: dict[str, None] = {}
    try:
        filenames = sorted(f for f in os.listdir(_PROJECT_DIR) if f.endswith(".py") and f not in _SKIP_FILES)
    except OSError:
        logger.exception("Could not list the project directory -- no strings pre-warmed")
        _cache = []
        return _cache

    for filename in filenames:
        path = os.path.join(_PROJECT_DIR, filename)
        try:
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=filename)
        except (OSError, SyntaxError):
            logger.exception("Could not parse %s while collecting UI strings -- skipping it", filename)
            continue
        for text in _from_tree(tree):
            seen.setdefault(text, None)

    _cache = list(seen)
    logger.info("Collected %d fixed user-facing strings for pre-translation", len(_cache))
    return _cache

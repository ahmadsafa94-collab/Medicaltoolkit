"""
Tiny durable ring-buffer of the most recent server-side exceptions, feeding
the admin panel's "🪵 Recent errors" view. bot.py's existing global_error_handler
already catches and logs (to stdout/Railway logs) every exception that
escapes a handler -- this just ALSO appends a short record here so an admin
can see the same thing from inside the bot chat, without needing Railway
log access.
"""

import json
import logging
import os
import time
import traceback

from config import STORAGE_DIR, MAX_RECENT_ERRORS, MAX_RECENT_REPORTS

logger = logging.getLogger(__name__)

_ADMIN_DIR = os.path.join(STORAGE_DIR, "_admin")
_LOG_PATH = os.path.join(_ADMIN_DIR, "recent_errors.json")
_REPORTS_PATH = os.path.join(_ADMIN_DIR, "reported_problems.json")


def record_error(exception: BaseException, context: str = "") -> None:
    try:
        os.makedirs(_ADMIN_DIR, exist_ok=True)
        entries = _load(_LOG_PATH)
        entries.append(
            {
                "ts": time.time(),
                "context": context,
                "error": f"{type(exception).__name__}: {exception}",
                "traceback": "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))[-3000:],
            }
        )
        entries = entries[-MAX_RECENT_ERRORS:]
        _save(_LOG_PATH, entries)
    except Exception:
        logger.exception("Failed to record error to admin_log (non-fatal)")


def _load(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(path: str, entries: list[dict]) -> None:
    os.makedirs(_ADMIN_DIR, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(entries, f)
    os.replace(tmp_path, path)


def recent_errors(limit: int = 10) -> list[dict]:
    return _load(_LOG_PATH)[-limit:][::-1]  # most recent first


def record_report(user_id: int, who: str, text: str) -> None:
    """
    Durable record of a 🐞 Report a problem submission (customer_flow.py's
    handle_feedback_send), separate from the live forward to ADMIN_USER_IDS'
    chats -- that forward scrolls away in a busy admin DM, this is what backs
    the admin panel's "🐞 Reported problems" view so nothing gets lost.
    """
    try:
        entries = _load(_REPORTS_PATH)
        entries.append({"ts": time.time(), "user_id": user_id, "who": who, "text": text})
        entries = entries[-MAX_RECENT_REPORTS:]
        _save(_REPORTS_PATH, entries)
    except Exception:
        logger.exception("Failed to record reported problem to admin_log (non-fatal)")


def recent_reports(limit: int = 10) -> list[dict]:
    return _load(_REPORTS_PATH)[-limit:][::-1]  # most recent first

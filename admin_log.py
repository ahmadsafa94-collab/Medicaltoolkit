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

from config import STORAGE_DIR, MAX_RECENT_ERRORS

logger = logging.getLogger(__name__)

_ADMIN_DIR = os.path.join(STORAGE_DIR, "_admin")
_LOG_PATH = os.path.join(_ADMIN_DIR, "recent_errors.json")


def record_error(exception: BaseException, context: str = "") -> None:
    try:
        os.makedirs(_ADMIN_DIR, exist_ok=True)
        entries = _load()
        entries.append(
            {
                "ts": time.time(),
                "context": context,
                "error": f"{type(exception).__name__}: {exception}",
                "traceback": "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))[-3000:],
            }
        )
        entries = entries[-MAX_RECENT_ERRORS:]
        tmp_path = _LOG_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(entries, f)
        os.replace(tmp_path, _LOG_PATH)
    except Exception:
        logger.exception("Failed to record error to admin_log (non-fatal)")


def _load() -> list[dict]:
    if not os.path.exists(_LOG_PATH):
        return []
    try:
        with open(_LOG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def recent_errors(limit: int = 10) -> list[dict]:
    return _load()[-limit:][::-1]  # most recent first

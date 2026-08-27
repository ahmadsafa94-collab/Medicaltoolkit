"""
Per-user "recent lookups" and "bookmarks" for drug lookups.

Persisted to a single JSON file (not just in-memory) so recent/bookmarked
drugs survive a bot restart -- session_cache.py is deliberately TTL-limited
and exists for a different purpose (letting inline buttons reference a
lookup for ~30 minutes), not for long-term per-user history.

Concurrency: a single asyncio.Lock serializes all reads/writes within this
process (fine for a single-instance bot -- see session_cache.py's docstring
for the same caveat about multi-worker deployments), and every write goes
through a write-to-temp-then-os.replace so a crash mid-write can't leave a
half-written, corrupt JSON file behind.
"""

import asyncio
import json
import logging
import os

from config import STORAGE_DIR

logger = logging.getLogger(__name__)

_FILE = os.path.join(STORAGE_DIR, "user_history.json")
_LOCK = asyncio.Lock()

MAX_RECENT = 15
MAX_BOOKMARKS = 100


def _load() -> dict:
    if not os.path.exists(_FILE):
        return {}
    try:
        with open(_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("user_history.json unreadable or corrupt -- starting fresh instead of crashing")
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(_FILE) or ".", exist_ok=True)
    tmp_path = _FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_path, _FILE)  # atomic on POSIX -- readers never see a half-written file


def _entry(data: dict, user_id: int) -> dict:
    return data.setdefault(str(user_id), {"recent": [], "bookmarks": []})


async def record_recent(user_id: int, drug_name: str) -> None:
    async with _LOCK:
        data = _load()
        entry = _entry(data, user_id)
        recent = entry["recent"]
        recent[:] = [d for d in recent if d.lower() != drug_name.lower()]
        recent.insert(0, drug_name)
        del recent[MAX_RECENT:]
        _save(data)


async def get_recent(user_id: int) -> list:
    async with _LOCK:
        data = _load()
        return list(_entry(data, user_id)["recent"])


async def add_bookmark(user_id: int, drug_name: str) -> str:
    """Returns 'added', 'duplicate', or 'full'."""
    async with _LOCK:
        data = _load()
        entry = _entry(data, user_id)
        bookmarks = entry["bookmarks"]
        if any(b.lower() == drug_name.lower() for b in bookmarks):
            return "duplicate"
        if len(bookmarks) >= MAX_BOOKMARKS:
            return "full"
        bookmarks.append(drug_name)
        _save(data)
        return "added"


async def remove_bookmark(user_id: int, drug_name: str) -> bool:
    async with _LOCK:
        data = _load()
        entry = _entry(data, user_id)
        before = len(entry["bookmarks"])
        entry["bookmarks"] = [b for b in entry["bookmarks"] if b.lower() != drug_name.lower()]
        changed = len(entry["bookmarks"]) != before
        if changed:
            _save(data)
        return changed


async def get_bookmarks(user_id: int) -> list:
    async with _LOCK:
        data = _load()
        return list(_entry(data, user_id)["bookmarks"])

"""
Tiny in-memory cache used to let inline buttons reference a previous drug
lookup without re-querying openFDA on every tap.

Not persistent (lost on restart) and not shared across processes -- fine for
a single-instance bot. If you ever run multiple bot workers behind a load
balancer, swap this for Redis with the same get/put interface.
"""

import time
import uuid
from collections import OrderedDict

_MAX_ENTRIES = 500
_TTL_SECONDS = 60 * 30  # half an hour is plenty for a user to tap through sections

_store: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()


def put(sections: dict) -> str:
    """Store a lookup_drug() result, return a short id to reference it by."""
    _evict_expired()
    while len(_store) >= _MAX_ENTRIES:
        _store.popitem(last=False)  # drop oldest

    cache_id = uuid.uuid4().hex[:10]
    _store[cache_id] = (time.time(), sections)
    return cache_id


def get(cache_id: str) -> dict | None:
    """Retrieve a cached result by id, or None if missing/expired."""
    _evict_expired()
    entry = _store.get(cache_id)
    if entry is None:
        return None
    return entry[1]


def _evict_expired():
    now = time.time()
    expired = [k for k, (ts, _) in _store.items() if now - ts > _TTL_SECONDS]
    for k in expired:
        del _store[k]

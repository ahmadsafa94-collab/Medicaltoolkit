"""
📚 Request a Book: a customer sends a URL from the Book Shelf mini app, an
admin quotes a price in USD, the customer pays via a Telegram Stars invoice
(converted from that USD quote -- config.USD_TO_STARS_RATE), and once paid
the admin delivers the PDF, which gets added to the customer's own Book
Shelf and sent to their chat so they can download it directly.

Storage: one shared JSON file (STORAGE_DIR/_admin/book_requests.json,
same _admin/ home as admin_log.py's error/report logs and
subscriptions.py's users index) since -- unlike a user's own subscription
or library -- an admin needs to look these up by request_id regardless of
which customer they belong to, and list every pending one.

State machine (request["status"]):
    pending -> quoted -> paid -> delivered
                  \-> declined
"""

import json
import logging
import os
import time
import uuid

from config import STORAGE_DIR, USD_TO_STARS_RATE

logger = logging.getLogger(__name__)

_ADMIN_DIR = os.path.join(STORAGE_DIR, "_admin")
_REQUESTS_PATH = os.path.join(_ADMIN_DIR, "book_requests.json")


def _load() -> dict:
    if not os.path.exists(_REQUESTS_PATH):
        return {}
    try:
        with open(_REQUESTS_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(requests: dict) -> None:
    os.makedirs(_ADMIN_DIR, exist_ok=True)
    tmp_path = _REQUESTS_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(requests, f)
    os.replace(tmp_path, _REQUESTS_PATH)


def usd_to_stars(price_usd: float) -> int:
    return max(1, round(price_usd * USD_TO_STARS_RATE))


def create_request(user_id: int, who: str, url: str, note: str = "") -> dict:
    requests = _load()
    request_id = uuid.uuid4().hex[:8]
    record = {
        "request_id": request_id,
        "user_id": user_id,
        "who": who,
        "url": url,
        "note": note,
        "status": "pending",
        "price_usd": None,
        "stars": None,
        "created_at": time.time(),
        "quoted_at": None,
        "paid_at": None,
        "delivered_at": None,
    }
    requests[request_id] = record
    _save(requests)
    return record


def get_request(request_id: str) -> dict | None:
    return _load().get(request_id)


def list_pending() -> list[dict]:
    """Requests still awaiting a price quote -- the admin panel's 📚 Book Requests view."""
    return sorted((r for r in _load().values() if r["status"] == "pending"), key=lambda r: r["created_at"])


def _update(request_id: str, **fields) -> dict | None:
    requests = _load()
    record = requests.get(request_id)
    if record is None:
        return None
    record.update(fields)
    _save(requests)
    return record


def set_quote(request_id: str, price_usd: float) -> dict | None:
    return _update(request_id, status="quoted", price_usd=price_usd, stars=usd_to_stars(price_usd), quoted_at=time.time())


def set_declined(request_id: str) -> dict | None:
    return _update(request_id, status="declined")


def set_paid(request_id: str) -> dict | None:
    return _update(request_id, status="paid", paid_at=time.time())


def set_delivered(request_id: str) -> dict | None:
    return _update(request_id, status="delivered", delivered_at=time.time())

"""
Spaced-repetition flashcard storage + scheduling (SM-2, the same algorithm
Anki itself is built on -- see anki_export.py for why that pairing matters
for the .apkg export feature).

Storage: STORAGE_DIR/{user_id}/flashcards/{book_id}.json -- a flat list of
card records per book, generated from one chapter's text at a time (see
chapter_ai.generate_flashcards) but reviewed together across a book.
"""

import json
import logging
import os
import time
import uuid

from config import STORAGE_DIR

logger = logging.getLogger(__name__)

MAX_CARDS_PER_BOOK = 500  # a generous ceiling, not a normal-use limit

# SM-2 defaults for a brand-new card.
_DEFAULT_EASE = 2.5
_MIN_EASE = 1.3

# Quality buttons shown to the reviewer, mapped to SM-2's 0-5 quality scale.
QUALITY_AGAIN = 0
QUALITY_HARD = 3
QUALITY_GOOD = 4
QUALITY_EASY = 5


def _cards_path(user_id: int, book_id: str) -> str:
    return os.path.join(STORAGE_DIR, str(user_id), "flashcards", f"{book_id}.json")


def _reminder_state_path(user_id: int) -> str:
    return os.path.join(STORAGE_DIR, str(user_id), "flashcards", "_reminder_state.json")


def _load(user_id: int, book_id: str) -> list[dict]:
    path = _cards_path(user_id, book_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_id: int, book_id: str, cards: list[dict]) -> None:
    path = _cards_path(user_id, book_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(cards, f)
    os.replace(tmp_path, path)


def add_cards(user_id: int, book_id: str, book_title: str, chapter_title: str, raw_cards: list[dict]) -> list[dict]:
    """raw_cards: [{"front": str, "back": str}, ...] from chapter_ai.generate_flashcards(). Returns the newly-added card records."""
    cards = _load(user_id, book_id)
    now = time.time()
    new_cards = []
    for rc in raw_cards:
        if len(cards) + len(new_cards) >= MAX_CARDS_PER_BOOK:
            break
        card = {
            "id": uuid.uuid4().hex[:10],
            "book_id": book_id,
            "book_title": book_title,
            "chapter_title": chapter_title,
            "front": rc["front"],
            "back": rc["back"],
            "due_ts": now,  # new cards are immediately due
            "interval_days": 0,
            "ease_factor": _DEFAULT_EASE,
            "reps": 0,
            "created_at": now,
        }
        new_cards.append(card)
    cards.extend(new_cards)
    _save(user_id, book_id, cards)
    return new_cards


def get_deck(user_id: int, book_id: str) -> list[dict]:
    return _load(user_id, book_id)


def due_cards(user_id: int, book_id: str, limit: int = 20) -> list[dict]:
    now = time.time()
    cards = [c for c in _load(user_id, book_id) if c["due_ts"] <= now]
    cards.sort(key=lambda c: c["due_ts"])
    return cards[:limit]


def count_due(user_id: int, book_id: str) -> int:
    now = time.time()
    return sum(1 for c in _load(user_id, book_id) if c["due_ts"] <= now)


def count_due_all_books(user_id: int, book_ids: list[str]) -> int:
    return sum(count_due(user_id, book_id) for book_id in book_ids)


def all_cards(user_id: int, book_id: str, limit: int = 200) -> list[dict]:
    """Every card in the deck, most-recently-created last -- for the "📚 All cards" review mode."""
    return _load(user_id, book_id)[:limit]


def hard_cards(user_id: int, book_id: str, limit: int = 100) -> list[dict]:
    """
    Cards with a below-default ease factor -- SM-2's own signal that a card
    has been rated Again/Hard at least once net of any later Good/Easy
    ratings (see review_card()'s ease-factor formula), used for the
    "❗ Hard cards" review mode. Sorted hardest (lowest ease) first.
    """
    cards = [c for c in _load(user_id, book_id) if c.get("ease_factor", _DEFAULT_EASE) < _DEFAULT_EASE]
    cards.sort(key=lambda c: c.get("ease_factor", _DEFAULT_EASE))
    return cards[:limit]


def count_all(user_id: int, book_id: str) -> int:
    return len(_load(user_id, book_id))


def count_hard(user_id: int, book_id: str) -> int:
    return sum(1 for c in _load(user_id, book_id) if c.get("ease_factor", _DEFAULT_EASE) < _DEFAULT_EASE)


def review_card(user_id: int, book_id: str, card_id: str, quality: int) -> dict | None:
    """
    Apply one SM-2 review step. quality is 0-5 (see the QUALITY_* constants
    above). Returns the updated card, or None if card_id wasn't found.
    """
    cards = _load(user_id, book_id)
    card = next((c for c in cards if c["id"] == card_id), None)
    if card is None:
        return None

    ease = card.get("ease_factor", _DEFAULT_EASE)
    reps = card.get("reps", 0)
    interval = card.get("interval_days", 0)

    if quality < 3:
        reps = 0
        interval = 1
    else:
        if reps == 0:
            interval = 1
        elif reps == 1:
            interval = 6
        else:
            interval = round(interval * ease)
        reps += 1

    ease = max(_MIN_EASE, ease + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02)))

    card["ease_factor"] = ease
    card["reps"] = reps
    card["interval_days"] = interval
    card["due_ts"] = time.time() + interval * 86400
    card["last_reviewed_at"] = time.time()

    _save(user_id, book_id, cards)
    return card


def delete_deck(user_id: int, book_id: str) -> None:
    try:
        os.remove(_cards_path(user_id, book_id))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Daily due-cards reminder bookkeeping (see bot.py's background reminder task)
# ---------------------------------------------------------------------------

def _today_key() -> str:
    t = time.gmtime()
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def should_send_reminder_today(user_id: int) -> bool:
    path = _reminder_state_path(user_id)
    if not os.path.exists(path):
        return True
    try:
        with open(path) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return True
    return state.get("last_reminder_date") != _today_key()


def mark_reminder_sent(user_id: int) -> None:
    path = _reminder_state_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump({"last_reminder_date": _today_key()}, f)
    os.replace(tmp_path, path)

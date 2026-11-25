"""
Specialty libraries -- books the admin provides for EVERY customer.

A user picks a specialty (e.g. Psychiatry) in the mini app's 💬 Ask tab,
ticks the books they want answers from, optionally ticks books from their
own shelf as well, and asks one question across all of them (see
pdf_qa.answer_question_multi). Only admins can add or remove books and
specialties; everyone else sees the list read-only.

Shared books are ANSWER SOURCES ONLY. They are not on anyone's shelf, are
never offered for reading or download, and nothing in the API returns
their file -- users get answers with short cited excerpts, the same as
from their own books. That is deliberate: the admin is responsible for
having the right to offer a title this way, and handing out the PDF itself
would be a different thing entirely.

Stored as one small JSON file under STORAGE_DIR/_shared/. Book ids carry an
"sh" prefix so they can never collide with a user's own book ids, since
both kinds share pdf_qa's index directory and the mini app's job table.
"""

import book_store
import json
import os
import tempfile
import threading
import time
import uuid

from config import STORAGE_DIR, VOYAGE_MODEL

SHARED_DIR = os.path.join(STORAGE_DIR, "_shared")
BOOKS_DIR = os.path.join(SHARED_DIR, "books")
_REGISTRY_PATH = os.path.join(SHARED_DIR, "library.json")

MAX_SPECIALTY_NAME_CHARS = 40
MAX_SPECIALTIES = 60

_lock = threading.RLock()


class SharedLibraryError(Exception):
    """A message safe to show the admin as-is."""


def is_shared_id(book_id) -> bool:
    return isinstance(book_id, str) and book_id.startswith("sh") and book_id.isalnum()


def _load() -> dict:
    try:
        with open(_REGISTRY_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.setdefault("specialties", [])
    data.setdefault("books", {})
    for b in data["books"].values():  # stale index = not searchable; see library.index_is_current
        if b.get("qa_indexed") and b.get("index_model", "voyage-4") != VOYAGE_MODEL:
            b["qa_indexed"] = False
    return data


def _save(data: dict) -> None:
    os.makedirs(SHARED_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=SHARED_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, _REGISTRY_PATH)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def clean_specialty_name(name) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise SharedLibraryError("Type a specialty name first.")
    if len(name) > MAX_SPECIALTY_NAME_CHARS:
        raise SharedLibraryError(f"Keep the specialty name under {MAX_SPECIALTY_NAME_CHARS} characters.")
    return name


def _match(specialties: list[str], name: str) -> str | None:
    """The stored spelling of a specialty, matched case-insensitively."""
    for s in specialties:
        if s.lower() == name.lower():
            return s
    return None


def list_specialties() -> list[str]:
    return list(_load()["specialties"])


def add_specialty(name: str) -> str:
    name = clean_specialty_name(name)
    with _lock:
        data = _load()
        existing = _match(data["specialties"], name)
        if existing:
            return existing
        if len(data["specialties"]) >= MAX_SPECIALTIES:
            raise SharedLibraryError("That's the maximum number of specialties.")
        data["specialties"].append(name)
        data["specialties"].sort(key=str.lower)
        _save(data)
    return name


def remove_specialty(name: str) -> None:
    """Only an EMPTY specialty can be removed -- books are removed one by one, on purpose."""
    with _lock:
        data = _load()
        existing = _match(data["specialties"], clean_specialty_name(name))
        if not existing:
            return
        if any(b["specialty"] == existing for b in data["books"].values()):
            raise SharedLibraryError("Remove this specialty's books first.")
        data["specialties"].remove(existing)
        _save(data)


def new_book_id() -> str:
    return "sh" + uuid.uuid4().hex[:10]


def add_book(specialty: str, title: str, pdf_path: str, page_count: int, added_by: int, book_id: str | None = None) -> str:
    specialty = add_specialty(specialty)
    book_id = book_id or new_book_id()
    with _lock:
        data = _load()
        data["books"][book_id] = {
            "title": title,
            "specialty": specialty,
            "pdf_path": pdf_path,
            "page_count": page_count,
            "qa_indexed": False,
            "num_chunks": 0,
            "index_error": None,
            "added_by": added_by,
            "added_at": time.time(),
        }
        _save(data)
    return book_id


def get_book(book_id: str) -> dict | None:
    return _load()["books"].get(book_id)


def list_books() -> dict:
    return _load()["books"]


def _update(book_id: str, **fields) -> bool:
    with _lock:
        data = _load()
        if book_id not in data["books"]:
            return False
        data["books"][book_id].update(fields)
        _save(data)
    return True


def rename_book(book_id: str, title) -> str:
    title = " ".join(str(title or "").split())
    if not title:
        raise SharedLibraryError("The title can't be empty.")
    if len(title) > 200:
        raise SharedLibraryError("Keep the title under 200 characters.")
    if not _update(book_id, title=title):
        raise SharedLibraryError("That book no longer exists.")
    return title


def set_chapters(book_id: str, chapters: list[dict]) -> bool:
    """Chapter ranges shared by every user who opens this book (computed once, see webapp_api)."""
    return _update(book_id, chapters=chapters)


def mark_indexed(book_id: str, num_chunks: int) -> bool:
    return _update(book_id, qa_indexed=True, num_chunks=num_chunks, index_error=None, index_model=VOYAGE_MODEL)


def set_index_error(book_id: str, error: str) -> bool:
    return _update(book_id, index_error=error)


def remove_book(book_id: str) -> dict | None:
    """Drop the registry entry and its PDF. The caller removes the Q&A index (pdf_qa.delete_index)."""
    with _lock:
        data = _load()
        book = data["books"].pop(book_id, None)
        if book is None:
            return None
        _save(data)
    try:
        os.remove(book["pdf_path"])
    except OSError:
        pass  # evicted to the bucket, or already gone
    book_store.delete_async(book["pdf_path"])
    return book


_SUGGESTIONS_PATH = os.path.join(SHARED_DIR, "suggestions.json")
MAX_SUGGESTIONS_PER_DAY = 5


def add_suggestion(user_id: int, who: str, specialty: str | None, title: str, note: str) -> dict:
    """Record a customer's book suggestion. Raises SharedLibraryError past the daily cap."""
    title = " ".join(str(title or "").split())[:200]
    note = str(note or "").strip()[:1000]
    if not title:
        raise SharedLibraryError("Type the book's title (and author, if you know it).")
    with _lock:
        try:
            with open(_SUGGESTIONS_PATH, encoding="utf-8") as f:
                items = json.load(f)
        except (OSError, ValueError):
            items = []
        day_ago = time.time() - 86400
        if sum(1 for s in items if s["user_id"] == user_id and s["ts"] > day_ago) >= MAX_SUGGESTIONS_PER_DAY:
            raise SharedLibraryError("Thanks! You've sent the most suggestions for today -- try again tomorrow.")
        item = {"ts": time.time(), "user_id": user_id, "who": who, "specialty": specialty, "title": title, "note": note}
        items.append(item)
        os.makedirs(SHARED_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=SHARED_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(items[-2000:], f)
        os.replace(tmp, _SUGGESTIONS_PATH)
    return item


def catalog() -> list[dict]:
    """
    What the mini app shows: every specialty with its books, in name order.
    pdf_path never leaves this module's callers -- see the module docstring.
    """
    data = _load()
    out = []
    for name in data["specialties"]:
        books = [
            {
                "book_id": bid,
                "title": b["title"],
                "page_count": b["page_count"],
                "qa_indexed": bool(b.get("qa_indexed")),
                "index_error": b.get("index_error"),
                "has_chapters": bool(b.get("chapters")),
            }
            for bid, b in data["books"].items()
            if b["specialty"] == name
        ]
        books.sort(key=lambda b: b["title"].lower())
        out.append({"name": name, "books": books})
    return out

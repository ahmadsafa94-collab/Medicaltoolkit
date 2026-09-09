"""
Per-user book registry -- the single source of truth for "what books does
this user have." This backs BOTH the Book Shelf mini app and the chat's
/ask book-picker, so a book uploaded from either surface shows up on both.

A book gets an entry here the moment it's uploaded (from the chat's PDF
upload in bot.py, or the mini app's own uploader in webapp_api.py) --
chapter division and Q&A indexing are metadata attached to that SAME entry
later, not separate registrations. This is a deliberate change from the
registry's earlier shape, where an entry was only created once a book had
already been indexed for Q&A: now that the Book Shelf needs to show every
uploaded book (indexed or not, divided into chapters or not), "uploaded"
has to be the moment of registration, and everything else is just fields
that fill in over time.

One consequence: the original PDF at pdf_path is now kept indefinitely
(never auto-deleted after indexing, unlike the old design), since the
Book Shelf's "Read", "Divide into chapters", "Summarize", and "Quiz"
features all need the actual file, not just its embeddings. See bot.py and
webapp_api.py's docstrings for where that file lives and how it survives
each handler's own cleanup step.

Persisted per-user as a small JSON file -- this is registry metadata only
(title, page count, chapter boundaries, a Q&A-indexed flag), not the PDF
bytes or embeddings themselves; those live at pdf_path and in pdf_qa's
QA_INDEX_DIR respectively.
"""

import json
import os
import time
import uuid

from config import STORAGE_DIR


def _registry_path(user_id: int) -> str:
    return os.path.join(STORAGE_DIR, str(user_id), "library.json")


def _load(user_id: int) -> dict:
    path = _registry_path(user_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save(user_id: int, registry: dict):
    path = _registry_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(registry, f)
    os.replace(tmp_path, path)  # atomic on POSIX


def add_book(user_id: int, title: str, pdf_path: str, page_count: int, source: str = "shelf") -> str:
    """
    Register a newly-uploaded book. `source` is "chat" or "shelf" -- purely
    informational, in case the UI ever wants to show where a book came from.
    """
    registry = _load(user_id)
    book_id = uuid.uuid4().hex[:10]
    registry[book_id] = {
        "title": title,
        "pdf_path": pdf_path,
        "page_count": page_count,
        "uploaded_at": time.time(),
        "source": source,
        "chapters": None,           # list of {"title","start_page","end_page"} once divided
        "chapters_status": "none",  # "none" | "pending" | "done" | "error"
        "chapters_error": None,
        "qa_indexed": False,
        "num_chunks": 0,
        "bookmark_page": None,  # last page the user bookmarked in the reader, 1-indexed
        "cover_color": None,    # user-chosen hex color ("#rrggbb") for the shelf cover, or None for the default
    }
    _save(user_id, registry)
    return book_id


def list_books(user_id: int) -> dict:
    """Return {book_id: entry, ...}, newest upload last (insertion order)."""
    return _load(user_id)


def get_book(user_id: int, book_id: str) -> dict | None:
    return _load(user_id).get(book_id)


def _update(user_id: int, book_id: str, **fields) -> bool:
    registry = _load(user_id)
    if book_id not in registry:
        return False
    registry[book_id].update(fields)
    _save(user_id, registry)
    return True


def set_chapters_pending(user_id: int, book_id: str) -> bool:
    return _update(user_id, book_id, chapters_status="pending", chapters_error=None)


def set_chapters(user_id: int, book_id: str, chapters: list[dict]) -> bool:
    """chapters: [{"title": str, "start_page": int, "end_page": int}, ...], 1-indexed inclusive."""
    return _update(user_id, book_id, chapters=chapters, chapters_status="done", chapters_error=None)


def set_chapters_error(user_id: int, book_id: str, error: str) -> bool:
    return _update(user_id, book_id, chapters_status="error", chapters_error=error)


def mark_indexed(user_id: int, book_id: str, num_chunks: int) -> bool:
    return _update(user_id, book_id, qa_indexed=True, num_chunks=num_chunks)


def rename_book(user_id: int, book_id: str, new_title: str) -> bool:
    return _update(user_id, book_id, title=new_title)


def set_bookmark(user_id: int, book_id: str, page: int | None) -> bool:
    """page=None clears the bookmark."""
    return _update(user_id, book_id, bookmark_page=page)


def set_cover_color(user_id: int, book_id: str, color: str | None) -> bool:
    """color=None resets the cover to the default wood-gradient look."""
    return _update(user_id, book_id, cover_color=color)


def remove_book(user_id: int, book_id: str, delete_file: bool = False) -> bool:
    """
    delete_file=True also best-effort deletes the underlying PDF -- only
    pass that for an explicit user-initiated "remove from shelf," never for
    a background job's own error handling (a failed chapter-division or
    indexing attempt should leave the book on the shelf, just not divided/
    indexed, so the user can retry -- it should never make the book vanish).
    """
    registry = _load(user_id)
    if book_id not in registry:
        return False
    entry = registry.pop(book_id)
    _save(user_id, registry)
    if delete_file:
        try:
            os.remove(entry["pdf_path"])
        except OSError:
            pass
    return True

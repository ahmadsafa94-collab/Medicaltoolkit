"""
Personal note-taking, tied to a specific book and page -- "synced to
bookmarks" in the sense that a new note defaults to the book's current
🔖 bookmarked page (see library.py's bookmark_page) when one is set, so
jotting a note down naturally follows from "I bookmarked this page because
it mattered" without having to re-type the page number.

Storage: STORAGE_DIR/{user_id}/notes/{book_id}.json -- same small
per-purpose JSON file pattern as flashcards.py/library.py's own sub-stores.
"""

import json
import os
import time
import uuid

from config import STORAGE_DIR

MAX_NOTES_PER_BOOK = 300


def _notes_path(user_id: int, book_id: str) -> str:
    return os.path.join(STORAGE_DIR, str(user_id), "notes", f"{book_id}.json")


def _load(user_id: int, book_id: str) -> list[dict]:
    path = _notes_path(user_id, book_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save(user_id: int, book_id: str, notes: list[dict]) -> None:
    path = _notes_path(user_id, book_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(notes, f)
    os.replace(tmp_path, path)


def add_note(user_id: int, book_id: str, page: int | None, text: str) -> dict:
    notes = _load(user_id, book_id)
    now = time.time()
    note = {
        "id": uuid.uuid4().hex[:10],
        "book_id": book_id,
        "page": page,
        "text": text,
        "created_at": now,
        "updated_at": now,
    }
    notes.append(note)
    notes = notes[-MAX_NOTES_PER_BOOK:]
    _save(user_id, book_id, notes)
    return note


def list_notes(user_id: int, book_id: str) -> list[dict]:
    notes = _load(user_id, book_id)
    notes.sort(key=lambda n: n.get("updated_at", 0), reverse=True)
    return notes


def get_note(user_id: int, book_id: str, note_id: str) -> dict | None:
    return next((n for n in _load(user_id, book_id) if n["id"] == note_id), None)


def update_note(user_id: int, book_id: str, note_id: str, text: str) -> bool:
    notes = _load(user_id, book_id)
    note = next((n for n in notes if n["id"] == note_id), None)
    if note is None:
        return False
    note["text"] = text
    note["updated_at"] = time.time()
    _save(user_id, book_id, notes)
    return True


def delete_note(user_id: int, book_id: str, note_id: str) -> bool:
    notes = _load(user_id, book_id)
    filtered = [n for n in notes if n["id"] != note_id]
    if len(filtered) == len(notes):
        return False
    _save(user_id, book_id, filtered)
    return True


def delete_notes_for_book(user_id: int, book_id: str) -> None:
    try:
        os.remove(_notes_path(user_id, book_id))
    except OSError:
        pass

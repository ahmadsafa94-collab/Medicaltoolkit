"""
Tracks which books each user has made searchable (indexed for "Ask my book"),
so /ask can show them a pick-list without re-scanning the filesystem.
Persisted per-user as a small JSON file -- this is registry metadata only
(title, short id, page count), not the embeddings themselves (those live in
pdf_qa's QA_INDEX_DIR).
"""

import json
import os
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
    os.replace(tmp_path, path)  # atomic on POSIX, consistent with user_history.py's approach


def add_book(user_id: int, title: str, num_chunks: int) -> str:
    """Register a newly-indexed book, return its short book_id."""
    registry = _load(user_id)
    book_id = uuid.uuid4().hex[:10]
    registry[book_id] = {"title": title, "num_chunks": num_chunks}
    _save(user_id, registry)
    return book_id


def list_books(user_id: int) -> dict:
    """Return {book_id: {"title": ..., "num_chunks": ...}, ...}"""
    return _load(user_id)


def get_book(user_id: int, book_id: str) -> dict | None:
    return _load(user_id).get(book_id)


def update_chunk_count(user_id: int, book_id: str, num_chunks: int):
    registry = _load(user_id)
    if book_id in registry:
        registry[book_id]["num_chunks"] = num_chunks
        _save(user_id, registry)


def remove_book(user_id: int, book_id: str) -> bool:
    registry = _load(user_id)
    if book_id in registry:
        del registry[book_id]
        _save(user_id, registry)
        return True
    return False

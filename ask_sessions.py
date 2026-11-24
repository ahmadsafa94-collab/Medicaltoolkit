"""
Saved Ask-tab topics: every question asked across books is kept, grouped
by topic (a topic runs until the user taps "New topic"), so answers can be
reopened, continued, or exported as a PDF later.

One small JSON file per user. Each topic keeps the books it was asked
against, so reopening it ticks the same books again.
"""

import json
import os
import tempfile
import threading
import time

from config import STORAGE_DIR

MAX_TOPICS = 200          # oldest-updated topics are dropped past this
MAX_TURNS_PER_TOPIC = 80  # oldest turns within one topic are dropped past this

_lock = threading.Lock()


def _path(user_id: int) -> str:
    return os.path.join(STORAGE_DIR, str(user_id), "ask_topics.json")


def _load(user_id: int) -> dict:
    try:
        with open(_path(user_id), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(user_id: int, data: dict) -> None:
    path = _path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def valid_id(topic_id) -> bool:
    return isinstance(topic_id, str) and 8 <= len(topic_id) <= 40 and topic_id.replace("-", "").isalnum()


KINDS = ("books", "drug", "interactions")


def append_turn(
    user_id: int, topic_id: str, books: list[dict], question: str, answer: str, sources: list,
    kind: str = "books", title: str | None = None,
) -> None:
    """
    kind: "books" (Ask your books), "drug" (Drug LookUp -- books holds the
    drug), or "interactions" (one check per topic). title defaults to the
    first question.
    """
    with _lock:
        data = _load(user_id)
        now = time.time()
        topic = data.get(topic_id)
        if topic is None:
            topic = {"title": (title or question).strip()[:120], "created": now, "turns": [], "kind": kind}
        topic["updated"] = now
        topic["books"] = [{"book_id": b["book_id"], "title": b["title"]} for b in books]
        topic["turns"].append({"q": question, "a": answer, "sources": sources, "ts": now})
        topic["turns"] = topic["turns"][-MAX_TURNS_PER_TOPIC:]
        data[topic_id] = topic
        if len(data) > MAX_TOPICS:
            for old in sorted(data, key=lambda k: data[k].get("updated", 0))[: len(data) - MAX_TOPICS]:
                data.pop(old, None)
        _save(user_id, data)


def list_topics(user_id: int, kind: str | None = None) -> list[dict]:
    data = _load(user_id)
    out = [
        {
            "topic_id": tid,
            "title": t.get("title") or "Untitled",
            "updated": t.get("updated", 0),
            "turns": len(t.get("turns", [])),
            "books": [b["title"] for b in t.get("books", [])],
            "kind": t.get("kind", "books"),
        }
        for tid, t in data.items()
        if kind is None or t.get("kind", "books") == kind
    ]
    out.sort(key=lambda t: t["updated"], reverse=True)
    return out


def get_topic(user_id: int, topic_id: str) -> dict | None:
    return _load(user_id).get(topic_id)


def delete_topic(user_id: int, topic_id: str) -> bool:
    with _lock:
        data = _load(user_id)
        if data.pop(topic_id, None) is None:
            return False
        _save(user_id, data)
        return True

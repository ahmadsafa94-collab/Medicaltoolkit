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


# ---------------------------------------------------------------------------
# Ask AI (Q&A) conversation history -- lets the mini app's "🕘 History"
# button reopen a past conversation instead of every chat thread vanishing
# the moment the user leaves the Ask AI panel. Kept in its own per-book file
# (rather than inline on the book's registry entry) since full Q&A text
# across many sessions can grow much larger than the rest of a book's small
# metadata record, and there's no reason to round-trip it on every plain
# get_book()/list_books() call that doesn't need it.
# ---------------------------------------------------------------------------

MAX_QA_SESSIONS_PER_BOOK = 50   # oldest-updated sessions are dropped past this
MAX_TURNS_PER_QA_SESSION = 60   # oldest turns within one session are dropped past this


def _qa_sessions_path(user_id: int, book_id: str) -> str:
    return os.path.join(STORAGE_DIR, str(user_id), "qa_sessions", f"{book_id}.json")


def _load_qa_sessions(user_id: int, book_id: str) -> list[dict]:
    path = _qa_sessions_path(user_id, book_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_qa_sessions(user_id: int, book_id: str, sessions: list[dict]) -> None:
    path = _qa_sessions_path(user_id, book_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(sessions, f)
    os.replace(tmp_path, path)


def list_qa_sessions(user_id: int, book_id: str) -> list[dict]:
    """
    Lightweight summaries only (no turn text) -- newest-updated first -- for
    a history list UI. Use get_qa_session() to fetch one session's full turns.
    """
    sessions = _load_qa_sessions(user_id, book_id)
    sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    summaries = []
    for s in sessions:
        turns = s.get("turns") or []
        summaries.append({
            "session_id": s["session_id"],
            "mode": s.get("mode", "single"),
            "started_at": s.get("started_at"),
            "updated_at": s.get("updated_at"),
            "turn_count": len(turns),
            "preview": turns[0]["question"][:80] if turns else "",
        })
    return summaries


def get_qa_session(user_id: int, book_id: str, session_id: str) -> dict | None:
    for s in _load_qa_sessions(user_id, book_id):
        if s.get("session_id") == session_id:
            return s
    return None


def append_qa_turn(
    user_id: int, book_id: str, session_id: str, mode: str, question: str, answer: str, sources: list
) -> dict:
    """
    Append one question/answer turn to `session_id`, creating that session
    if it doesn't exist yet. `session_id` is chosen client-side (see
    app.js) once per conversation thread -- every turn asked in the same
    thread lands in the same session, so the whole conversation (not just
    one exchange) is what "🕘 History" reopens later.
    """
    sessions = _load_qa_sessions(user_id, book_id)
    now = time.time()
    session = next((s for s in sessions if s.get("session_id") == session_id), None)
    if session is None:
        session = {"session_id": session_id, "mode": mode, "started_at": now, "updated_at": now, "turns": []}
        sessions.append(session)

    session["mode"] = mode
    session["updated_at"] = now
    session["turns"].append({"question": question, "answer": answer, "sources": sources})
    session["turns"] = session["turns"][-MAX_TURNS_PER_QA_SESSION:]

    sessions.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    sessions = sessions[:MAX_QA_SESSIONS_PER_BOOK]
    _save_qa_sessions(user_id, book_id, sessions)
    return session


def delete_qa_sessions_for_book(user_id: int, book_id: str) -> None:
    """Best-effort cleanup when the book itself is deleted -- avoids an orphaned history file."""
    try:
        os.remove(_qa_sessions_path(user_id, book_id))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Quiz attempts -- backs the mini app's "🏁 End Test" summary and its
# "🕘 Quiz History" button, same rationale (and same per-book file, kept
# separate from the small registry entry) as the Q&A sessions above.
# ---------------------------------------------------------------------------

MAX_QUIZ_ATTEMPTS_PER_BOOK = 50  # oldest attempts are dropped past this


def _quiz_attempts_path(user_id: int, book_id: str) -> str:
    return os.path.join(STORAGE_DIR, str(user_id), "quiz_attempts", f"{book_id}.json")


def _load_quiz_attempts(user_id: int, book_id: str) -> list[dict]:
    path = _quiz_attempts_path(user_id, book_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_quiz_attempts(user_id: int, book_id: str, attempts: list[dict]) -> None:
    path = _quiz_attempts_path(user_id, book_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(attempts, f)
    os.replace(tmp_path, path)


def list_quiz_attempts(user_id: int, book_id: str) -> list[dict]:
    """Lightweight summaries only (no questions) -- newest first -- for a history list UI."""
    attempts = _load_quiz_attempts(user_id, book_id)
    attempts.sort(key=lambda a: a.get("created_at", 0), reverse=True)
    return [
        {
            "attempt_id": a["attempt_id"],
            "created_at": a.get("created_at"),
            "difficulty": a.get("difficulty"),
            "chapter_titles": a.get("chapter_titles", []),
            "total": a.get("total", 0),
            "correct_count": a.get("correct_count", 0),
            "percentage": a.get("percentage", 0),
        }
        for a in attempts
    ]


def get_quiz_attempt(user_id: int, book_id: str, attempt_id: str) -> dict | None:
    for a in _load_quiz_attempts(user_id, book_id):
        if a.get("attempt_id") == attempt_id:
            return a
    return None


def save_quiz_attempt(user_id: int, book_id: str, attempt: dict) -> dict:
    """
    `attempt` is a fully-formed record (attempt_id, created_at, difficulty,
    chapter_titles, questions, answers, correct_count, total, percentage --
    see webapp_api.py's save_quiz_attempt_endpoint, which computes the score
    server-side rather than trusting the client's own tally). Stored
    wholesale so "🕘 Quiz History" can show a full read-only review later,
    not just the score.
    """
    attempts = _load_quiz_attempts(user_id, book_id)
    attempts.append(attempt)
    attempts.sort(key=lambda a: a.get("created_at", 0), reverse=True)
    attempts = attempts[:MAX_QUIZ_ATTEMPTS_PER_BOOK]
    _save_quiz_attempts(user_id, book_id, attempts)
    return attempt


def delete_quiz_attempts_for_book(user_id: int, book_id: str) -> None:
    """Best-effort cleanup when the book itself is deleted -- avoids an orphaned history file."""
    try:
        os.remove(_quiz_attempts_path(user_id, book_id))
    except OSError:
        pass


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

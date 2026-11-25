"""
The reader's own tools on top of a book's pages:

  highlights   coloured highlights of selected text, per user and book,
               anchored to the page's text layer (span index + offset), so
               they come back in place at any zoom
  search       find a word or phrase across every page of the book; page
               texts are extracted once (pypdfium2, fast) and cached
  explain      the AI explains a selected passage, in the context of the book

Storage: STORAGE_DIR/{user_id}/highlights/{book_id}.json and
STORAGE_DIR/pagetext/{book_id}.json (page texts are the same for everyone
reading that file, so the cache is shared).
"""

import book_store
import json
import logging
import os
import re
import threading
import time
import uuid

import cost_ledger
from config import CLAUDE_MODEL, STORAGE_DIR

logger = logging.getLogger(__name__)

COLORS = ("yellow", "green", "blue", "pink")
MAX_HIGHLIGHTS_PER_BOOK = 1000
MAX_HIGHLIGHT_CHARS = 3000
MAX_SEARCH_RESULTS = 200

_BOOK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,80}")
_lock = threading.Lock()


def _check_id(book_id: str) -> str:
    # The id becomes part of a path: never let a forged one walk out of it.
    if not isinstance(book_id, str) or not _BOOK_ID_RE.fullmatch(book_id):
        raise ValueError("invalid book id")
    return book_id


def _write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


# ---------------------------------------------------------------------
# Highlights
# ---------------------------------------------------------------------

def _hl_path(user_id: int, book_id: str) -> str:
    return os.path.join(STORAGE_DIR, str(int(user_id)), "highlights", f"{_check_id(book_id)}.json")


def _hl_load(user_id: int, book_id: str) -> list[dict]:
    try:
        with open(_hl_path(user_id, book_id)) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def list_highlights(user_id: int, book_id: str) -> list[dict]:
    items = _hl_load(user_id, book_id)
    items.sort(key=lambda h: (h.get("page") or 0, (h.get("anchor") or {}).get("s", 0), (h.get("anchor") or {}).get("so", 0)))
    return items


def _valid_anchor(anchor) -> dict | None:
    """{s, so, e, eo}: first/last text-layer span and the offsets in them."""
    if not isinstance(anchor, dict):
        return None
    try:
        vals = {k: anchor[k] for k in ("s", "so", "e", "eo")}
    except KeyError:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100000 for v in vals.values()):
        return None
    if (vals["e"], vals["eo"]) <= (vals["s"], vals["so"]) or vals["e"] - vals["s"] > 2000:
        return None
    return vals


def add_highlight(user_id: int, book_id: str, page: int, text: str, color: str, anchor) -> dict:
    anchor = _valid_anchor(anchor)
    if anchor is None:
        raise ValueError("invalid selection")
    if color not in COLORS:
        raise ValueError("invalid colour")
    item = {
        "id": uuid.uuid4().hex[:10],
        "page": page,
        "text": " ".join(text.split())[:MAX_HIGHLIGHT_CHARS],
        "color": color,
        "anchor": anchor,
        "created_at": time.time(),
    }
    with _lock:
        items = _hl_load(user_id, book_id)
        if len(items) >= MAX_HIGHLIGHTS_PER_BOOK:
            raise ValueError(f"This book already has {MAX_HIGHLIGHTS_PER_BOOK} highlights. Delete some first.")
        items.append(item)
        _write(_hl_path(user_id, book_id), items)
    return item


def update_highlight(user_id: int, book_id: str, hl_id: str, color: str) -> dict | None:
    if color not in COLORS:
        raise ValueError("invalid colour")
    with _lock:
        items = _hl_load(user_id, book_id)
        item = next((h for h in items if h.get("id") == hl_id), None)
        if item is None:
            return None
        item["color"] = color
        _write(_hl_path(user_id, book_id), items)
    return item


def delete_highlight(user_id: int, book_id: str, hl_id: str) -> bool:
    with _lock:
        items = _hl_load(user_id, book_id)
        kept = [h for h in items if h.get("id") != hl_id]
        if len(kept) == len(items):
            return False
        _write(_hl_path(user_id, book_id), kept)
    return True


def delete_highlights_for_book(user_id: int, book_id: str) -> None:
    try:
        os.remove(_hl_path(user_id, book_id))
    except (OSError, ValueError):
        pass


def highlights_as_text(title: str, items: list[dict]) -> str:
    """The highlights for the PDF export, page by page."""
    lines = [f"{len(items)} highlight{'s' if len(items) != 1 else ''} from {title}", ""]
    for h in items:
        lines.append(f"Page {h.get('page')} ({h.get('color')}):")
        lines.append(f"“{h.get('text', '')}”")
        lines.append("")
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------
# Search in the book
# ---------------------------------------------------------------------

_text_locks: dict[str, threading.Lock] = {}


def _pagetext_path(book_id: str) -> str:
    return os.path.join(STORAGE_DIR, "pagetext", f"{_check_id(book_id)}.json")


def page_texts(book_id: str, pdf_path: str) -> list[str]:
    """Every page's text, extracted once and cached. Synchronous: run in a thread."""
    path = _pagetext_path(book_id)
    book_store.ensure_local(pdf_path)  # an evicted book comes back from the bucket first
    try:
        mtime = os.path.getmtime(pdf_path)
    except OSError:
        mtime = None
    lock = _text_locks.setdefault(book_id, threading.Lock())
    with lock:
        try:
            with open(path) as f:
                cached = json.load(f)
            if cached.get("mtime") == mtime and isinstance(cached.get("pages"), list):
                return cached["pages"]
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        import pypdfium2 as pdfium  # already a dependency (book covers)

        pages = []
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                try:
                    tp = page.get_textpage()
                    pages.append(tp.get_text_range() or "")
                    tp.close()
                except Exception:
                    pages.append("")
                finally:
                    page.close()
        finally:
            pdf.close()
        try:
            _write(path, {"mtime": mtime, "pages": pages})
        except OSError:
            logger.exception("Couldn't cache page texts for %s", book_id)
        return pages


def _norm(text: str) -> str:
    # Line-end hyphenation and line breaks shouldn't stop a match.
    text = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", text)
    return re.sub(r"\s+", " ", text)


def search(book_id: str, pdf_path: str, query: str) -> dict:
    """{"results": [{"page", "snippet", "start", "end"}], "total": n, "pages_with_text": n}. Synchronous."""
    q = re.sub(r"\s+", " ", query).strip()
    pages = page_texts(book_id, pdf_path)
    results, total = [], 0
    pattern = re.compile(re.escape(q), re.I)
    for num, raw in enumerate(pages, 1):
        text = _norm(raw)
        for m in pattern.finditer(text):
            total += 1
            if len(results) < MAX_SEARCH_RESULTS:
                a, b = max(0, m.start() - 70), min(len(text), m.end() + 70)
                snippet = ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")
                start = m.start() - a + (1 if a else 0)
                results.append({"page": num, "snippet": snippet, "start": start, "end": start + (m.end() - m.start())})
    return {"results": results, "total": total, "pages_with_text": sum(1 for p in pages if p.strip())}


# ---------------------------------------------------------------------
# Explain a passage
# ---------------------------------------------------------------------

def explain(book_title: str, page: int | None, passage: str, context: str, language: str = "English") -> str:
    """The AI explains a selected passage. Synchronous: run in a thread."""
    from pdf_processor import client  # the one Anthropic client

    system = (
        "You help a medical student or clinician understand a passage they selected in a textbook. "
        "Explain it clearly and concisely for a phone screen: what it means, the key terms, and why it matters "
        "clinically. Use the surrounding page text for context. Where you add general medical knowledge beyond "
        "the passage, say so briefly. Do not invent doses or numbers that aren't in the text. "
        f"Respond in {language}."
    )
    content = (
        f"Book: {book_title}" + (f", page {page}" if page else "") + "\n\n"
        f"SELECTED PASSAGE:\n{passage}\n\n"
        + (f"SURROUNDING PAGE TEXT (for context):\n{context}\n" if context else "")
    )
    response = client.messages.create(
        model=CLAUDE_MODEL,
        thinking={"type": "disabled"},
        max_tokens=900,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    try:
        cost_ledger.record_claude_response("reader_explain", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")
    return "".join(b.text for b in response.content if b.type == "text").strip()

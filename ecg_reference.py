"""
Admin-curated ECG teaching books, used to ground 🫀 ECG Interpretation in
real textbook material instead of the model's own recall.

This is the ECG counterpart of what ecg_lab_ai.py already does for labs,
where glossary.py's curated reference ranges are handed to Claude as the
only authoritative source of normal/abnormal. ECG had no such anchor at
all -- the read came purely from what the model happened to remember -- so
an admin uploads ECG textbooks here once and every user's ECG read is
checked against them.

Nothing here "trains" or fine-tunes the model, which isn't something the
Anthropic API exposes and isn't what's needed: the books are indexed as
embeddings (retrieval), and the passages relevant to THIS tracing are
pulled into the interpretation prompt at read time. Practically that gets
the same result a user wants from "learn from these books" -- the answer
follows the book's criteria and terminology -- while staying current with
whatever books the admin has loaded, with no retraining step.

Where retrieval happens matters: ecg_lab_ai.interpret_ecg is a draft pass
then an independent verify pass. The draft's INPUT is an image, which
can't be embedded against text chunks, but its OUTPUT is a structured text
read (Rate/Rhythm/Axis/Intervals/morphology) -- an excellent retrieval
query. So the draft runs unchanged, its text fetches the matching teaching
passages, and the verify pass corrects the draft against them.

Storage mirrors book_requests.py: a shared JSON catalog under _admin/ (an
admin needs these regardless of which user is asking), with the PDFs in
_admin/ecg_reference/ and the vector indexes in pdf_qa.py's own
QA_INDEX_DIR under an "ecgref_" id prefix, so pdf_qa's build/load/delete
index code is reused as-is rather than duplicated.
"""

import json
import logging
import os
import time
import uuid

import numpy as np

import cost_ledger
import pdf_qa
from config import (
    ECG_REFERENCE_DIR,
    ECG_REFERENCE_MAX_CHARS,
    ECG_REFERENCE_TOP_K,
    STORAGE_DIR,
    VOYAGE_MODEL,
)

logger = logging.getLogger(__name__)

_ADMIN_DIR = os.path.join(STORAGE_DIR, "_admin")
_CATALOG_PATH = os.path.join(_ADMIN_DIR, "ecg_reference.json")

# ref_id -> (meta_mtime, meta, vectors). Every ECG read searches every
# reference book, so re-reading a few MB of vectors off disk per ECG would
# be pure waste; the mtime check means a re-indexed book is still picked up
# without a restart.
_INDEX_CACHE: dict[str, tuple[float, dict, np.ndarray]] = {}


def _load_catalog() -> dict:
    if not os.path.exists(_CATALOG_PATH):
        return {}
    try:
        with open(_CATALOG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_catalog(catalog: dict) -> None:
    os.makedirs(_ADMIN_DIR, exist_ok=True)
    tmp_path = _CATALOG_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(catalog, f)
    os.replace(tmp_path, _CATALOG_PATH)


def index_id(ref_id: str) -> str:
    """The pdf_qa index namespace for a reference book (ref_id is a generated hex id, so this is path-safe)."""
    return f"ecgref_{ref_id}"


def list_books() -> list[dict]:
    """Every registered reference book, oldest first."""
    return sorted(_load_catalog().values(), key=lambda b: b["added_at"])


def has_books() -> bool:
    return bool(_load_catalog())


def get_book(ref_id: str) -> dict | None:
    return _load_catalog().get(ref_id)


def new_ref_id() -> str:
    return uuid.uuid4().hex[:8]


def pdf_path_for(ref_id: str, filename: str) -> str:
    os.makedirs(ECG_REFERENCE_DIR, exist_ok=True)
    return os.path.join(ECG_REFERENCE_DIR, f"{ref_id}_{filename}")


def register_book(ref_id: str, title: str, pdf_path: str, num_chunks: int, added_by: int) -> dict:
    catalog = _load_catalog()
    record = {
        "ref_id": ref_id,
        "title": title,
        "pdf_path": pdf_path,
        "num_chunks": num_chunks,
        "added_by": added_by,
        "added_at": time.time(),
    }
    catalog[ref_id] = record
    _save_catalog(catalog)
    return record


def remove_book(ref_id: str) -> dict | None:
    """Drop a reference book: catalog entry, its vector index, and the stored PDF."""
    catalog = _load_catalog()
    record = catalog.pop(ref_id, None)
    if record is None:
        return None
    _save_catalog(catalog)
    _INDEX_CACHE.pop(ref_id, None)
    pdf_qa.delete_index(index_id(ref_id))
    try:
        os.remove(record["pdf_path"])
    except OSError:
        pass
    return record


async def index_book(pdf_path: str, ref_id: str, title: str, progress_cb=None) -> int:
    """Embed a reference book into its own pdf_qa index. Returns the chunk count."""
    return await pdf_qa.build_index(pdf_path, index_id(ref_id), title, progress_cb=progress_cb)


def _cached_index(ref_id: str) -> tuple[dict, np.ndarray] | None:
    meta_path, _ = pdf_qa._index_paths(index_id(ref_id))
    try:
        mtime = os.path.getmtime(meta_path)
    except OSError:
        return None

    cached = _INDEX_CACHE.get(ref_id)
    if cached and cached[0] == mtime:
        return cached[1], cached[2]

    loaded = pdf_qa.load_index(index_id(ref_id))
    if loaded is None:
        return None
    meta, vectors = loaded
    _INDEX_CACHE[ref_id] = (mtime, meta, vectors)
    return meta, vectors


def search_sync(query: str, top_k: int = ECG_REFERENCE_TOP_K) -> list[dict]:
    """
    Best passages for `query` across every reference book, globally ranked.

    Synchronous on purpose: ecg_lab_ai.interpret_ecg is itself sync and is
    already run via asyncio.to_thread by its callers, so blocking here is
    correct and keeps the whole interpretation path a single plain function
    rather than forcing an async rewrite of every ECG caller.
    """
    books = list_books()
    if not books or not query.strip():
        return []

    result = pdf_qa.voyage_client.embed([query], model=VOYAGE_MODEL, input_type="query")
    query_vec = np.array(result.embeddings[0], dtype=np.float32)
    norm = np.linalg.norm(query_vec)
    if norm > 0:
        query_vec /= norm

    try:
        cost_ledger.record_voyage_tokens("ecg_reference_search", len(query) // 4)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    hits = []
    for book in books:
        loaded = _cached_index(book["ref_id"])
        if loaded is None:
            continue
        meta, vectors = loaded
        scores = vectors @ query_vec  # cosine similarity; both sides pre-normalized at index time
        for idx in np.argsort(scores)[::-1][: min(top_k, len(scores))]:
            chunk = meta["chunks"][idx]
            hits.append(
                {
                    "text": chunk["text"],
                    "page": chunk["page"],
                    "title": book["title"],
                    "score": float(scores[idx]),
                }
            )

    hits.sort(key=lambda h: -h["score"])
    return hits[:top_k]


def reference_context(query: str, top_k: int = ECG_REFERENCE_TOP_K) -> str:
    """
    Retrieved teaching passages formatted for a prompt, or "" when there's
    nothing to add (no books uploaded, no index, or retrieval failed).

    Deliberately swallows its own errors: ECG interpretation worked before
    any reference book existed and must keep working if Voyage is down or
    an index is missing -- grounding is an enhancement to the read, never a
    prerequisite for it.
    """
    try:
        hits = search_sync(query, top_k=top_k)
    except Exception:
        logger.exception("ECG reference retrieval failed (non-fatal, falling back to an unreferenced read)")
        return ""

    blocks = []
    used = 0
    for hit in hits:
        block = f"[{hit['title']}, p.{hit['page']}]\n{hit['text']}"
        if used + len(block) > ECG_REFERENCE_MAX_CHARS:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)

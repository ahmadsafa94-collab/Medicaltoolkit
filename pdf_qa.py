"""
"Ask my book" -- semantic Q&A over a previously-uploaded PDF, similar to
NotebookLM: the user asks a question in plain language, we find the most
relevant passages by meaning (not just keyword match), and Claude answers
citing the exact page numbers those passages came from.

Pipeline:
  1. extract_full_page_text()  -- pull ALL text per page (not the short
     previews pdf_processor.py uses for chapter detection)
  2. build_chunks()            -- break each page into overlapping ~1200-char
     windows so a chunk is small enough to embed precisely but big enough to
     contain a full thought
  3. embed_texts()              -- call Voyage AI to turn chunks into vectors
  4. build_index() / load_index -- persist chunks+vectors to disk per book
  5. search_index()            -- cosine-similarity nearest neighbors for a
     question's embedding
  6. answer_question()         -- feed the top matches to Claude with a
     citation-focused system prompt

Same "don't let a slow/stalled external API hang the bot forever" concern as
pdf_processor.py applies here, doubly so -- indexing a long book makes many
sequential Voyage API calls in a loop, so the Voyage client is given an
explicit per-request timeout AND a few retries (its own default is NO
timeout and NO retries at all, confirmed against Voyage's docs).
"""

import asyncio
import json
import logging
import os
import re

import numpy as np
import pdfplumber
import voyageai

import cost_ledger
from config import (
    CLAUDE_MODEL,
    VOYAGE_API_KEY,
    VOYAGE_MODEL,
    QA_INDEX_DIR,
    QA_CHUNK_SIZE_CHARS,
    QA_CHUNK_OVERLAP_CHARS,
    QA_TOP_K,
    QA_EMBED_BATCH_SIZE,
    VOYAGE_CLIENT_TIMEOUT_SECONDS,
    VOYAGE_CLIENT_MAX_RETRIES,
)
from pdf_processor import client as claude_client  # reuse the one Anthropic client instance, not a second one

logger = logging.getLogger(__name__)

voyage_client = voyageai.Client(
    api_key=VOYAGE_API_KEY,
    timeout=VOYAGE_CLIENT_TIMEOUT_SECONDS,
    max_retries=VOYAGE_CLIENT_MAX_RETRIES,
)


class IndexingError(Exception):
    pass


def extract_full_page_text(pdf_path: str) -> list[str]:
    """Return full text of every page (unlike pdf_processor's short previews)."""
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages.append(re.sub(r"\s+", " ", text).strip())
    return pages


def build_chunks(page_texts: list[str]) -> list[dict]:
    """
    Split each page's text into overlapping chunks. Chunks never cross a
    page boundary, so every chunk has one unambiguous page number to cite.
    Returns list of {"text": str, "page": int (1-indexed)}.
    """
    chunks = []
    for page_num, text in enumerate(page_texts, start=1):
        if not text:
            continue
        if len(text) <= QA_CHUNK_SIZE_CHARS:
            chunks.append({"text": text, "page": page_num})
            continue

        start = 0
        while start < len(text):
            end = start + QA_CHUNK_SIZE_CHARS
            chunk_text = text[start:end]
            # avoid cutting mid-word at the boundary
            if end < len(text):
                chunk_text = chunk_text.rsplit(" ", 1)[0]
            chunks.append({"text": chunk_text.strip(), "page": page_num})
            start += QA_CHUNK_SIZE_CHARS - QA_CHUNK_OVERLAP_CHARS

    return chunks


async def embed_texts(texts: list[str], input_type: str) -> list[list[float]]:
    """
    Embed a list of texts via Voyage AI, batching to stay under API limits.
    input_type is "document" for indexing, "query" for a user's question --
    Voyage's models are tuned differently for each, which meaningfully
    improves retrieval quality over using the same embedding for both.
    """
    all_embeddings = []
    for i in range(0, len(texts), QA_EMBED_BATCH_SIZE):
        batch = texts[i:i + QA_EMBED_BATCH_SIZE]
        result = await asyncio.to_thread(
            voyage_client.embed, batch, model=VOYAGE_MODEL, input_type=input_type
        )
        all_embeddings.extend(result.embeddings)
        try:
            # Voyage's Python SDK doesn't return per-call token usage, so this
            # is a rough ~4-chars-per-token estimate, good enough for the
            # admin cost dashboard (Voyage is already the cheapest line item
            # by a wide margin -- see the delivered pricing roadmap).
            approx_tokens = sum(len(t) for t in batch) // 4
            cost_ledger.record_voyage_tokens("book_indexing", approx_tokens)
        except Exception:
            logger.exception("Cost ledger logging failed (non-fatal)")
    return all_embeddings


def _index_paths(book_id: str) -> tuple[str, str]:
    os.makedirs(QA_INDEX_DIR, exist_ok=True)
    meta_path = os.path.join(QA_INDEX_DIR, f"{book_id}_meta.json")
    vectors_path = os.path.join(QA_INDEX_DIR, f"{book_id}_vectors.npy")
    return meta_path, vectors_path


async def build_index(pdf_path: str, book_id: str, title: str, progress_cb=None) -> int:
    """
    Full pipeline: extract -> chunk -> embed -> save to disk.
    progress_cb, if given, is an async callable(str) called with status
    updates (useful for editing a Telegram message during a long index build).
    Returns the number of chunks indexed.
    """
    if progress_cb:
        await progress_cb("Reading pages...")
    try:
        page_texts = await asyncio.to_thread(extract_full_page_text, pdf_path)
    except Exception as e:
        raise IndexingError(f"Couldn't read this PDF's text: {e}")

    chunks = build_chunks(page_texts)
    if not chunks:
        raise IndexingError("No extractable text found in this PDF (it may be scanned images without OCR).")

    if progress_cb:
        await progress_cb(f"Embedding {len(chunks)} passages ({VOYAGE_MODEL})...")
    try:
        embeddings = await embed_texts([c["text"] for c in chunks], input_type="document")
    except Exception as e:
        raise IndexingError(f"Voyage AI embedding request failed: {e}")

    vectors = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0  # guard against a degenerate all-zero embedding causing a NaN/inf vector
    # Normalize once at index time so search is a plain dot product (cosine similarity)
    vectors /= norms

    meta_path, vectors_path = _index_paths(book_id)
    with open(meta_path, "w") as f:
        json.dump({"title": title, "chunks": chunks}, f)
    np.save(vectors_path, vectors)

    if progress_cb:
        await progress_cb("Done.")
    return len(chunks)


def delete_index(book_id: str) -> None:
    """Best-effort removal of a book's Q&A index files, if any exist (e.g. when the book itself is deleted)."""
    meta_path, vectors_path = _index_paths(book_id)
    for path in (meta_path, vectors_path):
        try:
            os.remove(path)
        except OSError:
            pass


def load_index(book_id: str) -> tuple[dict, np.ndarray] | None:
    meta_path, vectors_path = _index_paths(book_id)
    if not (os.path.exists(meta_path) and os.path.exists(vectors_path)):
        return None
    with open(meta_path) as f:
        meta = json.load(f)
    vectors = np.load(vectors_path)
    return meta, vectors


async def search_index(meta: dict, vectors: np.ndarray, question: str, top_k: int = QA_TOP_K) -> list[dict]:
    """Return the top_k most relevant chunks for `question`, each with a similarity score."""
    [query_embedding] = await embed_texts([question], input_type="query")
    query_vec = np.array(query_embedding, dtype=np.float32)
    norm = np.linalg.norm(query_vec)
    if norm > 0:
        query_vec /= norm

    scores = vectors @ query_vec  # cosine similarity, since both sides are pre-normalized
    top_k = min(top_k, len(scores))
    top_indices = np.argsort(scores)[::-1][:top_k]

    results = []
    for idx in top_indices:
        chunk = meta["chunks"][idx]
        results.append({"text": chunk["text"], "page": chunk["page"], "score": float(scores[idx])})
    return results


MAX_HISTORY_TURNS = 6  # older turns are dropped rather than growing the prompt without bound


async def answer_question(
    book_id: str, question: str, history: list[dict] | None = None, language: str = "English"
) -> dict:
    """
    Full Q&A flow for a previously-indexed book: retrieve relevant passages,
    ask Claude to answer using ONLY those passages, with numbered inline
    citations tied to the returned `sources` list.

    history, if given, is prior turns for THIS same book/session as
    [{"question": str, "answer": str}, ...], oldest first. When present,
    it's replayed as real multi-turn messages to Claude so the answer can
    flow naturally from earlier turns ("continuous" mode in the mini app)
    instead of treating every question in isolation. When omitted/empty,
    behavior is identical to the original single-shot Q&A.

    Returns {"answer": str, "sources": [{"n": int, "page": int, "text": str}, ...]}
    where `answer` contains bracketed citation markers like "[1]" that refer
    to `sources` by their "n" field (NOT by page number, since one page can
    back multiple excerpts and one excerpt is only ever excerpt N once).
    Raises IndexingError if the book hasn't been indexed.
    """
    loaded = load_index(book_id)
    if loaded is None:
        raise IndexingError("This book hasn't been indexed yet. Process it and tap 'Make searchable' first.")
    meta, vectors = loaded

    if not question.strip():
        raise IndexingError("Please send your question as text.")

    matches = await search_index(meta, vectors, question)
    if not matches:
        raise IndexingError("This book's index is empty -- try re-indexing it.")

    numbered = list(enumerate(matches, start=1))
    context = "\n\n".join(f"[{n}] (Page {m['page']})\n{m['text']}" for n, m in numbered)

    history = (history or [])[-MAX_HISTORY_TURNS:]

    system_prompt = (
        f"You are answering questions about the book '{meta['title']}' using ONLY the numbered "
        "excerpts below. Follow these rules strictly:\n"
        "1. Answer using only information in the excerpts -- do not use outside knowledge, even if "
        "you know the topic.\n"
        "2. Every factual claim must end with a bracketed citation number matching the excerpt it "
        "came from, like this: \"The heart has four chambers [1].\" If multiple excerpts support one "
        "claim, cite all of them: \"...four chambers [1][2].\" Cite the excerpt NUMBER shown below, "
        "never the page number.\n"
        "3. If the excerpts don't contain enough information to answer, say so clearly instead of "
        "guessing.\n"
        "4. Be concise and direct -- this will be read on a phone screen.\n"
        + (
            "5. This is one turn in an ongoing conversation about this book -- answer the CURRENT "
            "question so it flows naturally from the earlier turns shown (don't re-explain things "
            "already covered), but the citation numbers below apply ONLY to the current question's "
            "excerpts. Do not reuse or reference citation numbers from earlier turns.\n"
            if history else ""
        )
        + f"6. Respond in {language}.\n"
        + f"\nEXCERPTS:\n{context}"
    )

    messages = []
    for turn in history:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})
    messages.append({"role": "user", "content": question})

    try:
        response = await asyncio.to_thread(
            claude_client.messages.create,
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=system_prompt,
            messages=messages,
        )
    except Exception as e:
        raise IndexingError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response("ask_ai", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    answer_text = "".join(block.text for block in response.content if block.type == "text").strip()

    return {
        "answer": answer_text,
        "sources": [{"n": n, "page": m["page"], "text": m["text"][:150]} for n, m in numbered],
    }

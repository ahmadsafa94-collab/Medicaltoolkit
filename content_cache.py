"""
Work already paid for, keyed by the exact bytes of a PDF.

The same textbook gets uploaded again and again -- by different users, or
by one user who deleted and re-added it. Two expensive steps depend only
on the file's content, never on who uploaded it:

  * chapter detection (an AI pass over every page -- Anthropic tokens), and
  * the "Ask my book" search index (every passage embedded -- Voyage tokens).

Both are stored here under the file's SHA-256 the first time they run, and
reused whenever a byte-identical file shows up again. A file that differs
by a single byte (a different edition, a re-scan, an annotated copy) hashes
differently and is processed normally, so a reused result can never belong
to a different book.

Reuse across users is safe: the cached data is derived only from the file,
and anyone who can present the identical file already has its full content.

Index files are hard-linked into each book's own index paths rather than
copied, so a textbook indexed by 50 users takes the disk space of one.
The cache is kept when users delete their copies -- that is the point: the
next upload of the same book is still free.
"""

import hashlib
import json
import logging
import os
import shutil
import tempfile

from config import STORAGE_DIR, VOYAGE_MODEL

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(STORAGE_DIR, "_content_cache")
_HASH_CHUNK = 4 * 1024 * 1024


def file_sha256(path: str) -> str:
    """Synchronous; ~1-2 s for a 500 MB file. Call via asyncio.to_thread from async code."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(_HASH_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _entry_dir(digest: str) -> str:
    if not (len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
        raise ValueError("not a sha256 hex digest")
    return os.path.join(CACHE_DIR, digest[:2], digest)


def _atomic_json(path: str, data) -> None:
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


def _link_or_copy(src: str, dst: str) -> None:
    """Hard-link src to dst (atomically replacing dst); copy if linking isn't possible."""
    tmp = f"{dst}.{os.getpid()}.link.tmp"
    try:
        os.remove(tmp)
    except OSError:
        pass
    try:
        os.link(src, tmp)
    except OSError:
        shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


# --- chapter detection -------------------------------------------------

def get_chapters(digest: str) -> list[dict] | None:
    try:
        with open(os.path.join(_entry_dir(digest), "chapters.json"), encoding="utf-8") as f:
            chapters = json.load(f)
        return chapters if isinstance(chapters, list) and chapters else None
    except (OSError, ValueError):
        return None


def put_chapters(digest: str, chapters: list[dict]) -> None:
    try:
        _atomic_json(os.path.join(_entry_dir(digest), "chapters.json"), chapters)
    except Exception:
        logger.exception("Couldn't cache chapters for %s (non-fatal)", digest[:12])


# --- Q&A index ---------------------------------------------------------

def _index_files(digest: str) -> tuple[str, str]:
    # Keyed by model as well as content: an index from another embedding
    # model is useless for questions embedded by the current one.
    d = os.path.join(_entry_dir(digest), VOYAGE_MODEL)
    return os.path.join(d, "qa_meta.json"), os.path.join(d, "qa_vectors.npy")


def restore_index(digest: str, meta_path: str, vectors_path: str) -> bool:
    """Give a book the cached index for this content. False if there is none."""
    c_meta, c_vec = _index_files(digest)
    if not (os.path.exists(c_meta) and os.path.exists(c_vec)):
        return False
    try:
        # Vectors first, meta second: the same order pdf_qa writes them, so a
        # reader never pairs new meta with old vectors (load_index also
        # rejects a count mismatch).
        _link_or_copy(c_vec, vectors_path)
        _link_or_copy(c_meta, meta_path)
        return True
    except Exception:
        logger.exception("Couldn't restore cached index %s (will rebuild)", digest[:12])
        return False


def store_index(digest: str, meta_path: str, vectors_path: str) -> None:
    """Remember a freshly built index for this content."""
    c_meta, c_vec = _index_files(digest)
    try:
        os.makedirs(os.path.dirname(c_meta), exist_ok=True)
        _link_or_copy(vectors_path, c_vec)
        _link_or_copy(meta_path, c_meta)
    except Exception:
        logger.exception("Couldn't cache index %s (non-fatal)", digest[:12])

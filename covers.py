"""
Book covers: page 1 of the PDF, rendered to a JPEG and cached on disk.

Two sizes: "thumb" for the small icon next to a title, and "full" for the
tap-to-enlarge view. Rendering a page takes a fraction of a second with
pdfium (already installed with pdfplumber); after that every request is a
file read.

Only the first page ever leaves the server this way -- for a shared
specialty book that is the one page users can see, as on a shop shelf.
"""

import book_store
import logging
import os
import tempfile
import threading

import pypdfium2 as pdfium

from config import STORAGE_DIR

logger = logging.getLogger(__name__)

COVERS_DIR = os.path.join(STORAGE_DIR, "_covers")
_WIDTHS = {"thumb": 120, "full": 1000}   # px; thumb is shown ~40 px wide on 3x screens
# Height caps too: scaling by width alone let a tall, thin page (a 20 x
# 14400 pt MediaBox) render to a multi-gigabyte bitmap and kill the process.
_MAX_HEIGHTS = {"thumb": 240, "full": 1600}
_QUALITY = {"thumb": 75, "full": 85}
_renders = threading.BoundedSemaphore(2)  # at most two cover renders at once


class CoverError(Exception):
    pass


def _path(book_id: str, size: str) -> str:
    if not book_id.isalnum():
        raise CoverError("bad book id")
    return os.path.join(COVERS_DIR, f"{book_id}_{size}.jpg")


def get_cover(book_id: str, pdf_path: str, size: str = "thumb") -> str:
    """Path of the cached JPEG, rendering it first if needed. Synchronous -- call via asyncio.to_thread."""
    if size not in _WIDTHS:
        raise CoverError("bad size")
    out = _path(book_id, size)
    if os.path.exists(out):
        return out
    failed = out + ".failed"
    if os.path.exists(failed):  # don't re-render a cover that already failed on every request
        raise CoverError("This book's cover couldn't be rendered.")
    os.makedirs(COVERS_DIR, exist_ok=True)
    with _renders:
        if os.path.exists(out):  # rendered by another request while this one waited
            return out
        try:
            pdf = pdfium.PdfDocument(book_store.ensure_local(pdf_path))
            try:
                page = pdf[0]
                width, height = page.get_size()
                scale = min(_WIDTHS[size] / max(width, 1), _MAX_HEIGHTS[size] / max(height, 1))
                image = page.render(scale=scale).to_pil().convert("RGB")
                page.close()
            finally:
                pdf.close()
            fd, tmp = tempfile.mkstemp(dir=COVERS_DIR, suffix=".jpg")
            try:
                with os.fdopen(fd, "wb") as f:
                    image.save(f, "JPEG", quality=_QUALITY[size], optimize=True)
                os.replace(tmp, out)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning("Couldn't render cover for %s: %s", book_id, e)
            try:
                open(failed, "w").close()
            except OSError:
                pass
            raise CoverError("This book's cover couldn't be rendered.")
    return out


def delete_covers(book_id: str) -> None:
    for size in _WIDTHS:
        for suffix in ("", ".failed"):
            try:
                os.remove(_path(book_id, size) + suffix)
            except (OSError, CoverError):
                pass

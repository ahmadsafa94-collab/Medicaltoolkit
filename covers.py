"""
Book covers: page 1 of the PDF, rendered to a JPEG and cached on disk.

Two sizes: "thumb" for the small icon next to a title, and "full" for the
tap-to-enlarge view. Rendering a page takes a fraction of a second with
pdfium (already installed with pdfplumber); after that every request is a
file read.

Only the first page ever leaves the server this way -- for a shared
specialty book that is the one page users can see, as on a shop shelf.
"""

import logging
import os
import tempfile

import pypdfium2 as pdfium

from config import STORAGE_DIR

logger = logging.getLogger(__name__)

COVERS_DIR = os.path.join(STORAGE_DIR, "_covers")
_WIDTHS = {"thumb": 120, "full": 1000}   # px; thumb is shown ~40 px wide on 3x screens
_QUALITY = {"thumb": 75, "full": 85}


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
    os.makedirs(COVERS_DIR, exist_ok=True)
    try:
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            page = pdf[0]
            width, _height = page.get_size()
            scale = _WIDTHS[size] / max(width, 1)
            image = page.render(scale=scale).to_pil().convert("RGB")
            page.close()
        finally:
            pdf.close()
    except Exception as e:
        logger.warning("Couldn't render cover for %s: %s", book_id, e)
        raise CoverError("This book's cover couldn't be rendered.")
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
    return out


def delete_covers(book_id: str) -> None:
    for size in _WIDTHS:
        try:
            os.remove(_path(book_id, size))
        except (OSError, CoverError):
            pass

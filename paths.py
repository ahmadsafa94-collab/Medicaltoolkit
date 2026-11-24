"""
Small path helpers shared between bot.py (the chat side) and webapp_api.py
(the Book Shelf mini app's backend). Pulled out on their own specifically to
avoid a circular import: bot.py imports webapp_api.py (to run its server
alongside the polling loop), so webapp_api.py can't import these back out of
bot.py.
"""

import os
import re

from config import STORAGE_DIR


def user_dir(user_id: int) -> str:
    path = os.path.join(STORAGE_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


def safe_pdf_filename(raw_name: str | None) -> str:
    """
    Turn a client-supplied filename into something safe to join onto a
    server-side path. Both the chat upload (Telegram's doc.file_name) and
    the mini app's uploader (a plain HTML file input) hand us client-supplied
    strings that could contain path-traversal sequences (e.g.
    "../../etc/whatever.pdf") -- strip any directory components and keep
    only a safe character set.
    """
    name = os.path.basename(raw_name or "")
    name = re.sub(r"[^\w.-]", "_", name).strip(". ") or "upload"
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    # Filesystems limit names in BYTES (255): a 148-character Persian name is
    # 284 bytes and failed to save. The stem is cut to 180 bytes -- room for
    # unique_path's " (2)" -- and .pdf always survives the cut.
    stem = stem.encode("utf-8")[:180].decode("utf-8", "ignore").strip(". ") or "upload"
    return stem + ".pdf"


def unique_path(path: str) -> str:
    """
    If `path` already exists, append " (2)", " (3)", etc. before the
    extension until a name that doesn't collide is found -- used when
    landing an uploaded PDF in a user's library folder so a same-named
    re-upload never silently overwrites an earlier, still-relevant book.
    """
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while True:
        candidate = f"{base} ({i}){ext}"
        if not os.path.exists(candidate):
            return candidate
        i += 1

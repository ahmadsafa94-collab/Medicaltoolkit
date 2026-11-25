"""
Backend API for the Book Shelf Telegram Mini App (webapp/ static files).

Built on Starlette directly (the ASGI toolkit FastAPI itself wraps) rather
than FastAPI -- this keeps the dependency list to starlette + uvicorn +
python-multipart, all pure-Python/well-established packages, and every
request/response here is small and explicit enough that FastAPI's
pydantic-request-model sugar wouldn't save much. Validation is done by hand,
the same explicit style already used throughout this codebase (see
pdf_processor.detect_chapters, quiz_ai.generate_quiz).

Runs as an ASGI app inside the SAME process as the chat bot (see bot.py's
main()), sharing one asyncio event loop and one filesystem -- there is no
separate deployment or shared-storage problem to solve, since this is just
another coroutine running alongside dp.start_polling(bot).

Every endpoint authenticates via webapp_auth.validate_init_data() using the
"X-Telegram-Init-Data" header the frontend attaches to every request (see
webapp/app.js's api() helper) -- there is no other session mechanism, and a
request without a valid, freshly-signed initData is always rejected before
touching any user's books.

Long-running AI actions (chapter division, Q&A indexing, summarizing, quiz
generation) run as background asyncio tasks tracked in an in-memory job
table, polled via GET /api/books/{book_id}/jobs/{job_type} -- the mini
app's fetch calls that kick these off return immediately rather than
holding one HTTP request open for however long a multi-minute job takes.
"""

import asyncio
import contextvars
import logging
import os
import tempfile
import json
import re
import shutil
import time
import uuid

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import anki_export
import ask_sessions
import book_requests
import chapter_ai
import covers
import flashcards
import library
import notes
import pdf_export
import accounts
import reader_tools
import recommended_books
import pdf_qa
import quiz_ai
import shared_library
import subscriptions
import drug_lookup
import drug_qa
import interaction_ai
import pubmed
import web_sources
import lexidrug
import name_resolver
from bot_instance import bot as tg_bot
from chapter_flow import build_chapter_ai_kb
from config import (
    ADMIN_USER_IDS,
    ANTHROPIC_API_KEY,  # noqa: F401 -- imported so a missing key fails fast at startup, same as bot.py
    ASK_TIMEOUT_SECONDS,
    BOOK_SUMMARY_TIMEOUT_SECONDS,
    CHAPTER_DIVISION_TIMEOUT_SECONDS,
    CHAPTER_SUMMARY_TIMEOUT_SECONDS,
    FLASHCARD_GENERATION_TIMEOUT_SECONDS,
    MAX_BOOK_PAGES,
    MAX_SHELF_UPLOAD_BYTES,
    MAX_SHELF_UPLOAD_PAGES,
    QA_INDEXING_TIMEOUT_SECONDS,
    scaled_timeout,
    QUIZ_GENERATION_TIMEOUT_SECONDS,
    SEND_CHAPTER_FILES_TIMEOUT_SECONDS,
    STORAGE_DIR,
)
from paths import safe_pdf_filename, unique_path, user_dir
from pdf_processor import (
    ChapterDetectionError,
    compute_chapter_ranges,
    open_check,
    detect_chapters_cached,
    split_pdf_by_chapters,
)
from telegram_helpers import send_documents_by_chat_id
from webapp_auth import AuthError, validate_init_data

logger = logging.getLogger(__name__)


class ApiError(HTTPException):
    """Same as Starlette's HTTPException, just spelled locally for readability at call sites."""


async def _api_error_handler(request: Request, exc: HTTPException):
    request.state.error_detail = exc.detail  # for the account activity log
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

# The signed-in user of the request being handled. Jobs started from it
# (asyncio tasks and to_thread calls copy the context) see the same value,
# which is what scopes a specialty book's jobs to the user who started them.
_request_user_id: contextvars.ContextVar = contextvars.ContextVar("request_user_id", default=None)


def require_user(request: Request) -> dict:
    init_data = request.headers.get("x-telegram-init-data", "")
    try:
        user = validate_init_data(init_data)
    except AuthError as e:
        raise ApiError(status_code=401, detail=str(e))
    # Same gate as the bot's _block_gate middleware: a blocked user must not
    # keep using the mini app (which calls the same paid AI features).
    if not subscriptions.is_admin(user["id"]) and subscriptions.is_blocked(user["id"]):
        raise ApiError(status_code=403, detail="Your access to this bot has been suspended.")
    _request_user_id.set(user["id"])
    request.state.user_id = user["id"]  # for the account activity log (AccountLogMiddleware)
    return user


# ---------------------------------------------------------------------------
# In-memory background job tracking (single process -- see module docstring)
# ---------------------------------------------------------------------------

_jobs: dict[tuple, dict] = {}
_JOB_KEEP_SECONDS = 3600          # a finished job's result is dropped after this
_TIMEOUT_COOLDOWN_SECONDS = 600   # see start_job


def _job_key(book_id: str, job_type: str) -> tuple:
    """
    One job per book and type -- except on a specialty book, which many
    users share: there every user's summary/quiz/flashcards job is their
    own (they used to see, and even act on, each other's results). Only
    indexing a specialty book is one job for everyone.
    """
    if job_type != "index" and shared_library.is_shared_id(book_id):
        return (book_id, job_type, _request_user_id.get())
    return (book_id, job_type)


def _prune_jobs() -> None:
    now = time.time()
    for key, job in list(_jobs.items()):
        if job.get("status") != "running" and now - job.get("finished_at", now) > _JOB_KEEP_SECONDS:
            _jobs.pop(key, None)


def forget_jobs(book_id: str) -> None:
    """Drop every finished job of a deleted book (a running one finishes and expires on its own)."""
    for key in [k for k, j in _jobs.items() if k[0] == book_id and j.get("status") != "running"]:
        _jobs.pop(key, None)


def start_job(book_id: str, job_type: str, coro) -> bool:
    """
    Run `coro` (an awaitable, not yet started) as a background task, tracked
    in _jobs so the frontend can poll instead of holding a connection open.
    Returns False without starting anything if a job of this type is
    already running for this book (the frontend should disable the
    triggering button while status == "running").
    """
    _prune_jobs()
    key = _job_key(book_id, job_type)
    previous = _jobs.get(key, {})
    if previous.get("status") == "running":
        coro.close()
        return False
    # A timed-out job's worker thread can't be stopped and keeps running in
    # the background; starting the same job again at once would stack them
    # (each still making its AI call, uncharged after the refund).
    if previous.get("timed_out") and time.time() - previous.get("finished_at", 0) < _TIMEOUT_COOLDOWN_SECONDS:
        coro.close()
        raise ApiError(
            status_code=429,
            detail="The last attempt is still finishing in the background. Please try again in a few minutes.",
        )
    _jobs[key] = {"status": "running", "result": None, "error": None, "progress": None, "started_at": time.time()}

    async def runner():
        try:
            result = await coro
            _jobs[key] = {"status": "done", "result": result, "error": None, "progress": None, "finished_at": time.time()}
        except Exception as e:
            logger.exception("Background job %s failed", key)
            _jobs[key] = {
                "status": "error", "result": None, "error": _job_error_text(e), "progress": None,
                "finished_at": time.time(), "timed_out": isinstance(e, asyncio.TimeoutError),
            }

    # Held in a set: asyncio keeps only a weak reference to a bare
    # create_task(), so an unreferenced job could be garbage-collected
    # mid-run and simply vanish, leaving the job "running" forever.
    task = asyncio.create_task(runner())
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)
    return True


_running_tasks: set = set()


def _job_error_text(e: Exception) -> str:
    """
    What a failed job shows the user. The errors this module raises on
    purpose (RuntimeError / ChapterDetectionError with a written message)
    pass through; anything else is an unexpected crash whose text -- a
    Python or API error string -- means nothing to a student, so it is
    logged (above) and replaced.
    """
    if isinstance(e, asyncio.TimeoutError):
        return "That took too long and was stopped. Nothing was used from your allowance -- please try again."
    if isinstance(e, (RuntimeError, ChapterDetectionError)) and str(e):
        return str(e)
    return "Something went wrong on our side. Nothing was used from your allowance -- please try again."


def start_metered_job(book_id: str, job_type: str, coro, user_id: int, feature: str, units: int = 1) -> bool:
    """
    start_job() for a job that spends the user's daily allowance. The units
    are taken up front (so a free user out of allowance gets a 402 now, not
    a failed job later) and given back if the job fails, times out, or was
    never started because the same job was already running -- a failed or
    refused request must not cost anything.
    """
    try:
        subscriptions.consume(user_id, feature, units)
    except subscriptions.QuotaExceeded as e:
        coro.close()
        raise ApiError(status_code=402, detail=str(e))

    async def refunding():
        try:
            return await coro
        except BaseException:
            subscriptions.refund(user_id, feature, units)
            raise

    wrapper = refunding()
    try:
        started = start_job(book_id, job_type, wrapper)
    except ApiError:
        coro.close()
        subscriptions.refund(user_id, feature, units)
        raise
    if not started:
        coro.close()
        subscriptions.refund(user_id, feature, units)
    return started


def _valid_chapter_indices(raw, chapters: list) -> list[int]:
    """Whole numbers within the book's chapters, each once, in order (no -1, True, or 20,000 duplicates)."""
    if not isinstance(raw, list) or not raw:
        raise ApiError(status_code=400, detail="Select at least one chapter.")
    out = []
    for i in raw:
        if type(i) is not int or not (0 <= i < len(chapters)):
            raise ApiError(status_code=400, detail="Invalid chapter_indices.")
        if i not in out:
            out.append(i)
    return out


def set_job_progress(book_id: str, job_type: str, progress) -> None:
    key = _job_key(book_id, job_type)
    if _jobs.get(key, {}).get("status") == "running":  # not a straggler from an older run
        _jobs[key]["progress"] = progress


def get_job(book_id: str, job_type: str) -> dict | None:
    return _jobs.get(_job_key(book_id, job_type))


# ---------------------------------------------------------------------------
# Books: list / upload / detail / file
# ---------------------------------------------------------------------------

def _book_or_404(user_id: int, book_id: str) -> dict:
    """
    A book this user may use: one of their own, or a specialty-library book.
    A specialty book is served through the user's reference to it (created
    on first use, refreshed from the shared copy every time), so every
    per-book endpoint below works on it unchanged.
    """
    if shared_library.is_shared_id(book_id):
        shared = shared_library.get_book(book_id)
        if shared is None:
            # Removed from the library by the admin: drop the stale
            # reference (never the file -- it's already gone).
            library.remove_book(user_id, book_id, delete_file=False)
            raise ApiError(status_code=404, detail="This book was removed from the library.")
        return library.upsert_shared_ref(user_id, book_id, shared)
    book = library.get_book(user_id, book_id)
    if book is None:
        raise ApiError(status_code=404, detail="Book not found.")
    return book


def _public_book(book_id: str, book: dict) -> dict:
    """Strip the server-side pdf_path out of anything sent to the frontend."""
    out = {k: v for k, v in book.items() if k != "pdf_path"}
    out["book_id"] = book_id
    return out


async def list_books(request: Request):
    user = require_user(request)
    # Specialty books have their own shelf tab; ?all=1 (the Bookmarks list)
    # includes them, since a bookmark in one should be findable too.
    all_books = request.query_params.get("all") == "1"
    books = library.list_books(user["id"]) if all_books else library.own_books(user["id"])
    # shelf_limit/shelf_used let the mini app show the upgrade prompt on the
    # "+" button instead of letting a free user pick a file, upload it, and
    # only then be refused. null limit = unlimited (Premium). shelf_used is
    # sent rather than letting the client count books itself, since paid
    # 📚 Requested Books don't count toward the cap and that rule belongs in
    # one place (library.count_toward_shelf_limit). Enforcement still lives
    # in upload_book() -- this is only what the UI needs to be polite.
    return JSONResponse(
        {
            "books": [_public_book(bid, b) for bid, b in books.items()],
            "shelf_limit": subscriptions.shelf_limit(user["id"]),
            "shelf_used": library.count_toward_shelf_limit(user["id"]),
            # Sent so the client's own pre-upload check can never drift
            # from the server's (it used to hardcode 200MB).
            "max_upload_mb": MAX_SHELF_UPLOAD_BYTES // 1024 // 1024,
        }
    )


async def get_book(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    return JSONResponse(_public_book(book_id, book))


async def rename_book(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    if library.is_shared_ref(_book_or_404(user["id"], book_id)):
        raise ApiError(status_code=403, detail="Specialty-library books are named by the admin.")

    body = await _json_body(request)

    new_title = _text(body, "title", 300)
    if not new_title:
        raise ApiError(status_code=400, detail="Title can't be empty.")
    if len(new_title) > 200:
        raise ApiError(status_code=400, detail="Title is too long (200 characters max).")

    library.rename_book(user["id"], book_id, new_title)
    return JSONResponse(_public_book(book_id, library.get_book(user["id"], book_id)))


_VALID_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


async def set_cover_color(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)

    body = await _json_body(request)

    color = body.get("color")
    if color is not None and not _VALID_HEX_COLOR.match(color):
        raise ApiError(status_code=400, detail="Color must be a hex value like '#8a5a3c', or null to reset.")

    library.set_cover_color(user["id"], book_id, color)
    return JSONResponse(_public_book(book_id, library.get_book(user["id"], book_id)))


async def delete_book(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    if library.is_shared_ref(book):
        # Only this user's reference (and their notes/history) goes; the
        # file, index and cover belong to the specialty library.
        library.remove_book(user["id"], book_id, delete_file=False)
        _delete_user_book_data(user["id"], book_id)
        return JSONResponse({"deleted": True, "book_id": book_id})

    library.remove_book(user["id"], book_id, delete_file=True)
    pdf_qa.delete_index(book_id)
    covers.delete_covers(book_id)
    _delete_user_book_data(user["id"], book_id)
    forget_jobs(book_id)
    return JSONResponse({"deleted": True, "book_id": book_id})


def _delete_user_book_data(user_id: int, book_id: str) -> None:
    """Everything of this user's that hangs off the book -- notes and flashcards too, which used to stay behind."""
    library.delete_qa_sessions_for_book(user_id, book_id)
    library.delete_quiz_attempts_for_book(user_id, book_id)
    notes.delete_notes_for_book(user_id, book_id)
    reader_tools.delete_highlights_for_book(user_id, book_id)
    flashcards.delete_deck(user_id, book_id)


async def set_bookmark(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    body = await _json_body(request)

    page = body.get("page")
    if page is not None:
        if not isinstance(page, int) or isinstance(page, bool) or not (1 <= page <= book["page_count"]):
            raise ApiError(status_code=400, detail="Invalid page number.")

    library.set_bookmark(user["id"], book_id, page)
    return JSONResponse(_public_book(book_id, library.get_book(user["id"], book_id)))


def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


async def upload_book(request: Request):
    user = require_user(request)
    user_id = user["id"]

    # Free-plan shelf cap, before a byte of the body is read: app.js also
    # checks this before opening the file picker, but that's a convenience,
    # not the enforcement -- the client can't be trusted, and anyone can
    # POST here directly. The slot is reserved for the whole upload, so
    # several uploads started together can't all pass a check none of them
    # has been counted in yet.
    try:
        slot = subscriptions.shelf_slot(user_id, library.count_toward_shelf_limit(user_id))
        slot.__enter__()
    except subscriptions.ShelfLimitReached as e:
        raise ApiError(status_code=402, detail=str(e))
    try:
        return await _receive_upload(request, user_id)
    finally:
        slot.__exit__(None, None, None)


async def _receive_upload(request: Request, user_id: int):
    dest_path, page_count, form, filename = await _save_uploaded_pdf(
        request, os.path.join(user_dir(user_id), "library")
    )
    book_title = (form.get("title") or "").strip() or os.path.splitext(filename)[0] or "Untitled book"
    book_id = library.add_book(user_id, book_title, dest_path, page_count, source="shelf")
    return JSONResponse(_public_book(book_id, library.get_book(user_id, book_id)))


async def _save_uploaded_pdf(request: Request, dest_dir: str) -> tuple[str, int, object, str]:
    """
    Receive one multipart PDF into dest_dir, enforcing the size and page
    limits. Returns (saved path, page count, the parsed form, safe filename).
    Shared by the personal shelf upload and the admin's specialty-library
    upload so both get exactly the same checks.
    """
    # Content-Length is required, and checked before request.form() runs:
    # Starlette spools the WHOLE multipart body to a temp file inside
    # form(), so a body with no length header was written to disk in full
    # before the size loop below ever saw it. Browsers (and Telegram's
    # WebView) always send it for a FormData upload.
    limit_mb = MAX_SHELF_UPLOAD_BYTES // 1024 // 1024
    content_length = request.headers.get("content-length")
    if not (content_length and content_length.isdigit()):
        raise ApiError(status_code=411, detail="Upload rejected: missing file size. Please try again.")
    if int(content_length) > MAX_SHELF_UPLOAD_BYTES + 1_000_000:
        raise ApiError(status_code=413, detail=f"That file is over the {limit_mb}MB limit.")

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise ApiError(status_code=400, detail="No file uploaded (expected a 'file' form field).")

    os.makedirs(dest_dir, exist_ok=True)
    filename = safe_pdf_filename(upload.filename)
    dest_path = unique_path(os.path.join(dest_dir, filename))

    total = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SHELF_UPLOAD_BYTES:
                    raise ApiError(status_code=413, detail=f"That file is over the {limit_mb}MB limit.")
                # Off the event loop: at hundreds of MB, writing inline
                # would stall every other request (and the bot) meanwhile.
                await asyncio.to_thread(out.write, chunk)
    except ApiError:
        _safe_remove(dest_path)
        raise
    except Exception:
        logger.exception("Mini app upload failed while receiving the file")
        _safe_remove(dest_path)
        raise ApiError(status_code=400, detail="The upload was interrupted. Please try again.")

    if total == 0:
        _safe_remove(dest_path)
        raise ApiError(status_code=400, detail="That file was empty.")

    try:
        page_count = await asyncio.to_thread(open_check, dest_path)
    except ChapterDetectionError as e:
        _safe_remove(dest_path)
        raise ApiError(status_code=400, detail=str(e))

    if page_count > MAX_SHELF_UPLOAD_PAGES:
        _safe_remove(dest_path)
        raise ApiError(
            status_code=400,
            detail=f"This PDF has {page_count} pages, over the {MAX_SHELF_UPLOAD_PAGES}-page limit.",
        )
    return dest_path, page_count, form, filename


def _read_range(path: str, start: int, end: int):
    length = end - start + 1

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return gen()


async def get_book_file(request: Request):
    """
    Serves the raw PDF bytes for the mini app's pdf.js reader, with HTTP
    Range support -- pdf.js fetches a book page-by-page/byte-range-by-
    byte-range rather than downloading the whole file up front, which
    matters a lot for a 1500-page, 200MB book.
    """
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    path = book["pdf_path"]
    if not os.path.exists(path):
        raise ApiError(status_code=404, detail="The book file is missing on the server.")

    file_size = os.path.getsize(path)
    range_header = request.headers.get("range")

    if range_header is None:
        return StreamingResponse(
            _read_range(path, 0, file_size - 1),
            media_type="application/pdf",
            headers={"Content-Length": str(file_size), "Accept-Ranges": "bytes"},
        )

    try:
        _units, _, rng = range_header.partition("=")
        start_s, _, end_s = rng.partition("-")
        if not start_s:  # suffix range: the last N bytes
            start = max(0, file_size - int(end_s))
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s else file_size - 1
        end = min(end, file_size - 1)
    except ValueError:
        raise ApiError(status_code=416, detail="Invalid Range header.")

    if start > end or start >= file_size:
        raise ApiError(status_code=416, detail="Requested range not satisfiable.")

    return StreamingResponse(
        _read_range(path, start, end),
        status_code=206,
        media_type="application/pdf",
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
        },
    )


# ---------------------------------------------------------------------------
# Jobs (generic polling endpoint)
# ---------------------------------------------------------------------------

async def get_job_status(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    job_type = request.path_params["job_type"]
    _book_or_404(user["id"], book_id)  # 404 rather than leaking job state for someone else's book_id
    job = get_job(book_id, job_type)
    if job is None:
        return JSONResponse({"status": "none"})
    return JSONResponse(job)


# ---------------------------------------------------------------------------
# 1. Divide into chapters
# ---------------------------------------------------------------------------

async def _run_chapter_division(user_id: int, book_id: str, pdf_path: str):
    def progress(done, total):
        # Called from inside asyncio.to_thread's worker thread. Plain dict
        # item assignment is atomic under the GIL, so writing straight into
        # _jobs here (rather than round-tripping through the event loop) is
        # safe -- unlike bot.py's chat-upload flow, nothing here needs to
        # touch the Telegram API or any other coroutine-only object.
        set_job_progress(book_id, "chapters", f"Reading page {done}/{total}...")

    page_count = await asyncio.to_thread(open_check, pdf_path)
    if page_count > MAX_BOOK_PAGES:
        raise ChapterDetectionError(f"This book has {page_count} pages, over the {MAX_BOOK_PAGES}-page limit.")
    chapters = await asyncio.to_thread(detect_chapters_cached, pdf_path, progress)
    ranges = compute_chapter_ranges(chapters, page_count)
    library.set_chapters(user_id, book_id, ranges)
    # A specialty book divided by one user is divided for everyone after.
    if shared_library.is_shared_id(book_id):
        shared = shared_library.get_book(book_id)
        if shared is not None and not shared.get("chapters"):
            shared_library.set_chapters(book_id, ranges)
    return ranges


async def divide_into_chapters(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    async def job():
        library.set_chapters_pending(user["id"], book_id)
        try:
            return await asyncio.wait_for(
                _run_chapter_division(user["id"], book_id, book["pdf_path"]),
                timeout=scaled_timeout(CHAPTER_DIVISION_TIMEOUT_SECONDS, book.get("page_count") or 0),
            )
        except asyncio.TimeoutError:
            library.set_chapters_error(user["id"], book_id, "Chapter detection took too long and timed out.")
            raise
        except ChapterDetectionError as e:
            library.set_chapters_error(user["id"], book_id, str(e))
            raise
        except Exception:
            library.set_chapters_error(user["id"], book_id, "Something went wrong dividing this book. Please try again.")
            raise

    # Metered: this is a full AI pass over up to 1,000 pages, and before it
    # counted against nothing, so delete-and-re-divide was unlimited.
    started = start_metered_job(book_id, "chapters", job(), user["id"], "books")
    return JSONResponse({"started": started})


async def send_chapter_files(request: Request):
    """
    Physically splits the book into one PDF per chapter (pdf_processor's
    split_pdf_by_chapters -- the same function bot.py's chat-upload flow
    uses) and sends each one as a Telegram document straight to the user's
    chat, exactly like a chat-based PDF upload does. The Book Shelf mini
    app itself only ever kept chapter BOUNDARIES (library.set_chapters),
    never split files, since the in-app reader/summarize/quiz features all
    work fine against page ranges of the original PDF -- this endpoint is
    the one place a mini-app user gets actual standalone per-chapter files
    to keep or share, by asking for them explicitly.

    Runs as a background job (like every other multi-minute mini-app
    action) since splitting a long book AND sending many documents one at
    a time (flood-control pacing included) can take a while.
    """
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    chapters = book.get("chapters") or []
    if not chapters:
        raise ApiError(status_code=409, detail="Divide this book into chapters first.")

    async def job():
        output_dir = os.path.join(user_dir(user["id"]), "shelf_chapter_sends", book_id)
        shutil.rmtree(output_dir, ignore_errors=True)
        try:
            try:
                output_paths = await asyncio.to_thread(
                    split_pdf_by_chapters, book["pdf_path"], chapters, output_dir
                )
            except Exception:
                logger.exception("Splitting book %s into chapter files failed", book_id)
                raise RuntimeError("Couldn't split this book into chapter files. Please try again.")

            sent, total = await asyncio.wait_for(
                send_documents_by_chat_id(
                    tg_bot, user["id"], chapters, output_paths, get_reply_markup=build_chapter_ai_kb
                ),
                timeout=SEND_CHAPTER_FILES_TIMEOUT_SECONDS,
            )
            if sent == 0:
                raise RuntimeError(
                    "Couldn't send any chapter files to your chat -- make sure you've started a chat with the bot."
                )
            return {"sent": sent, "total": total}
        finally:
            # The files just delivered via Telegram serve no further purpose
            # sitting on disk -- clean them up the same way bot.py's own
            # chapter-send flow does, whether sending succeeded or not.
            shutil.rmtree(output_dir, ignore_errors=True)

    started = start_job(book_id, "send_chapters", job())
    return JSONResponse({"started": started})


# ---------------------------------------------------------------------------
# 2. Read -- the reader itself is client-side pdf.js against /file; no
#    separate endpoint needed.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 3. Summarize (whole book or one chapter)
# ---------------------------------------------------------------------------

async def summarize(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    chapters = book.get("chapters") or []

    body = await _json_body(request)

    scope = body.get("scope")
    if scope not in ("book", "chapter"):
        raise ApiError(status_code=400, detail="scope must be 'book' or 'chapter'.")
    if not chapters:
        raise ApiError(status_code=409, detail="Divide this book into chapters first.")

    if scope == "chapter":
        chapter_index = body.get("chapter_index")
        if not isinstance(chapter_index, int) or isinstance(chapter_index, bool) or not (0 <= chapter_index < len(chapters)):
            raise ApiError(status_code=400, detail="Invalid chapter_index.")
        chapter = chapters[chapter_index]

        async def job():
            try:
                # Extracted with MAX_CHARS_HARD_CAP (not the smaller
                # MAX_CHARS_PER_CHAPTER) and summarized via
                # summarize_chapter_full's map-reduce -- this is what makes a
                # long chapter's summary actually cover the WHOLE chapter
                # instead of only its first ~60,000 characters.
                text, hard_truncated = await asyncio.to_thread(
                    chapter_ai.extract_text_for_page_range,
                    book["pdf_path"],
                    chapter["start_page"],
                    chapter["end_page"],
                    chapter_ai.MAX_CHARS_HARD_CAP,
                )
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        chapter_ai.summarize_chapter_full,
                        chapter["title"],
                        text,
                        hard_truncated,
                        subscriptions.get_language(user["id"]),
                    ),
                    timeout=CHAPTER_SUMMARY_TIMEOUT_SECONDS,
                )
            except chapter_ai.ChapterAIError as e:
                raise RuntimeError(str(e))

        job_type = f"summary:chapter:{chapter_index}"
    else:
        async def job():
            def progress(done, total):
                set_job_progress(book_id, "summary:book", f"Summarizing chapter {done}/{total}...")

            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        chapter_ai.summarize_whole_book,
                        book["title"],
                        book["pdf_path"],
                        chapters,
                        progress,
                        subscriptions.get_language(user["id"]),
                        time.monotonic() + BOOK_SUMMARY_TIMEOUT_SECONDS,
                    ),
                    timeout=BOOK_SUMMARY_TIMEOUT_SECONDS,
                )
            except chapter_ai.ChapterAIError as e:
                raise RuntimeError(str(e))

        job_type = "summary:book"

    # A whole-book summary is one AI pass per chapter (often 30-80 calls),
    # so it costs one unit per chapter rather than one in total -- before,
    # it was the cheapest way to spend the most.
    units = min(len(chapters), chapter_ai.MAX_CHAPTERS_FOR_WHOLE_BOOK_SUMMARY) if scope == "book" else 1
    started = start_metered_job(book_id, job_type, job(), user["id"], "summaries", units)
    return JSONResponse({"started": started, "job_type": job_type})


async def export_summary_pdf(request: Request):
    """
    Renders an already-generated summary (whole-book or single-chapter) as
    a PDF and sends it as a Telegram document to the user's own chat with
    the bot, for the "📄 Export as PDF" button under the summary in the
    mini app's Summarize panel. Same reasoning and pattern as
    export_answer_pdf (Ask AI's own PDF export): the frontend already has
    the exact summary text on screen, and Telegram's in-app WebView has no
    reliable way to save a browser-triggered download, so the bot delivers
    it instead.
    """
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    body = await _json_body(request)

    text = _text(body, "text", 400000)
    title = _text(body, "title", 300) or book.get("title") or "Summary"
    if not text:
        raise ApiError(status_code=400, detail="Nothing to export -- generate a summary first.")

    try:
        pdf_bytes = await asyncio.to_thread(pdf_export.generate_text_pdf, title, text)
    except Exception:
        logger.exception("Summary PDF export failed for book_id=%s", book_id)
        raise ApiError(status_code=500, detail="Couldn't build the PDF. Please try again.")

    filename = safe_pdf_filename(f"{title} - Summary")
    try:
        await tg_bot.send_document(
            chat_id=user["id"],
            document=BufferedInputFile(pdf_bytes, filename=filename),
            caption=f"📝 Summary -- {title}",
        )
    except TelegramAPIError:
        logger.exception("Failed to send summary export PDF to chat_id=%s", user["id"])
        raise ApiError(
            status_code=502,
            detail="Couldn't send that to your Telegram chat. Make sure you've started a chat with the bot, then try again.",
        )

    return JSONResponse({"sent": True})


# ---------------------------------------------------------------------------
# 4. Ask questions using AI (reuses the pdf_qa module built for /ask in chat)
# ---------------------------------------------------------------------------

def _async_progress(sync_progress):
    async def _cb(text):
        sync_progress(text)
    return _cb


async def index_book(request: Request):
    """Kicks off Q&A indexing (Voyage embeddings) for this book, as a background job."""
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    if library.is_shared_ref(book):
        # A specialty book's index is the library's, built once for everyone.
        # Never rebuild it per user; report its state through the same job
        # entry the client polls.
        if book.get("qa_indexed"):
            _jobs[_job_key(book_id, "index")] = {
                "status": "done", "result": {"num_chunks": book.get("num_chunks", 0)}, "error": None, "progress": None,
            }
            return JSONResponse({"started": True})
        if (get_job(book_id, "index") or {}).get("status") == "running":
            return JSONResponse({"started": True})
        shared = shared_library.get_book(book_id) or {}
        if shared.get("index_error") and not subscriptions.is_admin(user["id"]):
            # A failed full-book indexing run is the admin's to retry; any
            # user re-triggering it was an unmetered, repeatable cost.
            raise ApiError(status_code=409, detail="This book is still being prepared by the admin. Please try again later.")
        return JSONResponse({"started": _start_shared_index(book_id)})

    async def job():
        def progress(text):
            set_job_progress(book_id, "index", text)

        try:
            num_chunks = await asyncio.wait_for(
                pdf_qa.build_index(book["pdf_path"], book_id, book["title"], progress_cb=_async_progress(progress)),
                timeout=scaled_timeout(QA_INDEXING_TIMEOUT_SECONDS, book.get("page_count") or 0),
            )
        except pdf_qa.IndexingError as e:
            raise RuntimeError(str(e))
        library.mark_indexed(user["id"], book_id, num_chunks)
        return {"num_chunks": num_chunks}

    started = start_job(book_id, "index", job())
    return JSONResponse({"started": started})


async def ask_book(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    body = await _json_body(request)
    question = _text(body, "question", 4000)
    raw_history = body.get("history") or []
    raw_session_id = body.get("session_id")
    session_id = raw_session_id.strip() if isinstance(raw_session_id, str) and raw_session_id.strip() else None
    mode = body.get("mode") if body.get("mode") in ("single", "conversational") else "single"

    if not book.get("qa_indexed"):
        raise ApiError(status_code=409, detail="Index this book for Q&A first (tap 'Ask questions using AI').")
    if not question:
        raise ApiError(status_code=400, detail="Question can't be empty.")

    # history is only ever client-supplied conversation state for THIS
    # book/session (see app.js's "Conversational" mode) -- validated
    # defensively since it's untrusted input, same as any other body field.
    if not isinstance(raw_history, list):
        raise ApiError(status_code=400, detail="Invalid history.")
    history = []
    for turn in raw_history[-pdf_qa.MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            raise ApiError(status_code=400, detail="Invalid history entry.")
        q, a = turn.get("question"), turn.get("answer")
        if not isinstance(q, str) or not isinstance(a, str):
            raise ApiError(status_code=400, detail="Invalid history entry.")
        history.append({"question": q[:_HISTORY_Q_CHARS], "answer": a[:_HISTORY_A_CHARS]})

    # Consumed only once the request is known to be valid, and refunded by
    # the metered block on any failure below -- a timeout or an AI error
    # used to cost the user one of their ten daily questions.
    try:
        with subscriptions.metered(user["id"], "questions"):
            result = await asyncio.wait_for(
                pdf_qa.answer_question(
                    book_id, question, history=history, language=subscriptions.get_language(user["id"]),
                    title=book["title"],
                ),
                timeout=ASK_TIMEOUT_SECONDS,
            )
    except subscriptions.QuotaExceeded as e:
        raise ApiError(status_code=402, detail=str(e))
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail="That took too long and wasn't counted. Please try again.")
    except pdf_qa.IndexingError as e:
        # pdf_qa's own messages are written for users (not indexed yet,
        # search service down); the metered block has already refunded.
        raise ApiError(status_code=409, detail=f"{e} (Not counted.)")
    except ApiError:
        raise
    except Exception:
        logger.exception("Book Q&A failed for book_id=%s", book_id)
        raise ApiError(status_code=500, detail="Something went wrong answering that (not counted). Please try again.")

    # session_id is a client-generated thread id (see app.js's genId()) --
    # every question asked in the same Ask AI thread carries the same one,
    # so the whole conversation lands in one history entry for "🕘 History"
    # to reopen later, not just this single exchange. Persistence failing
    # should never take down an otherwise-successful answer, so it's
    # best-effort and logged rather than raised.
    if session_id:
        try:
            library.append_qa_turn(
                user["id"], book_id, session_id, mode, question, result["answer"], result.get("sources", [])
            )
        except Exception:
            logger.exception("Failed to persist Q&A turn to history for book_id=%s", book_id)

    return JSONResponse(result)


async def list_qa_sessions_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    return JSONResponse({"sessions": library.list_qa_sessions(user["id"], book_id)})


async def get_qa_session_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    session_id = request.path_params["session_id"]
    session = library.get_qa_session(user["id"], book_id, session_id)
    if session is None:
        raise ApiError(status_code=404, detail="Conversation not found.")
    return JSONResponse(session)


async def export_answer_pdf(request: Request):
    """
    Renders one already-answered Q&A exchange (question + Claude's answer +
    its numbered sources) as a PDF and sends it as a Telegram document to
    the user's own chat with the bot, for the "Export as PDF" button under
    each answer in the mini app's Ask AI panel.

    Sent via Telegram rather than streamed back as an HTTP response: this
    runs inside Telegram's in-app WebView, which has no native "save file"
    UI, and the standard web trick (an object URL + hidden <a download>)
    doesn't reliably save anything a user can find afterwards there --
    confirmed broken in practice. Every user of this mini app already has
    an open chat with this exact bot (that's how they got here), so
    delivering the file where Telegram itself already knows how to offer a
    download is the reliable option, same reasoning as bot.py's own
    chapter-file/drug-lookup PDF exports.

    Deliberately takes the question/answer/sources straight from the
    request body instead of re-running pdf_qa.answer_question() -- the
    frontend already has the exact exchange the user is looking at (see
    app.js's qaHistory), and re-asking would cost another Claude call, risk
    a slightly different answer, and require re-sending conversation
    history just to reconstruct something already in hand. Same
    "deliberately dumb formatting step" philosophy as pdf_export.py's other
    caller (drug_lookup's "Export as PDF").
    """
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    body = await _json_body(request)

    question = _text(body, "question", 4000)
    answer = _text(body, "answer", 20000)
    raw_sources = body.get("sources") or []

    if not question or not answer:
        raise ApiError(status_code=400, detail="Nothing to export -- question and answer are both required.")
    if not isinstance(raw_sources, list):
        raise ApiError(status_code=400, detail="Invalid sources.")

    sources = []
    for s in raw_sources:
        if not isinstance(s, dict):
            continue
        n, page, text = s.get("n"), s.get("page"), s.get("text")
        if isinstance(n, int) and isinstance(page, int) and isinstance(text, str):
            sources.append({"n": n, "page": page, "text": text})

    body_lines = [
        "Q: " + question,
        "",
        answer,
    ]
    if sources:
        body_lines.append("")
        body_lines.append("Sources")
        for s in sources:
            body_lines.append(f"[{s['n']}] Page {s['page']} — {s['text']}")

    try:
        pdf_bytes = await asyncio.to_thread(
            pdf_export.generate_text_pdf, book.get("title") or "Book Q&A", "\n".join(body_lines)
        )
    except Exception:
        logger.exception("Q&A answer PDF export failed for book_id=%s", book_id)
        raise ApiError(status_code=500, detail="Couldn't build the PDF. Please try again.")

    filename = safe_pdf_filename((book.get("title") or "qa-answer") + " - Q&A")
    try:
        await tg_bot.send_document(
            chat_id=user["id"],
            document=BufferedInputFile(pdf_bytes, filename=filename),
            caption=f"📄 Q&A export -- {book.get('title') or 'your book'}",
        )
    except TelegramAPIError:
        logger.exception("Failed to send Q&A export PDF to chat_id=%s", user["id"])
        raise ApiError(
            status_code=502,
            detail="Couldn't send that to your Telegram chat. Make sure you've started a chat with the bot, then try again.",
        )

    return JSONResponse({"sent": True})


# ---------------------------------------------------------------------------
# 5. Quiz (chapter selection, difficulty, question count)
# ---------------------------------------------------------------------------

async def create_quiz(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    chapters = book.get("chapters") or []
    if not chapters:
        raise ApiError(status_code=409, detail="Divide this book into chapters first.")

    body = await _json_body(request)

    chapter_indices = body.get("chapter_indices")
    difficulty = body.get("difficulty")
    num_questions = body.get("num_questions")

    chapter_indices = _valid_chapter_indices(chapter_indices, chapters)
    selected = [chapters[i] for i in chapter_indices]
    # Validated here, before any allowance is taken, rather than only inside
    # the job where a bad value surfaced as a failed (and charged) quiz.
    if difficulty not in quiz_ai.VALID_DIFFICULTIES:
        raise ApiError(status_code=400, detail="Pick a difficulty: easy, medium or difficult.")
    if not isinstance(num_questions, int) or isinstance(num_questions, bool) or not (1 <= num_questions <= quiz_ai.MAX_QUESTIONS):
        raise ApiError(status_code=400, detail=f"Number of questions must be between 1 and {quiz_ai.MAX_QUESTIONS}.")

    async def job():
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    quiz_ai.generate_quiz,
                    book["title"],
                    book["pdf_path"],
                    selected,
                    difficulty,
                    num_questions,
                    subscriptions.get_language(user["id"]),
                ),
                timeout=QUIZ_GENERATION_TIMEOUT_SECONDS,
            )
        except quiz_ai.QuizError as e:
            raise RuntimeError(str(e))

    started = start_metered_job(book_id, "quiz", job(), user["id"], "quizzes")
    return JSONResponse({"started": started})


async def save_quiz_attempt_endpoint(request: Request):
    """
    Records one finished (or ended-early) quiz attempt for "🕘 Quiz History"
    to list and reopen later, called when the user taps "🏁 End Test".

    The score is computed HERE from `questions` + `answers`, never trusted
    from the client, even though this is a personal study history with no
    competitive stakes -- it's just as easy to get right server-side, and
    it means a frontend bug can never silently write a wrong score into a
    student's own history.
    """
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    body = await _json_body(request)

    questions = body.get("questions")
    answers = body.get("answers")
    difficulty = body.get("difficulty")
    chapter_titles = body.get("chapter_titles")

    if not isinstance(questions, list) or not questions:
        raise ApiError(status_code=400, detail="No questions to record.")
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise ApiError(status_code=400, detail="answers must be a list the same length as questions.")
    if not isinstance(chapter_titles, list):
        chapter_titles = []
    if difficulty not in quiz_ai.VALID_DIFFICULTIES:
        difficulty = "medium"

    validated_questions = []
    correct_count = 0
    for q, a in zip(questions, answers):
        if not isinstance(q, dict) or "question" not in q or "options" not in q or "correct_index" not in q:
            raise ApiError(status_code=400, detail="Malformed question entry.")
        options = q["options"]
        if not isinstance(options, list) or len(options) != 4:
            raise ApiError(status_code=400, detail="Question does not have exactly 4 options.")
        ci = q["correct_index"]
        if isinstance(ci, bool) or not isinstance(ci, int) or not (0 <= ci < 4):
            raise ApiError(status_code=400, detail="Invalid correct_index in question.")
        # a is the user's chosen option index for this question, or null/None
        # if they never answered it before ending the test early.
        chosen = a if (isinstance(a, int) and not isinstance(a, bool) and 0 <= a < 4) else None
        if chosen is not None and chosen == ci:
            correct_count += 1
        validated_questions.append(
            {
                "question": str(q["question"]),
                "options": [str(o) for o in options],
                "correct_index": ci,
                "explanation": str(q.get("explanation", "")),
            }
        )

    total = len(validated_questions)
    attempt = {
        "attempt_id": uuid.uuid4().hex[:12],
        "created_at": time.time(),
        "difficulty": difficulty,
        "chapter_titles": [str(t) for t in chapter_titles][:50],
        "questions": validated_questions,
        "answers": [a if (isinstance(a, int) and not isinstance(a, bool) and 0 <= a < 4) else None for a in answers],
        "correct_count": correct_count,
        "total": total,
        "percentage": round(100 * correct_count / total, 1) if total else 0,
    }
    library.save_quiz_attempt(user["id"], book_id, attempt)
    return JSONResponse(attempt)


async def list_quiz_attempts_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    return JSONResponse({"attempts": library.list_quiz_attempts(user["id"], book_id)})


async def get_quiz_attempt_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    attempt_id = request.path_params["attempt_id"]
    attempt = library.get_quiz_attempt(user["id"], book_id, attempt_id)
    if attempt is None:
        raise ApiError(status_code=404, detail="Quiz attempt not found.")
    return JSONResponse(attempt)


# ---------------------------------------------------------------------------
# 6. Flashcards -- the mini app's own Flashcards section per book, sharing
# the exact same flashcards.py-backed deck flashcard_flow.py's chat-side
# 🧠 Study Tools -> 🗂 Flashcards uses, so cards generated/reviewed in
# either place show up in both.
# ---------------------------------------------------------------------------

def _flashcard_deck_summary(user_id: int, book_id: str) -> dict:
    return {
        "cards": flashcards.get_deck(user_id, book_id),
        "due_count": flashcards.count_due(user_id, book_id),
        "all_count": flashcards.count_all(user_id, book_id),
        "hard_count": flashcards.count_hard(user_id, book_id),
    }


async def list_flashcards_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    return JSONResponse(_flashcard_deck_summary(user["id"], book_id))


async def generate_flashcards_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    chapters = book.get("chapters") or []
    if not chapters:
        raise ApiError(status_code=409, detail="Divide this book into chapters first.")

    body = await _json_body(request)

    chapter_indices = body.get("chapter_indices")
    num_cards = body.get("num_cards")

    chapter_indices = _valid_chapter_indices(chapter_indices, chapters)
    selected = [chapters[i] for i in chapter_indices]
    if (
        not isinstance(num_cards, int)
        or isinstance(num_cards, bool)
        or not (1 <= num_cards <= chapter_ai.MAX_CARDS_PER_GENERATION)
    ):
        raise ApiError(status_code=400, detail=f"num_cards must be a whole number between 1 and {chapter_ai.MAX_CARDS_PER_GENERATION}.")

    # Cards already generated from any of these same chapters -- told to
    # Claude below as "already covered" so a second generation round for
    # the same chapter(s) writes genuinely new cards instead of repeats
    # (see chapter_ai.generate_flashcards's existing_fronts docstring).
    existing_fronts = [c["front"] for c in flashcards.cards_for_chapters(user["id"], book_id, chapter_indices)]

    async def job():
        try:
            raw_cards = await asyncio.wait_for(
                asyncio.to_thread(
                    chapter_ai.generate_flashcards_multi,
                    book["title"],
                    book["pdf_path"],
                    selected,
                    num_cards,
                    subscriptions.get_language(user["id"]),
                    existing_fronts=existing_fronts,
                ),
                timeout=FLASHCARD_GENERATION_TIMEOUT_SECONDS,
            )
        except chapter_ai.ChapterAIError as e:
            raise RuntimeError(str(e))
        chapter_label = ", ".join(c["title"] for c in selected)[:200]
        added = flashcards.add_cards(user["id"], book_id, book["title"], chapter_label, chapter_indices, raw_cards)
        return _flashcard_deck_summary(user["id"], book_id) | {"added_count": len(added)}

    started = start_metered_job(book_id, "flashcards", job(), user["id"], "flashcards")
    return JSONResponse({"started": started})


async def review_flashcard_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)

    body = await _json_body(request)

    card_id = body.get("card_id")
    quality = body.get("quality")
    if not isinstance(card_id, str) or not card_id:
        raise ApiError(status_code=400, detail="card_id is required.")
    if not isinstance(quality, int) or isinstance(quality, bool) or not (0 <= quality <= 5):
        raise ApiError(status_code=400, detail="quality must be a whole number between 0 and 5.")

    card = flashcards.review_card(user["id"], book_id, card_id, quality)
    if card is None:
        raise ApiError(status_code=404, detail="Card not found.")
    return JSONResponse(card)


async def export_flashcards_endpoint(request: Request):
    """
    Builds the deck's .apkg and sends it as a Telegram document to the
    user's own chat with the bot -- same reasoning as export_summary_pdf
    above: Telegram's in-app WebView has no reliable way to save a
    browser-triggered download, so the bot delivers it instead.
    """
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    cards = flashcards.get_deck(user["id"], book_id)
    if not cards:
        raise ApiError(status_code=409, detail="No cards to export yet -- generate some first.")

    workdir = user_dir(user["id"])
    # A file of its own per export: two taps used to share one path, and one
    # export's cleanup deleted the file the other was still reading.
    fd, output_path = tempfile.mkstemp(dir=workdir, prefix="anki-", suffix=".apkg")
    os.close(fd)
    try:
        await asyncio.to_thread(anki_export.export_deck_to_apkg, book["title"], book_id, cards, output_path)
        with open(output_path, "rb") as f:
            apkg_bytes = f.read()
    except anki_export.AnkiExportError as e:
        raise ApiError(status_code=500, detail=f"Couldn't export: {e}")
    finally:
        _safe_remove(output_path)

    filename = f"{book['title'][:50].strip() or 'deck'}.apkg"
    try:
        await tg_bot.send_document(
            chat_id=user["id"],
            document=BufferedInputFile(apkg_bytes, filename=filename),
            caption=f"🗂 Anki deck -- {book['title']}",
        )
    except TelegramAPIError:
        logger.exception("Failed to send .apkg export to chat_id=%s", user["id"])
        raise ApiError(
            status_code=502,
            detail="Couldn't send that to your Telegram chat. Make sure you've started a chat with the bot, then try again.",
        )

    return JSONResponse({"sent": True})


async def delete_flashcards_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    flashcards.delete_deck(user["id"], book_id)
    return JSONResponse({"deleted": True})


# ---------------------------------------------------------------------------
# 7. Notes -- the reader's own "📝 Notes" button per page, sharing the exact
# same notes.py-backed store as the chat-side 🧠 Study Tools -> 📓 My Notes,
# so a note added while reading shows up there too (and vice versa).
# ---------------------------------------------------------------------------

async def list_notes_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    return JSONResponse({"notes": notes.list_notes(user["id"], book_id)})


async def add_note_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    body = await _json_body(request)

    text = _text(body, "text", 10000)
    if not text:
        raise ApiError(status_code=400, detail="Note text is required.")

    page = body.get("page")
    if page is not None:
        if not isinstance(page, int) or isinstance(page, bool) or not (1 <= page <= book["page_count"]):
            raise ApiError(status_code=400, detail="Invalid page number.")

    note = notes.add_note(user["id"], book_id, page, text)
    return JSONResponse(note)


async def delete_note_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    note_id = request.path_params["note_id"]
    deleted = notes.delete_note(user["id"], book_id, note_id)
    if not deleted:
        raise ApiError(status_code=404, detail="Note not found.")
    return JSONResponse({"deleted": True})


# ---------------------------------------------------------------------------
# 7b. Reader tools (reader_tools.py): highlights, search in the book, and
# what can be done with selected text (explain it, make flashcards of it).
# ---------------------------------------------------------------------------

def _page_param(body: dict, book: dict, required: bool = True):
    page = body.get("page")
    if page is None and not required:
        return None
    if not isinstance(page, int) or isinstance(page, bool) or not (1 <= page <= (book.get("page_count") or 10**6)):
        raise ApiError(status_code=400, detail="Invalid page number.")
    return page


async def list_highlights_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    return JSONResponse({"highlights": reader_tools.list_highlights(user["id"], book_id)})


async def add_highlight_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    body = await _json_body(request)
    text = _text(body, "text", 20000)
    if not text:
        raise ApiError(status_code=400, detail="Select some text first.")
    try:
        item = reader_tools.add_highlight(
            user["id"], book_id, _page_param(body, book), text, body.get("color") or "yellow", body.get("anchor")
        )
    except ValueError as e:
        raise ApiError(status_code=400, detail=str(e))
    return JSONResponse(item)


async def update_highlight_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    body = await _json_body(request)
    try:
        item = reader_tools.update_highlight(user["id"], book_id, request.path_params["hl_id"], body.get("color"))
    except ValueError as e:
        raise ApiError(status_code=400, detail=str(e))
    if item is None:
        raise ApiError(status_code=404, detail="Highlight not found.")
    return JSONResponse(item)


async def delete_highlight_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)
    if not reader_tools.delete_highlight(user["id"], book_id, request.path_params["hl_id"]):
        raise ApiError(status_code=404, detail="Highlight not found.")
    return JSONResponse({"deleted": True})


async def export_highlights_pdf(request: Request):
    """All of a book's highlights as a PDF, sent to the user's chat (same pattern as the summary export)."""
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    items = reader_tools.list_highlights(user["id"], book_id)
    if not items:
        raise ApiError(status_code=400, detail="No highlights in this book yet.")
    title = book.get("title") or "Book"
    try:
        pdf_bytes = await asyncio.to_thread(
            pdf_export.generate_text_pdf, f"Highlights: {title}", reader_tools.highlights_as_text(title, items)
        )
    except Exception:
        logger.exception("Highlights PDF export failed for book_id=%s", book_id)
        raise ApiError(status_code=500, detail="Couldn't build the PDF. Please try again.")
    try:
        await tg_bot.send_document(
            chat_id=user["id"],
            document=BufferedInputFile(pdf_bytes, filename=safe_pdf_filename(f"{title} - Highlights")),
            caption=f"🖍 Highlights -- {title}",
        )
    except TelegramAPIError:
        logger.exception("Failed to send highlights PDF to chat_id=%s", user["id"])
        raise ApiError(
            status_code=502,
            detail="Couldn't send that to your Telegram chat. Make sure you've started a chat with the bot, then try again.",
        )
    return JSONResponse({"sent": True})


async def search_book_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    query = (request.query_params.get("q") or "").strip()
    if len(query) < 2 or len(query) > 200:
        raise ApiError(status_code=400, detail="Type at least 2 characters to search.")
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(reader_tools.search, book_id, book["pdf_path"], query), timeout=90
        )
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail="The book is still being read for search. Try again in a moment.")
    except Exception:
        logger.exception("Search failed in book %s", book_id)
        raise ApiError(status_code=500, detail="Couldn't search this book. Please try again.")
    return JSONResponse(result)


def _chapter_at(book: dict, page: int | None) -> tuple[list[int], str]:
    """The chapter a page is in (index list and title), for a flashcard made while reading."""
    chapters = book.get("chapters") or []
    if page:
        for i in range(len(chapters) - 1, -1, -1):
            if (chapters[i].get("start_page") or 0) <= page:
                return [i], chapters[i].get("title") or f"Page {page}"
    return [], f"Page {page}" if page else "Selection"


async def explain_selection_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    body = await _json_body(request)
    passage = _text(body, "text", 6000)
    if len(passage) < 3:
        raise ApiError(status_code=400, detail="Select some text first.")
    page = _page_param(body, book, required=False)
    context = ""
    if page:
        try:
            pages = await asyncio.wait_for(asyncio.to_thread(reader_tools.page_texts, book_id, book["pdf_path"]), timeout=60)
            context = (pages[page - 1] if page <= len(pages) else "")[:6000]
        except Exception:
            logger.info("No page text for explain in %s p.%s", book_id, page)
    try:
        with subscriptions.metered(user["id"], "questions"):
            answer = await asyncio.wait_for(
                asyncio.to_thread(
                    reader_tools.explain, book.get("title") or "Book", page, passage, context,
                    subscriptions.get_language(user["id"]),
                ),
                timeout=60,
            )
    except subscriptions.QuotaExceeded as e:
        raise ApiError(status_code=402, detail=str(e))
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail="That took too long (not counted). Please try again.")
    except Exception:
        logger.exception("Explain failed in book %s", book_id)
        raise ApiError(status_code=500, detail="Couldn't explain that just now (not counted). Please try again.")
    return JSONResponse({"answer": answer})


async def flashcards_from_selection_endpoint(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    body = await _json_body(request)
    passage = _text(body, "text", 6000)
    if len(passage) < 20:
        raise ApiError(status_code=400, detail="Select a longer passage (a sentence or more) to make a flashcard.")
    page = _page_param(body, book, required=False)
    indices, chapter_title = _chapter_at(book, page)
    num = 1 if len(passage) < 400 else 3
    try:
        with subscriptions.metered(user["id"], "questions"):
            raw = await asyncio.wait_for(
                asyncio.to_thread(
                    chapter_ai.generate_flashcards, f"{book.get('title') or 'Book'}, page {page or '?'}", passage, num,
                    subscriptions.get_language(user["id"]),
                ),
                timeout=60,
            )
    except subscriptions.QuotaExceeded as e:
        raise ApiError(status_code=402, detail=str(e))
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail="That took too long (not counted). Please try again.")
    except Exception:
        logger.exception("Flashcards from selection failed in book %s", book_id)
        raise ApiError(status_code=500, detail="Couldn't make flashcards just now (not counted). Please try again.")
    cards = flashcards.add_cards(user["id"], book_id, book.get("title") or "Book", chapter_title, indices, raw[:num])
    return JSONResponse({"cards": [{"front": c["front"], "back": c["back"]} for c in cards]})


# ---------------------------------------------------------------------------
# 8. Request a Book -- 📚 Request a Book button on the shelf itself (not one
# book's action grid, since the whole point is the customer doesn't have
# the book yet). See book_requests.py for the pending -> quoted -> paid ->
# delivered flow this kicks off; everything past this submission step
# (the admin's price quote, the customer's Stars payment, delivery) happens
# in chat -- admin_flow.py (/pricebook, /deliverbook) and customer_flow.py
# (bookreq:accept/decline, handle_successful_payment) -- since the customer
# needs a live message when the admin responds, which a mini app screen
# sitting open in the background can't reliably give them.
# ---------------------------------------------------------------------------

async def request_book_endpoint(request: Request):
    user = require_user(request)

    body = await _json_body(request)

    url = _text(body, "url", 2000)
    note = _text(body, "note", 5000)[:500]
    if not url:
        raise ApiError(status_code=400, detail="Please enter the book's URL.")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ApiError(status_code=400, detail="That doesn't look like a URL -- it should start with http:// or https://.")
    if len(url) > 2000:
        raise ApiError(status_code=400, detail="That URL is too long.")

    if not ADMIN_USER_IDS:
        raise ApiError(status_code=503, detail="This bot doesn't have an admin contact configured yet -- please try again later.")

    if book_requests.count_recent(user["id"]) >= book_requests.MAX_REQUESTS_PER_DAY:
        raise ApiError(
            status_code=429,
            detail=f"You've sent {book_requests.MAX_REQUESTS_PER_DAY} book requests today -- please wait for the admin to answer those.",
        )
    username = user.get("username")
    who = f"@{username}" if username else f"id {user['id']}"
    record = book_requests.create_request(user["id"], who, url, note)

    note_line = f"\n\nNote from customer: {note}" if note else ""
    sent_to_any = False
    for admin_id in ADMIN_USER_IDS:
        try:
            await tg_bot.send_message(
                chat_id=admin_id,
                text=(
                    f"📚 Book request from {who} (id {user['id']}):\n\n{url}{note_line}\n\n"
                    f"Reply with: /pricebook {record['request_id']} <price in USD, e.g. 12.50>"
                ),
            )
            sent_to_any = True
        except TelegramAPIError:
            logger.warning("Failed to forward book request %s to admin %s", record["request_id"], admin_id)

    if not sent_to_any:
        raise ApiError(status_code=502, detail="Couldn't reach the admin right now -- please try again later.")

    try:
        await tg_bot.send_message(
            chat_id=user["id"],
            text="📚 Your book request has been sent to the admin. You'll get a message right here in this chat once they quote a price.",
        )
    except TelegramAPIError:
        logger.warning("Failed to send book-request confirmation to chat_id=%s", user["id"])

    return JSONResponse({"sent": True, "request_id": record["request_id"]})


# ---------------------------------------------------------------------------
# App wiring
# ---------------------------------------------------------------------------

_WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")


# ---------------------------------------------------------------------------
# Drugs: interactions + label Q&A
# ---------------------------------------------------------------------------
#
# The same two tools the bot's 💊 Drugs Info menu offers, exposed to the mini
# app so they can be used interactively -- typing a name with live
# suggestions, building up a drug list by tapping, asking follow-ups in a
# thread -- instead of as a turn-by-turn chat exchange. Both call exactly the
# functions the chat flows call (interaction_ai.analyze_interactions,
# drug_qa.answer_question), so there is one implementation of each and the
# answers cannot drift between the two surfaces.

_DRUG_LOOKUP_TIMEOUT_SECONDS = 15   # openFDA is either quick or not coming back
_DRUG_ANSWER_TIMEOUT_SECONDS = 55
# An interaction check writes a structured block per pair, so it gets longer.
_INTERACTION_ANSWER_TIMEOUT_SECONDS = 85
_DRUG_RESOLVE_TIMEOUT_SECONDS = 15
_MAX_INTERACTION_DRUGS = 15  # mirrors interaction_flow.MAX_DRUGS

# A ceiling on the whole interaction check, not just its parts. Fifteen
# sequential lookups at 15s each plus a 55s answer is over four minutes in
# the worst case, and nothing in front of this app is guaranteed to wait
# that long: a proxy that gives up first replaces our JSON with its own
# error page, which is how a failed check reached a user as a bare
# "Request failed (502)" with nothing to act on. Failing ourselves, in
# time, means the reason always comes from us.
_DRUG_TOTAL_BUDGET_SECONDS = 110


# Client-supplied conversation history is trimmed per turn to these.
_HISTORY_Q_CHARS = 4000
_HISTORY_A_CHARS = 12000


def _text(body: dict, key: str, max_len: int) -> str:
    """A string field: missing -> "", stripped; 400 if it isn't a string or is longer than max_len."""
    value = body.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ApiError(status_code=400, detail=f"Invalid {key}.")
    value = value.strip()
    if len(value) > max_len:
        raise ApiError(status_code=400, detail=f"That {key} is too long (max {max_len} characters).")
    return value


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise ApiError(status_code=400, detail="Invalid JSON body.")
    if not isinstance(body, dict):
        raise ApiError(status_code=400, detail="Invalid JSON body.")
    return body


_RESOLVER_PER_HOUR = 30
_resolver_calls: dict[int, list[float]] = {}


def _resolver_allowed() -> bool:
    """
    "Did you mean" is an AI call, made on every miss and not counted
    against any allowance: limited per user, so a script of misses can't
    run up the AI bill (or openFDA's daily limit) for everyone.
    """
    uid = _request_user_id.get()
    if uid is None:
        return True
    now = time.time()
    calls = [t for t in _resolver_calls.get(uid, []) if now - t < 3600]
    if len(calls) >= _RESOLVER_PER_HOUR:
        _resolver_calls[uid] = calls
        return False
    calls.append(now)
    _resolver_calls[uid] = calls
    return True


async def _fetch_label(name: str) -> dict:
    """
    One drug's FDA label, or an ApiError carrying suggestions.

    A miss is answered with 404 plus the names Claude thinks were meant, so
    the mini app can offer them as buttons rather than making the user guess
    at the spelling again -- the same recovery the chat flow does with
    name_resolver, just handed to the client to render.
    """
    if drug_lookup.name_too_long(name):
        raise ApiError(status_code=400, detail=drug_lookup.TOO_LONG_MESSAGE)
    try:
        return await asyncio.wait_for(drug_lookup.lookup_drug(name), timeout=_DRUG_LOOKUP_TIMEOUT_SECONDS)
    except drug_lookup.DrugNotFoundError:
        if not _resolver_allowed():
            raise ApiError(
                status_code=404,
                detail={"message": f"No FDA label found for \"{name}\". Check the spelling.", "suggestions": []},
            )
        try:
            suggestions = await asyncio.wait_for(
                asyncio.to_thread(name_resolver.resolve_drug_names, name),
                timeout=_DRUG_RESOLVE_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("Drug name resolution failed for %r", name)
            suggestions = []
        raise ApiError(
            status_code=404,
            detail={"message": f"No FDA label found for \"{name}\".", "suggestions": suggestions},
        )
    except drug_lookup.DrugLookupRateLimitedError:
        raise ApiError(status_code=429, detail="openFDA is rate-limiting us right now. Please try again shortly.")
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail=f"Looking up \"{name}\" took too long. Please try again.")
    except Exception:
        # drug_lookup handles httpx.TimeoutException and ConnectError itself
        # but nothing else, so a ProxyError, a read error or a malformed
        # response comes straight through here. Uncaught, that leaves the
        # request with no JSON body at all and the client showing a bare
        # status code. Everything unexpected becomes one honest 502 instead.
        logger.exception("openFDA lookup failed unexpectedly for %r", name)
        raise ApiError(
            status_code=502,
            detail=f"Couldn't reach the FDA label database for \"{name}\". Please try again in a moment.",
        )


def has_full_access(user_id: int) -> bool:
    """Logged in to an account, Premium, or the admin. Everyone else is a guest."""
    return subscriptions.is_premium(user_id)


def _full_only(endpoint, what: str):
    """Guests can't save or export: the endpoint answers 403 with a pointer to logging in."""
    async def guarded(request: Request):
        user = require_user(request)
        if not has_full_access(user["id"]):
            raise ApiError(status_code=403, detail=f"{what} isn't available in guest mode. Log in (Shelf > Log in) for full access.")
        return await endpoint(request)
    guarded.__name__ = endpoint.__name__
    return guarded


def _save_drug_turn(
    user_id: int, topic_id, kind: str, drugs: list[str], question: str, answer: str, title: str, sources: list | None = None
) -> None:
    """Keep a Drugs-tab answer as a saved topic (best effort), like Ask-your-books answers."""
    if not ask_sessions.valid_id(topic_id) or not has_full_access(user_id):
        return  # guests' answers aren't saved
    try:
        ask_sessions.append_turn(
            user_id, topic_id, [{"book_id": f"drug:{d}", "title": d} for d in drugs], question, answer, sources or [],
            kind=kind, title=title,
        )
    except Exception:
        logger.exception("Couldn't save %s topic %s", kind, topic_id)


async def drug_search(request: Request):
    """Live name suggestions for the two inputs. Never an error: an empty
    list is a perfectly good answer to a half-typed word."""
    require_user(request)
    prefix = (request.query_params.get("q") or "").strip()
    if len(prefix) < 2:
        return JSONResponse({"names": []})
    try:
        names = await asyncio.wait_for(drug_lookup.search_drug_names(prefix), timeout=_DRUG_LOOKUP_TIMEOUT_SECONDS)
    except Exception:
        # Autocomplete is a convenience; a failed suggestion fetch must not
        # look like a broken screen while the user is still typing.
        logger.info("Drug name search failed for %r (non-fatal)", prefix)
        names = []
    return JSONResponse({"names": names})


async def drug_resolve(request: Request):
    """
    Confirm one name against openFDA, at the moment it is added.

    Checking each name as it goes in, rather than when the whole check
    runs, is what keeps a typo cheap. Before this, adding "Sertralin"
    succeeded silently and the mistake only surfaced after a two-drug
    interaction check had been waited out -- a slow failure for something
    knowable in a second, and the one path through this endpoint that was
    both the slowest and the likeliest to be hit.

    Returns the label's own spelling on success, or 404 with suggestions,
    which is the same shape the other two endpoints use for a miss.
    """
    require_user(request)
    body = await _json_body(request)
    name = _text(body, "name", 200)
    if not name:
        raise ApiError(status_code=400, detail="Type a drug name first.")
    sections = await _fetch_label(name)
    return JSONResponse({"name": sections.get("_name") or name})


async def drug_interactions(request: Request):
    user = require_user(request)
    body = await _json_body(request)

    raw_names = body.get("names")
    if not isinstance(raw_names, list):
        raise ApiError(status_code=400, detail="Invalid drug list.")
    names, seen = [], set()
    for raw in raw_names:
        if not isinstance(raw, str):
            raise ApiError(status_code=400, detail="Invalid drug list.")
        name = raw.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            names.append(name)
    if len(names) < 2:
        raise ApiError(status_code=400, detail="Add at least 2 drugs to check for interactions.")
    if len(names) > _MAX_INTERACTION_DRUGS:
        raise ApiError(
            status_code=400,
            detail=f"That's more than {_MAX_INTERACTION_DRUGS} drugs -- checking every pair beyond that "
            "produces a wall of text nobody reads. Remove a few.",
        )

    async def run() -> tuple[str, list[str]]:
        # Sequentially rather than with gather: openFDA rate-limits, and a
        # burst of 15 parallel lookups is the reliable way to get a 429 for
        # the whole request instead of an answer.
        drugs = []
        for name in names:
            sections = await _fetch_label(name)
            drugs.append({"name": sections.get("_name") or name, "sections": sections, "typed": name})
        await interaction_ai.add_drug_classes(drugs)  # best effort, never raises

        # Charged only here, once every label was found: a misspelled drug
        # or an openFDA hiccup used to cost a question before any AI ran.
        with subscriptions.metered(user["id"], "questions"):
            answer = await asyncio.wait_for(
                asyncio.to_thread(
                    interaction_ai.analyze_interactions, drugs, subscriptions.get_language(user["id"])
                ),
                timeout=_INTERACTION_ANSWER_TIMEOUT_SECONDS,
            )
        return answer, [d["name"] for d in drugs]

    try:
        answer, resolved = await asyncio.wait_for(run(), timeout=_DRUG_TOTAL_BUDGET_SECONDS)
    except subscriptions.QuotaExceeded as e:
        raise ApiError(status_code=402, detail=str(e))
    except ApiError:
        raise  # a 404/429/502 from a lookup already says exactly what went wrong
    except asyncio.TimeoutError:
        raise ApiError(
            status_code=504,
            detail=f"Checking {len(names)} drugs took too long. Try again, or with fewer drugs at once.",
        )
    except Exception:
        logger.exception("Mini app interaction check failed")
        raise ApiError(status_code=500, detail="Couldn't check those interactions just now (not counted). Please try again.")

    label = " + ".join(resolved)
    _save_drug_turn(user["id"], body.get("topic_id"), "interactions", resolved, f"Interactions: {label}", answer, title=label)
    return JSONResponse({"answer": answer, "drugs": resolved})


async def drug_ask(request: Request):
    user = require_user(request)
    body = await _json_body(request)

    name = _text(body, "drug", 200)
    question = _text(body, "question", 4000)
    if not name:
        raise ApiError(status_code=400, detail="Which drug is the question about?")
    if not question:
        raise ApiError(status_code=400, detail="Question can't be empty.")

    # Client-supplied conversation state for this drug, so a follow-up
    # ("and in renal impairment?") does not have to repeat itself. Untrusted
    # like any other body field, so it is validated shape-first.
    raw_history = body.get("history") or []
    if not isinstance(raw_history, list):
        raise ApiError(status_code=400, detail="Invalid history.")
    history = []
    for turn in raw_history[-drug_qa.MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            raise ApiError(status_code=400, detail="Invalid history entry.")
        q, a = turn.get("question"), turn.get("answer")
        if not isinstance(q, str) or not isinstance(a, str):
            raise ApiError(status_code=400, detail="Invalid history entry.")
        history.append({"question": q[:_HISTORY_Q_CHARS], "answer": a[:_HISTORY_A_CHARS]})

    # Websites the admin logged the bot in to (web_sources.py), ticked by the
    # user: searched for this drug and question, and cited next to the label.
    raw_webs = body.get("web_ids") or []
    if not isinstance(raw_webs, list) or len(raw_webs) > 5:
        raise ApiError(status_code=400, detail="Invalid websites.")
    webs = []
    use_pubmed = False
    for wid in dict.fromkeys(w for w in raw_webs if isinstance(w, str)):
        if wid == pubmed.SOURCE_ID:
            use_pubmed = True  # NCBI's API: no login, always available
            continue
        site = web_sources.get_site(wid[4:]) if wid.startswith("web:") and web_sources.valid_id(wid[4:]) else None
        if site is None or not site.get("enabled") or not site.get("search_url"):
            continue  # switched off since the list was loaded: just not searched
        webs.append({"id": wid[4:], "title": site["name"]})

    sections = await _fetch_label(name)
    resolved = sections.get("_name") or name

    web_passages, skipped = [], []
    if webs or use_pubmed:
        terms = await web_sources.search_terms(f"{resolved}: {question}")
        if resolved.lower() not in terms.lower():
            terms = f"{resolved} {terms}"[:200]
        sources = ([(pubmed.SOURCE_TITLE, pubmed.passages(terms), 20)] if use_pubmed else []) + [
            (w["title"], web_sources.passages(w["id"], terms), _WEB_FETCH_TIMEOUT_SECONDS) for w in webs
        ]
        results = await asyncio.gather(
            *(asyncio.wait_for(coro, timeout=limit) for _, coro, limit in sources), return_exceptions=True
        )
        for (title, _, _), r in zip(sources, results):
            if isinstance(r, BaseException):
                logger.warning("Source %s failed for a drug question: %r", title, r)
                skipped.append(title)
            else:
                web_passages += r
        web_passages = web_passages[:drug_qa.MAX_WEB_PASSAGES]

    try:
        with subscriptions.metered(user["id"], "questions"):
            answer = await asyncio.wait_for(
                asyncio.to_thread(
                    drug_qa.answer_question,
                    resolved,
                    sections,
                    question,
                    history,
                    subscriptions.get_language(user["id"]),
                    web_passages,
                ),
                timeout=_DRUG_ANSWER_TIMEOUT_SECONDS,
            )
    except subscriptions.QuotaExceeded as e:
        raise ApiError(status_code=402, detail=str(e))
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail="That question took too long to answer. Please try again.")
    except drug_qa.DrugQAError as e:
        raise ApiError(status_code=409, detail=str(e))
    except Exception:
        logger.exception("Mini app drug Q&A failed for %r", resolved)
        raise ApiError(status_code=500, detail="Couldn't answer that just now (not counted). Please try again.")

    sources = [
        {"n": i, "title": p["title"], "url": p["url"], "text": p["text"][:600]} for i, p in enumerate(web_passages, 1)
    ]
    _save_drug_turn(
        user["id"], body.get("topic_id"), "drug", [resolved], question, answer, title=f"{resolved}: {question}", sources=sources
    )
    return JSONResponse({"answer": answer, "drug": resolved, "sources": sources, "skipped": skipped})


async def drug_websites(request: Request):
    """The websites Drug LookUp can also search, and whether this user manages them (the admin)."""
    user = require_user(request)
    sites = [{"id": pubmed.SOURCE_ID, "title": pubmed.SOURCE_TITLE}] + [
        {"id": s["book_id"], "title": s["title"]} for s in web_sources.ask_sources()
    ]
    return JSONResponse({"sites": sites, "is_admin": subscriptions.is_admin(user["id"])})


# ---------------------------------------------------------------------
# Lexicomp / Lexidrug (read off the VMOS cloud phone -- see lexidrug.py)
# ---------------------------------------------------------------------
# Opening a drug is the slow step (the phone searches, opens and scrolls
# the monograph), so it is its own endpoint with its own budget, kept under
# the ~110s a proxy in front of us is assumed to wait. Asking then reads the
# cached monograph and is an ordinary Claude call.

_LEXI_OPEN_BUDGET_SECONDS = 100


async def lexidrug_status(request: Request):
    require_user(request)
    return JSONResponse({"enabled": lexidrug.is_enabled()})


async def _lexi_monograph(drug: str) -> dict:
    try:
        return await asyncio.wait_for(lexidrug.get_monograph(drug), timeout=_LEXI_OPEN_BUDGET_SECONDS)
    except lexidrug.LexidrugError as e:
        raise ApiError(status_code=502, detail=str(e))
    except asyncio.TimeoutError:
        raise ApiError(
            status_code=504,
            detail="The cloud phone took too long to open that monograph. Please try again -- "
            "it is often quicker the second time.",
        )
    except Exception:
        logger.exception("Lexidrug fetch failed unexpectedly for %r", drug)
        raise ApiError(status_code=502, detail="Couldn't read Lexidrug on the cloud phone. Please try again shortly.")


_LEXI_OPENS_PER_HOUR = 10
_lexi_opens: dict[int, list[float]] = {}


async def lexidrug_open(request: Request):
    user = require_user(request)
    body = await _json_body(request)
    drug = _text(body, "drug", 200)
    if not drug:
        raise ApiError(status_code=400, detail="Type a drug name first.")
    if len(drug) > 80:
        raise ApiError(status_code=400, detail="That drug name is too long.")
    # Opening an uncached drug drives the one cloud phone for up to 100 s,
    # for everyone: limited per user (cached ones are free and instant).
    if lexidrug._cache_get(drug) is None and not subscriptions.is_admin(user["id"]):
        now = time.time()
        recent = [t for t in _lexi_opens.get(user["id"], []) if now - t < 3600]
        if len(recent) >= _LEXI_OPENS_PER_HOUR:
            raise ApiError(status_code=429, detail="That's a lot of new drugs in an hour -- please try again a little later.")
        _lexi_opens[user["id"]] = recent + [now]
    entry = await _lexi_monograph(drug)
    return JSONResponse({"drug": entry["title"], "fetched_at": entry["fetched_at"]})


async def lexidrug_ask(request: Request):
    user = require_user(request)
    body = await _json_body(request)
    drug = _text(body, "drug", 200)
    question = _text(body, "question", 4000)
    if not drug:
        raise ApiError(status_code=400, detail="Which drug is the question about?")
    if not question:
        raise ApiError(status_code=400, detail="Question can't be empty.")

    raw_history = body.get("history") or []
    if not isinstance(raw_history, list):
        raise ApiError(status_code=400, detail="Invalid history.")
    history = []
    for turn in raw_history[-lexidrug.MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict) or not isinstance(turn.get("question"), str) or not isinstance(turn.get("answer"), str):
            raise ApiError(status_code=400, detail="Invalid history entry.")
        history.append({"question": turn["question"][:_HISTORY_Q_CHARS], "answer": turn["answer"][:_HISTORY_A_CHARS]})

    if len(drug) > 80:
        raise ApiError(status_code=400, detail="That drug name is too long.")
    # Only from the cache: opening a drug is /open's job (rate-limited there).
    entry = lexidrug._cache_get(drug)
    if entry is None:
        raise ApiError(status_code=409, detail="Open the drug first (tap Open), then ask.")

    try:
        with subscriptions.metered(user["id"], "questions"):
            answer = await asyncio.wait_for(
                asyncio.to_thread(
                    lexidrug.answer_question,
                    entry["title"],
                    entry["text"],
                    question,
                    history,
                    subscriptions.get_language(user["id"]),
                ),
                timeout=_DRUG_ANSWER_TIMEOUT_SECONDS,
            )
    except subscriptions.QuotaExceeded as e:
        raise ApiError(status_code=402, detail=str(e))
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail="That question took too long to answer. Please try again.")
    except lexidrug.LexidrugError as e:
        raise ApiError(status_code=502, detail=str(e))
    except Exception:
        logger.exception("Lexidrug Q&A failed for %r", drug)
        raise ApiError(status_code=500, detail="Couldn't answer that just now (not counted). Please try again.")

    return JSONResponse({"answer": answer, "drug": entry["title"]})


# ---------------------------------------------------------------------------
# 💬 Ask tab: one question across several books, and specialty libraries
# ---------------------------------------------------------------------------
# A user ticks any mix of their own indexed books and the admin-provided
# books of a specialty (shared_library.py), then asks once. Shared books are
# answer sources only -- no endpoint here returns their file.

_MULTI_ASK_TIMEOUT_SECONDS = 75  # several indexes to load and search, then one answer
_WEB_FETCH_TIMEOUT_SECONDS = 45  # one website: search, then read its top results (web_sources.py)


def _require_admin(request: Request) -> dict:
    user = require_user(request)
    if not subscriptions.is_admin(user["id"]):
        raise ApiError(status_code=403, detail="Only the admin can change the specialty libraries.")
    return user


def _shared_catalog() -> list[dict]:
    """shared_library.catalog() plus each book's live indexing state from the job table."""
    cat = shared_library.catalog()
    for spec in cat:
        for b in spec["books"]:
            job = get_job(b["book_id"], "index") or {}
            b["indexing"] = job.get("status") == "running"
            b["progress"] = job.get("progress") if b["indexing"] else None
    return cat


async def ask_library(request: Request):
    user = require_user(request)
    my_books = [
        {
            "book_id": bid,
            "title": b["title"],
            "page_count": b.get("page_count"),
            "qa_indexed": bool(b.get("qa_indexed")),
        }
        for bid, b in library.own_books(user["id"]).items()
    ]
    my_books.sort(key=lambda b: b["title"].lower())
    return JSONResponse(
        {
            "my_books": my_books,
            "specialties": _shared_catalog(),
            "is_admin": subscriptions.is_admin(user["id"]),
            # Admin only: the standard books per specialty, to upload into the library.
            "recommended": recommended_books.RECOMMENDED if subscriptions.is_admin(user["id"]) else None,
            "max_books": pdf_qa.MAX_BOOKS_PER_QUESTION,
            "max_upload_mb": MAX_SHELF_UPLOAD_BYTES // 1024 // 1024,
        }
    )


async def ask_multi(request: Request):
    user = require_user(request)
    body = await _json_body(request)
    question = _text(body, "question", 4000)
    raw_ids = body.get("book_ids")
    if not question:
        raise ApiError(status_code=400, detail="Question can't be empty.")
    if not isinstance(raw_ids, list) or not raw_ids:
        raise ApiError(status_code=400, detail="Tick at least one book to ask.")
    if len(raw_ids) > pdf_qa.MAX_BOOKS_PER_QUESTION:
        raise ApiError(
            status_code=400,
            detail=f"That's more than {pdf_qa.MAX_BOOKS_PER_QUESTION} books at once. Untick a few.",
        )

    # Every id is resolved against what THIS user may read: their own
    # shelf, or the shared libraries. An id that is neither is refused, so
    # a guessed id can never reach another user's book.
    books, seen = [], set()
    for bid in raw_ids:
        if not isinstance(bid, str) or bid in seen:
            continue
        seen.add(bid)
        if bid.startswith("web:"):
            continue  # websites moved to Drug LookUp; an old saved selection just drops them
        book = shared_library.get_book(bid) if shared_library.is_shared_id(bid) else library.get_book(user["id"], bid)
        if book is None:
            raise ApiError(status_code=404, detail="One of the selected books isn't available anymore. Refresh and try again.")
        if not book.get("qa_indexed"):
            raise ApiError(status_code=409, detail=f"\"{book['title']}\" isn't indexed for questions yet.")
        books.append({"book_id": bid, "title": book["title"]})
    if not books:
        raise ApiError(status_code=400, detail="Tick at least one book to ask.")

    raw_history = body.get("history") or []
    if not isinstance(raw_history, list):
        raise ApiError(status_code=400, detail="Invalid history.")
    history = []
    for turn in raw_history[-pdf_qa.MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict) or not isinstance(turn.get("question"), str) or not isinstance(turn.get("answer"), str):
            raise ApiError(status_code=400, detail="Invalid history entry.")
        history.append({"question": turn["question"][:_HISTORY_Q_CHARS], "answer": turn["answer"][:_HISTORY_A_CHARS]})

    try:
        with subscriptions.metered(user["id"], "questions"):  # one question, however many books
            result = await asyncio.wait_for(
                pdf_qa.answer_question_multi(
                    books, question, history=history, language=subscriptions.get_language(user["id"]),
                ),
                timeout=_MULTI_ASK_TIMEOUT_SECONDS,
            )
    except subscriptions.QuotaExceeded as e:
        raise ApiError(status_code=402, detail=str(e))
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail="That took too long and wasn't counted. Try again, or with fewer books.")
    except pdf_qa.IndexingError as e:
        raise ApiError(status_code=409, detail=f"{e} (Not counted.)")
    except Exception:
        logger.exception("Multi-book Q&A failed for books=%s", [b["book_id"] for b in books])
        raise ApiError(status_code=500, detail="Something went wrong answering that (not counted). Please try again.")

    # Saved under the client's topic id, so the whole topic can be reopened,
    # continued or exported later. Best effort: a failed save must not cost
    # the user the answer they just got.
    topic_id = body.get("topic_id")
    if ask_sessions.valid_id(topic_id) and has_full_access(user["id"]):  # guests' answers aren't saved
        try:
            ask_sessions.append_turn(user["id"], topic_id, books, question, result["answer"], result["sources"])
        except Exception:
            logger.exception("Couldn't save Ask topic %s", topic_id)
    return JSONResponse(result)


def _start_shared_index(book_id: str) -> bool:
    """Index a shared book in the background; the result is recorded on its registry entry."""
    book = shared_library.get_book(book_id)
    if book is None:
        return False

    async def job():
        def progress(text):
            set_job_progress(book_id, "index", text)

        try:
            num_chunks = await asyncio.wait_for(
                pdf_qa.build_index(book["pdf_path"], book_id, book["title"], progress_cb=_async_progress(progress)),
                timeout=scaled_timeout(QA_INDEXING_TIMEOUT_SECONDS, book.get("page_count") or 0),
            )
        except pdf_qa.IndexingError as e:
            shared_library.set_index_error(book_id, str(e))
            raise RuntimeError(str(e))
        except asyncio.TimeoutError:
            shared_library.set_index_error(book_id, "Indexing took too long and was stopped.")
            raise
        except Exception:
            shared_library.set_index_error(book_id, "Indexing failed unexpectedly.")
            raise
        shared_library.mark_indexed(book_id, num_chunks)
        # Divide it into chapters once, for every user who opens it --
        # otherwise each would pay (and wait) for the same division.
        if not (shared_library.get_book(book_id) or {}).get("chapters"):
            set_job_progress(book_id, "index", "Finding chapters...")
            try:
                chapters = await asyncio.wait_for(
                    asyncio.to_thread(detect_chapters_cached, book["pdf_path"]),
                    timeout=scaled_timeout(CHAPTER_DIVISION_TIMEOUT_SECONDS, book.get("page_count") or 0),
                )
                shared_library.set_chapters(book_id, compute_chapter_ranges(chapters, book.get("page_count") or 1))
            except Exception:
                logger.exception("Chapter division for shared book %s failed (non-fatal)", book_id)
        return {"num_chunks": num_chunks}

    shared_library.set_index_error(book_id, None)
    return start_job(book_id, "index", job())


def _shared_error(e: Exception) -> ApiError:
    return ApiError(status_code=400, detail=str(e))


async def shared_add_specialty(request: Request):
    _require_admin(request)
    body = await _json_body(request)
    try:
        name = shared_library.add_specialty(body.get("name"))
    except shared_library.SharedLibraryError as e:
        raise _shared_error(e)
    return JSONResponse({"name": name, "specialties": _shared_catalog()})


async def shared_remove_specialty(request: Request):
    _require_admin(request)
    body = await _json_body(request)
    try:
        shared_library.remove_specialty(body.get("name"))
    except shared_library.SharedLibraryError as e:
        raise _shared_error(e)
    return JSONResponse({"specialties": _shared_catalog()})


async def shared_upload(request: Request):
    """Admin uploads a PDF straight into a specialty; it is indexed automatically."""
    user = _require_admin(request)
    dest_path, page_count, form, filename = await _save_uploaded_pdf(request, shared_library.BOOKS_DIR)
    try:
        specialty = shared_library.clean_specialty_name(form.get("specialty"))
        title = " ".join(str(form.get("title") or "").split())[:200] or os.path.splitext(filename)[0] or "Untitled book"
        book_id = shared_library.add_book(specialty, title, dest_path, page_count, added_by=user["id"])
    except shared_library.SharedLibraryError as e:
        _safe_remove(dest_path)
        raise _shared_error(e)
    _start_shared_index(book_id)
    return JSONResponse({"book_id": book_id, "specialties": _shared_catalog()})


async def shared_publish(request: Request):
    """Admin copies a book from their OWN shelf into a specialty (reusing its index when it has one)."""
    user = _require_admin(request)
    body = await _json_body(request)
    book = _book_or_404(user["id"], body.get("book_id") if isinstance(body.get("book_id"), str) else "")
    try:
        specialty = shared_library.clean_specialty_name(body.get("specialty"))
    except shared_library.SharedLibraryError as e:
        raise _shared_error(e)
    if not os.path.exists(book["pdf_path"]):
        raise ApiError(status_code=404, detail="That book's file is missing on the server.")

    new_id = shared_library.new_book_id()
    dest_path = os.path.join(shared_library.BOOKS_DIR, f"{new_id}.pdf")
    os.makedirs(shared_library.BOOKS_DIR, exist_ok=True)
    # A real copy, not a link: deleting the shelf book later must not take
    # the shared one with it.
    await asyncio.to_thread(shutil.copyfile, book["pdf_path"], dest_path)
    try:
        shared_library.add_book(specialty, book["title"], dest_path, book.get("page_count") or 0, user["id"], book_id=new_id)
    except shared_library.SharedLibraryError as e:
        _safe_remove(dest_path)
        raise _shared_error(e)

    copied = book.get("qa_indexed") and await asyncio.to_thread(pdf_qa.copy_index, body["book_id"], new_id)
    if copied:
        shared_library.mark_indexed(new_id, book.get("num_chunks") or 0)
    else:
        _start_shared_index(new_id)
    return JSONResponse({"book_id": new_id, "specialties": _shared_catalog()})


async def shared_reindex(request: Request):
    _require_admin(request)
    book_id = request.path_params["book_id"]
    if shared_library.get_book(book_id) is None:
        raise ApiError(status_code=404, detail="Book not found.")
    return JSONResponse({"started": _start_shared_index(book_id), "specialties": _shared_catalog()})


async def shared_delete_book(request: Request):
    _require_admin(request)
    book_id = request.path_params["book_id"]
    if (get_job(book_id, "index") or {}).get("status") == "running":
        raise ApiError(status_code=409, detail="This book is still being indexed. Remove it once that finishes.")
    if shared_library.remove_book(book_id) is None:
        raise ApiError(status_code=404, detail="Book not found.")
    pdf_qa.delete_index(book_id)
    covers.delete_covers(book_id)
    return JSONResponse({"specialties": _shared_catalog()})


# ---------------------------------------------------------------------------
# Chunked uploads (shelf books and, for admins, specialty-library books)
# ---------------------------------------------------------------------------
# A single multipart POST was slow in three ways: Starlette spooled the
# whole body to a temp file before our code saw a byte, the file was then
# copied a second time onto the volume, and on a slow or high-latency link
# (Telegram's WebView on mobile data, far from the server) one TCP stream
# can't use the connection's full bandwidth -- and one dropped connection
# meant starting over.
#
# Now the client splits the file into 8 MB chunks and sends several at
# once; each chunk is written straight to its offset in the final file on
# the volume, a failed chunk is retried on its own, and "finish" only has
# to check the PDF and register it. The old /api/upload stays for clients
# still running a cached older app.js.

# 2 MB parts, sent 6 at a time by the client: on a slow mobile link a dropped
# connection loses at most one small part, and more parallel streams use a
# high-latency route (far from the Amsterdam server) better than fewer.
UPLOAD_CHUNK_BYTES = 2 * 1024 * 1024
_WRITE_BUFFER_BYTES = 1024 * 1024
_UPLOAD_TTL_SECONDS = 2 * 3600
_UPLOAD_MAX_LIFETIME_SECONDS = 6 * 3600  # however busy, an upload can't hold its space forever
_MAX_OPEN_UPLOADS_PER_USER = 3
_uploads: dict[str, dict] = {}

# Parts are assembled here, not at the book's final path, so an abandoned
# upload can't leave a file behind in someone's library; the finished file
# is moved into place only after it passed the checks. Each upload's state
# is kept next to its parts ({id}.json), so a restart -- every deploy is
# one -- doesn't lose an upload in progress: the client's next part picks
# it up again. Leftovers older than the longest an upload may live are
# removed at startup.
_UPLOAD_PARTS_DIR = os.path.join(STORAGE_DIR, "_uploads")
os.makedirs(_UPLOAD_PARTS_DIR, exist_ok=True)
_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")


def _sweep_upload_leftovers() -> None:
    cutoff = time.time() - 6 * 3600
    for name in os.listdir(_UPLOAD_PARTS_DIR):
        path = os.path.join(_UPLOAD_PARTS_DIR, name)
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


_sweep_upload_leftovers()


def _upload_meta_path(upload_id: str) -> str:
    return os.path.join(_UPLOAD_PARTS_DIR, f"{upload_id}.json")


def _save_upload_meta(upload_id: str, up: dict) -> None:
    data = {k: v for k, v in up.items() if k not in ("writers", "closed", "received")}
    data["received"] = sorted(up["received"])
    tmp = f"{_upload_meta_path(upload_id)}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _upload_meta_path(upload_id))
    except OSError:
        logger.exception("Couldn't save the state of upload %s", upload_id)
        _safe_remove(tmp)


def _load_upload(upload_id: str) -> dict | None:
    """An upload from before a restart, if its parts file and state are still here."""
    if not _UPLOAD_ID_RE.fullmatch(upload_id or ""):
        return None
    try:
        with open(_upload_meta_path(upload_id)) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not os.path.exists(data.get("path") or ""):
        return None
    data["received"] = set(data.get("received") or [])
    data["writers"] = 0
    data["closed"] = False
    _uploads[upload_id] = data
    logger.info("Upload %s picked up again after a restart (%d/%d parts)", upload_id, len(data["received"]), data["chunks"])
    return data


def _expire_uploads() -> None:
    now = time.time()
    for uid, u in list(_uploads.items()):
        if u["writers"]:
            continue
        if now - u["touched"] > _UPLOAD_TTL_SECONDS or now - u["started"] > _UPLOAD_MAX_LIFETIME_SECONDS:
            _uploads.pop(uid, None)
            u["closed"] = True
            _safe_remove(u["path"])
            _safe_remove(_upload_meta_path(uid))


async def upload_start(request: Request):
    user = require_user(request)
    body = await _json_body(request)
    _expire_uploads()

    size = body.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ApiError(status_code=400, detail="Invalid file size.")
    limit_mb = MAX_SHELF_UPLOAD_BYTES // 1024 // 1024
    if size > MAX_SHELF_UPLOAD_BYTES:
        raise ApiError(status_code=413, detail=f"That file is over the {limit_mb}MB limit.")
    filename = safe_pdf_filename(body.get("filename"))
    title = " ".join(str(body.get("title") or "").split())[:200] or os.path.splitext(filename)[0] or "Untitled book"

    target = body.get("target") or "shelf"
    if target == "shared":
        if not subscriptions.is_admin(user["id"]):
            raise ApiError(status_code=403, detail="Only the admin can change the specialty libraries.")
        try:
            specialty = shared_library.clean_specialty_name(body.get("specialty"))
        except shared_library.SharedLibraryError as e:
            raise _shared_error(e)
        dest_dir = shared_library.BOOKS_DIR
    elif target == "shelf":
        specialty = None
        # Checked now so a free user with a full shelf isn't made to upload
        # 300 MB first; checked again at finish, which is what enforces it.
        try:
            subscriptions.check_shelf_capacity(user["id"], library.count_toward_shelf_limit(user["id"]))
        except subscriptions.ShelfLimitReached as e:
            raise ApiError(status_code=402, detail=str(e))
        dest_dir = os.path.join(user_dir(user["id"]), "library")
    else:
        raise ApiError(status_code=400, detail="Invalid upload target.")

    if sum(1 for u in _uploads.values() if u["user_id"] == user["id"]) >= _MAX_OPEN_UPLOADS_PER_USER:
        raise ApiError(status_code=429, detail="You already have uploads in progress. Wait for them to finish.")

    upload_id = uuid.uuid4().hex
    path = os.path.join(_UPLOAD_PARTS_DIR, f"{upload_id}.part")
    # Reserve the full size up front, so every chunk can be written at its
    # own offset in any order.
    with open(path, "wb") as f:
        f.truncate(size)

    _uploads[upload_id] = {
        "user_id": user["id"], "path": path, "size": size, "title": title, "target": target,
        "specialty": specialty, "received": set(), "touched": time.time(), "started": time.time(),
        "chunks": -(-size // UPLOAD_CHUNK_BYTES), "dest_dir": dest_dir, "filename": filename,
        "writers": 0, "closed": False,
    }
    await asyncio.to_thread(_save_upload_meta, upload_id, _uploads[upload_id])
    return JSONResponse({"upload_id": upload_id, "chunk_size": UPLOAD_CHUNK_BYTES, "chunks": _uploads[upload_id]["chunks"]})


def _owned_upload(request: Request, user_id: int) -> tuple[str, dict]:
    upload_id = request.path_params["upload_id"]
    up = _uploads.get(upload_id) or _load_upload(upload_id)
    if up is None or up["user_id"] != user_id:
        raise ApiError(status_code=404, detail="This upload expired. Please start it again.")
    return upload_id, up


async def upload_chunk(request: Request):
    user = require_user(request)
    _expire_uploads()
    _, up = _owned_upload(request, user["id"])
    try:
        index = int(request.query_params.get("index", ""))
    except ValueError:
        raise ApiError(status_code=400, detail="Invalid chunk index.")
    if not (0 <= index < up["chunks"]):
        raise ApiError(status_code=400, detail="Invalid chunk index.")

    offset = index * UPLOAD_CHUNK_BYTES
    expected = min(UPLOAD_CHUNK_BYTES, up["size"] - offset)
    written = 0
    buf = bytearray()

    async def write(data: bytes) -> None:
        # Checked before every write: once "finish" has closed the upload
        # (and is checking or moving the file), nothing more may land in it.
        if up["closed"]:
            raise ApiError(status_code=409, detail="This upload is already finished.")
        await asyncio.to_thread(os.pwrite, fd, data, offset + written)

    if up["closed"]:
        raise ApiError(status_code=409, detail="This upload is already finished.")
    up["writers"] += 1
    fd = os.open(up["path"], os.O_WRONLY)
    try:
        # Network pieces arrive in ~64 KB bits; they're gathered into 1 MB
        # writes so the disk isn't hit (and a thread hop isn't paid) per bit.
        async for piece in request.stream():
            if not piece:
                continue
            if written + len(buf) + len(piece) > expected:
                raise ApiError(status_code=400, detail="Chunk is larger than expected.")
            buf += piece
            if len(buf) >= _WRITE_BUFFER_BYTES:
                await write(bytes(buf))
                written += len(buf)
                buf.clear()
        if buf:
            await write(bytes(buf))
            written += len(buf)
    finally:
        os.close(fd)
        up["writers"] -= 1
    if written != expected:
        # Connection cut mid-chunk: the client retries this one chunk.
        raise ApiError(status_code=400, detail="Chunk incomplete -- please retry.")
    up["received"].add(index)
    up["touched"] = time.time()
    await asyncio.to_thread(_save_upload_meta, request.path_params["upload_id"], up)
    return JSONResponse({"ok": True, "received": len(up["received"])})


async def upload_finish(request: Request):
    user = require_user(request)
    upload_id, up = _owned_upload(request, user["id"])
    missing = up["chunks"] - len(up["received"])
    if missing:
        raise ApiError(status_code=409, detail=f"{missing} part(s) of the file are still missing. Please retry.")
    if up["writers"]:
        raise ApiError(status_code=409, detail="Part of the file is still arriving. Please retry in a moment.")
    # Closed before the first await: no chunk can be written after this, so
    # the file that is checked is exactly the file that is kept.
    up["closed"] = True
    _uploads.pop(upload_id, None)
    _safe_remove(_upload_meta_path(upload_id))
    secs = max(0.001, time.time() - up["started"])
    logger.info(
        "Upload by %s: %.1f MB in %.0f s (%.2f MB/s, %d parts)",
        user["id"], up["size"] / 2**20, secs, up["size"] / 2**20 / secs, up["chunks"],
    )

    try:
        page_count = await asyncio.to_thread(open_check, up["path"])
    except ChapterDetectionError as e:
        _safe_remove(up["path"])
        raise ApiError(status_code=400, detail=str(e))
    if page_count > MAX_SHELF_UPLOAD_PAGES:
        _safe_remove(up["path"])
        raise ApiError(status_code=400, detail=f"This PDF has {page_count} pages, over the {MAX_SHELF_UPLOAD_PAGES}-page limit.")

    # Checked: now it moves into the library (same volume, so a rename).
    try:
        os.makedirs(up["dest_dir"], exist_ok=True)
        final_path = unique_path(os.path.join(up["dest_dir"], up["filename"]))
        await asyncio.to_thread(shutil.move, up["path"], final_path)
    except OSError:
        logger.exception("Couldn't move a finished upload into place")
        _safe_remove(up["path"])
        raise ApiError(status_code=500, detail="Couldn't save the file. Please try again.")
    up["path"] = final_path

    if up["target"] == "shared":
        try:
            book_id = shared_library.add_book(up["specialty"], up["title"], up["path"], page_count, added_by=user["id"])
        except shared_library.SharedLibraryError as e:
            _safe_remove(up["path"])
            raise _shared_error(e)
        _start_shared_index(book_id)
        return JSONResponse({"book_id": book_id, "specialties": _shared_catalog()})

    # No await between this check and add_book, so two uploads finishing
    # together can't both squeeze past the free shelf limit.
    try:
        subscriptions.check_shelf_capacity(user["id"], library.count_toward_shelf_limit(user["id"]))
    except subscriptions.ShelfLimitReached as e:
        _safe_remove(up["path"])
        raise ApiError(status_code=402, detail=str(e))
    book_id = library.add_book(user["id"], up["title"], up["path"], page_count, source="shelf")
    return JSONResponse(_public_book(book_id, library.get_book(user["id"], book_id)))


def _read_app_version() -> int:
    """The app.js?v=N cache-buster in index.html -- bumped on every front-end change."""
    try:
        with open(os.path.join(_WEBAPP_DIR, "index.html"), encoding="utf-8") as f:
            m = re.search(r"app\.js\?v=(\d+)", f.read())
        return int(m.group(1)) if m else 0
    except OSError:
        return 0


async def app_version(request: Request):
    """
    Lets an already-open mini app notice that a newer version was deployed.
    Telegram keeps a mini app's page alive between opens, so without this a
    user can keep running yesterday's code for hours -- which is exactly
    how a fixed uploader went untested: every attempt still used the old one.
    """
    return JSONResponse({"version": _read_app_version()})


async def upload_cancel(request: Request):
    user = require_user(request)
    upload_id, up = _owned_upload(request, user["id"])
    _uploads.pop(upload_id, None)
    up["closed"] = True
    _safe_remove(up["path"])
    _safe_remove(_upload_meta_path(upload_id))
    return JSONResponse({"cancelled": True})


# ---------------------------------------------------------------------------
# Book covers (page 1 as an image) and renaming shared books
# ---------------------------------------------------------------------------

async def _cover_response(book_id: str, pdf_path: str, request: Request):
    size = request.query_params.get("size", "thumb")
    if size not in ("thumb", "full"):
        raise ApiError(status_code=400, detail="size must be thumb or full.")
    try:
        path = await asyncio.to_thread(covers.get_cover, book_id, pdf_path, size)
    except covers.CoverError as e:
        raise ApiError(status_code=404, detail=str(e))
    # Covers never change for a given book id, so the browser may keep them.
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


async def book_cover(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    return await _cover_response(book_id, book["pdf_path"], request)


async def shared_book_cover(request: Request):
    require_user(request)  # any signed-in user may see a shared book's cover -- see covers.py
    book_id = request.path_params["book_id"]
    book = shared_library.get_book(book_id) if shared_library.is_shared_id(book_id) else None
    if book is None:
        raise ApiError(status_code=404, detail="Book not found.")
    return await _cover_response(book_id, book["pdf_path"], request)


async def shared_rename(request: Request):
    _require_admin(request)
    body = await _json_body(request)
    try:
        title = shared_library.rename_book(request.path_params["book_id"], body.get("title"))
    except shared_library.SharedLibraryError as e:
        raise _shared_error(e)
    return JSONResponse({"title": title, "specialties": _shared_catalog()})


async def suggest_book(request: Request):
    """A customer suggests a book for a specialty library; it's saved and sent to every admin."""
    user = require_user(request)
    body = await _json_body(request)
    username = user.get("username")
    who = f"@{username}" if username else f"id {user['id']}"
    specialty = body.get("specialty") if isinstance(body.get("specialty"), str) else None
    try:
        item = shared_library.add_suggestion(user["id"], who, specialty, body.get("title"), body.get("note"))
    except shared_library.SharedLibraryError as e:
        raise ApiError(status_code=400, detail=str(e))
    text = (
        f"💡 Book suggestion from {who}"
        + (f" for {item['specialty']}" if item["specialty"] else "")
        + f":\n\n📖 {item['title']}"
        + (f"\n📝 {item['note']}" if item["note"] else "")
        + f"\n\nReply: /replyuser {user['id']} <message>"
    )
    for admin_id in ADMIN_USER_IDS:
        try:
            await tg_bot.send_message(chat_id=admin_id, text=text)
        except TelegramAPIError:
            logger.exception("Couldn't forward a book suggestion to admin %s", admin_id)
    return JSONResponse({"sent": True})


# ---------------------------------------------------------------------------
# Saved Ask topics: list, reopen, delete, export as PDF
# ---------------------------------------------------------------------------

async def ask_topics(request: Request):
    user = require_user(request)
    kind = request.query_params.get("kind") or "books"
    if kind not in ask_sessions.KINDS:
        raise ApiError(status_code=400, detail="Unknown topic kind.")
    return JSONResponse({"topics": ask_sessions.list_topics(user["id"], kind)})


def _topic_or_404(user_id: int, topic_id: str) -> dict:
    topic = ask_sessions.get_topic(user_id, topic_id) if ask_sessions.valid_id(topic_id) else None
    if topic is None:
        raise ApiError(status_code=404, detail="That saved topic wasn't found.")
    return topic


async def ask_topic(request: Request):
    user = require_user(request)
    return JSONResponse(_topic_or_404(user["id"], request.path_params["topic_id"]))


async def ask_topic_delete(request: Request):
    user = require_user(request)
    if not ask_sessions.delete_topic(user["id"], request.path_params["topic_id"]):
        raise ApiError(status_code=404, detail="That saved topic wasn't found.")
    return JSONResponse({"deleted": True})


def _qa_text(turns: list[dict]) -> str:
    """The exported body: every question, its answer, and its sources (book + page)."""
    blocks = []
    for i, t in enumerate(turns, 1):
        lines = [f"## Q{i}: {t['q']}", "", t["a"]]
        if t.get("sources"):
            lines += ["", "**Sources**"]
            for s in t["sources"]:
                if s.get("url"):
                    where = f"{s.get('title')}, {s['url']}"
                else:
                    where = f"{s.get('title')}, p.{s.get('page')}" if s.get("title") else f"p.{s.get('page')}"
                lines.append(f"[{s.get('n')}] {where} — {s.get('text', '')}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


async def ask_export(request: Request):
    """
    Sends an Ask-tab answer -- one turn, or a whole saved topic -- to the
    user's chat as a PDF (the in-app WebView can't save files itself; see
    export_answer_pdf).
    """
    user = require_user(request)
    body = await _json_body(request)
    topic = _topic_or_404(user["id"], body.get("topic_id"))
    turns = topic.get("turns", [])
    index = body.get("turn")
    if index is not None:
        if not isinstance(index, int) or isinstance(index, bool) or not (0 <= index < len(turns)):
            raise ApiError(status_code=400, detail="Invalid answer number.")
        turns = [turns[index]]
    if not turns:
        raise ApiError(status_code=400, detail="Nothing to export yet.")

    title = topic.get("title") or "Ask your books"
    try:
        pdf_bytes = await asyncio.to_thread(pdf_export.generate_text_pdf, title, _qa_text(turns))
    except Exception:
        logger.exception("Ask topic PDF export failed")
        raise ApiError(status_code=500, detail="Couldn't build the PDF. Please try again.")
    try:
        await tg_bot.send_document(
            chat_id=user["id"],
            document=BufferedInputFile(pdf_bytes, filename=safe_pdf_filename(f"{title[:60]} - Answers")),
            caption=f"📄 {'Answer' if len(turns) == 1 else f'{len(turns)} answers'} -- {title[:80]}",
        )
    except TelegramAPIError:
        logger.exception("Failed to send Ask export PDF to chat_id=%s", user["id"])
        raise ApiError(
            status_code=502,
            detail="Couldn't send that to your Telegram chat. Make sure you've started a chat with the bot, then try again.",
        )
    return JSONResponse({"sent": True})


# ---------------------------------------------------------------------
# 🌐 Web sources (admin): a server-side browser to log in to a website,
# then where the bot should search it (web_sources.py)
# ---------------------------------------------------------------------

def _require_web_admin(request: Request) -> dict:
    user = require_user(request)
    if not subscriptions.is_admin(user["id"]):
        raise ApiError(status_code=403, detail="Only the admin can manage web sources.")
    return user


async def _web_call(coro):
    try:
        return await coro
    except web_sources.WebSourceError as e:
        raise ApiError(status_code=400, detail=str(e))
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail="The website took too long to answer.")
    except Exception:
        logger.exception("Web source action failed")
        raise ApiError(status_code=500, detail="The browser had a problem. Please try again.")


def _web_site_id(request: Request) -> str:
    site_id = request.path_params["site_id"]
    if web_sources.get_site(site_id) is None:
        raise ApiError(status_code=404, detail="That website isn't saved anymore.")
    return site_id


async def web_sites(request: Request):
    _require_web_admin(request)
    return JSONResponse({"sites": web_sources.list_sites(), "live": web_sources.live_info()})


async def web_browser_open(request: Request):
    _require_web_admin(request)
    body = await _json_body(request)
    site_id = body.get("site_id")
    url = body.get("url")
    if site_id:
        site = web_sources.get_site(site_id)
        if site is None:
            raise ApiError(status_code=404, detail="That website isn't saved anymore.")
        url = url or site["start_url"]
    return JSONResponse(await _web_call(web_sources.live_start(url or "", site_id or None)))


async def web_browser_frame(request: Request):
    _require_web_admin(request)
    return JSONResponse(await _web_call(web_sources.live_frame()))


async def web_browser_action(request: Request):
    _require_web_admin(request)
    body = await _json_body(request)
    return JSONResponse(await _web_call(web_sources.live_action(body)))


async def web_browser_save(request: Request):
    _require_web_admin(request)
    body = await _json_body(request)
    name = body.get("name") if isinstance(body.get("name"), str) else None
    site = await _web_call(web_sources.live_save(name, keep_steps=body.get("keep_steps", True) is not False))
    return JSONResponse({"site": site})


async def web_browser_close(request: Request):
    _require_web_admin(request)
    await web_sources.live_close()
    return JSONResponse({"closed": True})


async def web_site_update(request: Request):
    _require_web_admin(request)
    site_id = _web_site_id(request)
    body = await _json_body(request)
    fields = {k: body[k] for k in ("name", "search_url", "link_pattern", "max_results", "enabled") if k in body}
    try:
        return JSONResponse({"site": web_sources.update_site(site_id, fields)})
    except web_sources.WebSourceError as e:
        raise ApiError(status_code=400, detail=str(e))


async def web_site_delete(request: Request):
    _require_web_admin(request)
    site_id = _web_site_id(request)
    try:
        web_sources.delete_site(site_id)
    except web_sources.WebSourceError as e:
        raise ApiError(status_code=400, detail=str(e))
    return JSONResponse({"deleted": True})


async def web_site_relogin(request: Request):
    _require_web_admin(request)
    site_id = _web_site_id(request)
    await _web_call(asyncio.wait_for(web_sources.relogin(site_id), timeout=90))
    return JSONResponse({"site": next(s for s in web_sources.list_sites() if s["site_id"] == site_id)})


async def web_site_test(request: Request):
    """Run a search the way Ask your books will, and show what came back."""
    _require_web_admin(request)
    site_id = _web_site_id(request)
    body = await _json_body(request)
    query = _text(body, "query", 200)[:200]
    if not query:
        raise ApiError(status_code=400, detail="Type something to search for.")
    results = await _web_call(asyncio.wait_for(web_sources.search(site_id, query), timeout=90))
    return JSONResponse({
        "results": [
            {"title": r["title"], "url": r["url"], "chars": len(r["text"]), "preview": r["text"][:400]}
            for r in results
        ]
    })


async def backfill_shared_chapters() -> None:
    """
    Divide every indexed specialty book that has no chapters yet -- books
    uploaded before division became automatic. Runs once at startup, one
    book at a time; a book that fails is simply left for next time.
    """
    for book_id, book in list(shared_library.list_books().items()):
        if not book.get("qa_indexed") or book.get("chapters"):
            continue
        if (get_job(book_id, "index") or {}).get("status") == "running":
            continue  # the index job divides it itself when it finishes
        try:
            chapters = await asyncio.wait_for(
                asyncio.to_thread(detect_chapters_cached, book["pdf_path"]),
                timeout=scaled_timeout(CHAPTER_DIVISION_TIMEOUT_SECONDS, book.get("page_count") or 0),
            )
            shared_library.set_chapters(book_id, compute_chapter_ranges(chapters, book.get("page_count") or 1))
            logger.info("Divided specialty book %s into %d chapters", book_id, len(chapters))
        except Exception:
            logger.exception("Couldn't divide specialty book %s into chapters (will retry next start)", book_id)


# ---------------------------------------------------------------------------
# Accounts (accounts.py): log in / guest mode, and the admin's account list
# with each account's activity log.
# ---------------------------------------------------------------------------

async def me_endpoint(request: Request):
    user = require_user(request)
    uid = user["id"]
    username = accounts.username_for(uid)
    admin = subscriptions.is_admin(uid)
    full = has_full_access(uid)
    if admin:
        tier = "admin"
    elif username:
        tier = "account" if full else "expired"  # a paid account whose Premium has ended
    else:
        tier = "premium" if full else "guest"
    # A Stars Premium payer who hasn't made their account yet can register one.
    can_register = (
        not admin and not username and subscriptions.has_paid_premium(uid) and not accounts.paid_account_of(uid)
    )
    out = {"tier": tier, "username": username, "is_admin": admin, "full_access": full, "can_register": can_register}
    if not full:
        summary = subscriptions.usage_summary(uid)
        out["guest"] = {
            "questions_left": max(0, subscriptions.FREE_LIMITS["questions"] - summary["usage"].get("questions", 0)),
            "questions_per_day": subscriptions.FREE_LIMITS["questions"],
        }
    return JSONResponse(out)


async def login_endpoint(request: Request):
    user = require_user(request)
    body = await _json_body(request)
    try:
        name = await asyncio.to_thread(accounts.login, user["id"], body.get("username"), body.get("password"))
    except accounts.AccountError as e:
        raise ApiError(status_code=400, detail=str(e))
    return JSONResponse({"username": name})


async def register_endpoint(request: Request):
    """A Premium (Stars) payer chooses their own username and password; the account is linked at once."""
    user = require_user(request)
    if not subscriptions.has_paid_premium(user["id"]):
        raise ApiError(status_code=403, detail="Creating your own account comes with a Premium plan (⭐ My Plan in the bot).")
    body = await _json_body(request)
    try:
        name = await asyncio.to_thread(accounts.register, user["id"], body.get("username"), body.get("password"))
    except accounts.AccountError as e:
        raise ApiError(status_code=400, detail=str(e))
    return JSONResponse({"username": name})


async def logout_endpoint(request: Request):
    user = require_user(request)
    accounts.logout(user["id"])
    return JSONResponse({"ok": True})


def _account_call(fn, *args):
    try:
        return fn(*args)
    except accounts.AccountError as e:
        raise ApiError(status_code=400, detail=str(e))


async def admin_accounts_list(request: Request):
    _require_admin(request)
    return JSONResponse({"accounts": accounts.list_accounts()})


async def admin_account_create(request: Request):
    _require_admin(request)
    body = await _json_body(request)
    name, password = await asyncio.to_thread(
        _account_call, accounts.create_account, body.get("username"), (body.get("password") or None), _text(body, "note", 200)
    )
    return JSONResponse({"username": name, "password": password})


async def admin_account_password(request: Request):
    _require_admin(request)
    body = await _json_body(request)
    password = await asyncio.to_thread(_account_call, accounts.reset_password, request.path_params["username"], body.get("password") or None)
    return JSONResponse({"password": password})


async def admin_account_unlink(request: Request):
    _require_admin(request)
    _account_call(accounts.unlink, request.path_params["username"])
    return JSONResponse({"ok": True})


async def admin_account_disable(request: Request):
    _require_admin(request)
    body = await _json_body(request)
    _account_call(accounts.set_disabled, request.path_params["username"], bool(body.get("disabled")))
    return JSONResponse({"ok": True})


async def admin_account_delete(request: Request):
    _require_admin(request)
    _account_call(accounts.delete_account, request.path_params["username"])
    return JSONResponse({"ok": True})


async def admin_account_log(request: Request):
    _require_admin(request)
    return JSONResponse(await asyncio.to_thread(_account_call, accounts.read_log, request.path_params["username"]))



routes = [
    Route("/api/books", list_books, methods=["GET"]),
    Route("/api/upload", upload_book, methods=["POST"]),
    Route("/api/version", app_version, methods=["GET"]),
    Route("/api/uploads", upload_start, methods=["POST"]),
    Route("/api/uploads/{upload_id}/chunk", upload_chunk, methods=["PUT"]),
    Route("/api/uploads/{upload_id}/finish", upload_finish, methods=["POST"]),
    Route("/api/uploads/{upload_id}", upload_cancel, methods=["DELETE"]),
    Route("/api/books/{book_id}", get_book, methods=["GET"]),
    Route("/api/books/{book_id}", delete_book, methods=["DELETE"]),
    Route("/api/books/{book_id}/rename", rename_book, methods=["POST"]),
    Route("/api/books/{book_id}/cover", set_cover_color, methods=["POST"]),
    Route("/api/books/{book_id}/bookmark", _full_only(set_bookmark, "Saving bookmarks"), methods=["POST"]),
    Route("/api/books/{book_id}/file", get_book_file, methods=["GET"]),
    Route("/api/books/{book_id}/jobs/{job_type}", get_job_status, methods=["GET"]),
    Route("/api/books/{book_id}/chapters", divide_into_chapters, methods=["POST"]),
    Route("/api/books/{book_id}/chapters/send", _full_only(send_chapter_files, "Sending chapter files"), methods=["POST"]),
    Route("/api/books/{book_id}/summarize", summarize, methods=["POST"]),
    Route("/api/books/{book_id}/summarize/export", _full_only(export_summary_pdf, "Exporting"), methods=["POST"]),
    Route("/api/books/{book_id}/index", index_book, methods=["POST"]),
    Route("/api/books/{book_id}/ask", ask_book, methods=["POST"]),
    Route("/api/books/{book_id}/ask/export", _full_only(export_answer_pdf, "Exporting"), methods=["POST"]),
    Route("/api/books/{book_id}/qa/sessions", list_qa_sessions_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/qa/sessions/{session_id}", get_qa_session_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/quiz", create_quiz, methods=["POST"]),
    Route("/api/books/{book_id}/quiz/attempts", save_quiz_attempt_endpoint, methods=["POST"]),
    Route("/api/books/{book_id}/quiz/attempts", list_quiz_attempts_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/quiz/attempts/{attempt_id}", get_quiz_attempt_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/flashcards", list_flashcards_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/flashcards", delete_flashcards_endpoint, methods=["DELETE"]),
    Route("/api/books/{book_id}/flashcards/generate", generate_flashcards_endpoint, methods=["POST"]),
    Route("/api/books/{book_id}/flashcards/review", review_flashcard_endpoint, methods=["POST"]),
    Route("/api/books/{book_id}/flashcards/export", _full_only(export_flashcards_endpoint, "Exporting"), methods=["POST"]),
    Route("/api/books/{book_id}/notes", list_notes_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/notes", _full_only(add_note_endpoint, "Saving notes"), methods=["POST"]),
    Route("/api/books/{book_id}/notes/{note_id}", delete_note_endpoint, methods=["DELETE"]),
    Route("/api/books/{book_id}/highlights", list_highlights_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/highlights", _full_only(add_highlight_endpoint, "Highlighting"), methods=["POST"]),
    Route("/api/books/{book_id}/highlights/export", _full_only(export_highlights_pdf, "Exporting"), methods=["POST"]),
    Route("/api/books/{book_id}/highlights/{hl_id}", _full_only(update_highlight_endpoint, "Highlighting"), methods=["PATCH"]),
    Route("/api/books/{book_id}/highlights/{hl_id}", delete_highlight_endpoint, methods=["DELETE"]),
    Route("/api/books/{book_id}/search", search_book_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/explain", explain_selection_endpoint, methods=["POST"]),
    Route("/api/books/{book_id}/flashcards/from-selection", flashcards_from_selection_endpoint, methods=["POST"]),
    Route("/api/request-book", request_book_endpoint, methods=["POST"]),
    Route("/api/drugs/search", drug_search, methods=["GET"]),
    Route("/api/drugs/resolve", drug_resolve, methods=["POST"]),
    Route("/api/drugs/interactions", drug_interactions, methods=["POST"]),
    Route("/api/drugs/ask", drug_ask, methods=["POST"]),
    Route("/api/drugs/websites", drug_websites, methods=["GET"]),
    Route("/api/me", me_endpoint, methods=["GET"]),
    Route("/api/account/login", login_endpoint, methods=["POST"]),
    Route("/api/account/logout", logout_endpoint, methods=["POST"]),
    Route("/api/account/register", register_endpoint, methods=["POST"]),
    Route("/api/admin/accounts", admin_accounts_list, methods=["GET"]),
    Route("/api/admin/accounts", admin_account_create, methods=["POST"]),
    Route("/api/admin/accounts/{username}/password", admin_account_password, methods=["POST"]),
    Route("/api/admin/accounts/{username}/unlink", admin_account_unlink, methods=["POST"]),
    Route("/api/admin/accounts/{username}/disable", admin_account_disable, methods=["POST"]),
    Route("/api/admin/accounts/{username}/log", admin_account_log, methods=["GET"]),
    Route("/api/admin/accounts/{username}", admin_account_delete, methods=["DELETE"]),
    Route("/api/ask/library", ask_library, methods=["GET"]),
    Route("/api/ask/multi", ask_multi, methods=["POST"]),
    Route("/api/ask/topics", ask_topics, methods=["GET"]),
    Route("/api/ask/topics/{topic_id}", ask_topic, methods=["GET"]),
    Route("/api/ask/topics/{topic_id}", ask_topic_delete, methods=["DELETE"]),
    Route("/api/ask/export", _full_only(ask_export, "Exporting"), methods=["POST"]),
    Route("/api/admin/web/sites", web_sites, methods=["GET"]),
    Route("/api/admin/web/sites/{site_id}", web_site_update, methods=["POST"]),
    Route("/api/admin/web/sites/{site_id}", web_site_delete, methods=["DELETE"]),
    Route("/api/admin/web/sites/{site_id}/relogin", web_site_relogin, methods=["POST"]),
    Route("/api/admin/web/sites/{site_id}/test", web_site_test, methods=["POST"]),
    Route("/api/admin/web/browser/open", web_browser_open, methods=["POST"]),
    Route("/api/admin/web/browser/frame", web_browser_frame, methods=["GET"]),
    Route("/api/admin/web/browser/action", web_browser_action, methods=["POST"]),
    Route("/api/admin/web/browser/save", web_browser_save, methods=["POST"]),
    Route("/api/admin/web/browser/close", web_browser_close, methods=["POST"]),
    Route("/api/suggest-book", suggest_book, methods=["POST"]),
    Route("/api/shared/specialties", shared_add_specialty, methods=["POST"]),
    Route("/api/shared/specialties/remove", shared_remove_specialty, methods=["POST"]),
    Route("/api/shared/upload", shared_upload, methods=["POST"]),
    Route("/api/shared/publish", shared_publish, methods=["POST"]),
    Route("/api/shared/books/{book_id}/index", shared_reindex, methods=["POST"]),
    Route("/api/shared/books/{book_id}", shared_delete_book, methods=["DELETE"]),
    Route("/api/shared/books/{book_id}/rename", shared_rename, methods=["POST"]),
    Route("/api/shared/books/{book_id}/cover", shared_book_cover, methods=["GET"]),
    Route("/api/books/{book_id}/cover", book_cover, methods=["GET"]),
    Route("/api/lexidrug/status", lexidrug_status, methods=["GET"]),
    Route("/api/lexidrug/open", lexidrug_open, methods=["POST"]),
    Route("/api/lexidrug/ask", lexidrug_ask, methods=["POST"]),
    Mount("/webapp", app=StaticFiles(directory=_WEBAPP_DIR, html=True), name="webapp"),
]


# Every request body is limited -- the JSON ones were read whole into
# memory with no cap (a multi-GB "note" was accepted). Uploads have their
# own per-chunk and per-file limits and are exempt.
_MAX_BODY_BYTES = 2 * 1024 * 1024
_UNLIMITED_BODY_PATHS = re.compile(r"^/api/(uploads/[^/]+/chunk|upload|shared/upload)$")


class _BodyTooLarge(Exception):
    pass


class AccountLogMiddleware:
    """
    Every /api request made by a logged-in account goes into that account's
    activity log: endpoint, status, time taken, and the error message of a
    failed one. A request that crashes is logged as a 500 before the error
    goes on to the server's own handling.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith("/api/"):
            return await self.app(scope, receive, send)
        started = time.monotonic()
        status = {"code": 500}

        async def capture(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        crashed = None
        try:
            await self.app(scope, receive, capture)
        except Exception as e:
            crashed = e
            raise
        finally:
            state = scope.get("state") or {}
            uid = state.get("user_id")
            if uid is not None:
                try:
                    username = accounts.username_for(uid)
                    if username:
                        detail = state.get("error_detail")
                        if crashed is not None:
                            detail = f"Server error: {type(crashed).__name__}"
                        accounts.log_request(
                            username, scope.get("method", ""), path, status["code"],
                            (time.monotonic() - started) * 1000, detail if isinstance(detail, str) else None,
                        )
                except Exception:
                    logger.exception("Couldn't write the account activity log")


class BodySizeLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or _UNLIMITED_BODY_PATHS.match(scope.get("path", "")):
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    too_big = int(value) > _MAX_BODY_BYTES
                except ValueError:
                    too_big = True
                if too_big:
                    return await self._reject(send)
        received = 0
        started = False

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > _MAX_BODY_BYTES:
                    raise _BodyTooLarge()
            return message

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await self._reject(send)

    @staticmethod
    async def _reject(send):
        body = b'{"detail":"That request is too large."}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """
    Forces every /webapp/* response (index.html, style.css, app.js) to be
    treated as always-stale by any standards-compliant HTTP cache.

    Added after a real deploy where a CSS/JS change (a new shelf
    background, a new button) was pushed to Railway and confirmed correct
    on disk, but Telegram's in-app WebView kept showing the OLD version
    even after the user closed and reopened the Book Shelf mini app --
    Starlette's default StaticFiles only sets ETag/Last-Modified, which
    relies on the client bothering to revalidate rather than serving
    straight from its cache, and Telegram's WebView (and some mobile
    browsers generally) has been reported to skip that and just reuse
    whatever it cached from the URL before. This can't force Telegram's
    own internal caching to behave, but it removes every OTHER layer
    (a browser disk cache, an intermediary proxy/CDN) as a possible cause,
    and is the standard fix for "I redeployed but nothing changed" issues
    with Telegram Mini Apps. See webapp/index.html's `?v=` query strings
    on style.css/app.js for the other half of this fix -- a version bump
    there changes the URL itself, which busts a cache no header can.
    """
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith("/webapp/") or request.url.path == "/webapp":
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            # Scripts only from this server and Telegram's SDK, and no eval:
            # an injected attribute or a hostile PDF font can't run code, and
            # nothing can be sent anywhere but back here. No frame-ancestors:
            # Telegram's web clients show the app in an iframe.
            response.headers["Content-Security-Policy"] = _WEBAPP_CSP
        return response


_WEBAPP_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://telegram.org",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self'",
    "connect-src 'self'",
    "worker-src 'self' blob:",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
])


app = Starlette(
    routes=routes,
    exception_handlers={HTTPException: _api_error_handler},
    middleware=[Middleware(AccountLogMiddleware), Middleware(BodySizeLimitMiddleware), Middleware(NoCacheStaticMiddleware)],
)

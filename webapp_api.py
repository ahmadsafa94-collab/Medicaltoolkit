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
import logging
import os
import re
import shutil
import time
import uuid

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

import chapter_ai
import library
import pdf_export
import pdf_qa
import quiz_ai
from bot_instance import bot as tg_bot
from chapter_flow import build_chapter_ai_kb
from config import (
    ANTHROPIC_API_KEY,  # noqa: F401 -- imported so a missing key fails fast at startup, same as bot.py
    ASK_TIMEOUT_SECONDS,
    BOOK_SUMMARY_TIMEOUT_SECONDS,
    CHAPTER_DIVISION_TIMEOUT_SECONDS,
    CHAPTER_SUMMARY_TIMEOUT_SECONDS,
    MAX_PAGES_PER_PASS,
    MAX_SHELF_UPLOAD_BYTES,
    MAX_SHELF_UPLOAD_PAGES,
    QA_INDEXING_TIMEOUT_SECONDS,
    QUIZ_GENERATION_TIMEOUT_SECONDS,
    SEND_CHAPTER_FILES_TIMEOUT_SECONDS,
)
from paths import safe_pdf_filename, unique_path, user_dir
from pdf_processor import (
    ChapterDetectionError,
    compute_chapter_ranges,
    count_pages,
    detect_chapters,
    extract_page_previews,
    split_pdf_by_chapters,
)
from telegram_helpers import send_documents_by_chat_id
from webapp_auth import AuthError, validate_init_data

logger = logging.getLogger(__name__)


class ApiError(HTTPException):
    """Same as Starlette's HTTPException, just spelled locally for readability at call sites."""


async def _api_error_handler(request: Request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def require_user(request: Request) -> dict:
    init_data = request.headers.get("x-telegram-init-data", "")
    try:
        return validate_init_data(init_data)
    except AuthError as e:
        raise ApiError(status_code=401, detail=str(e))


# ---------------------------------------------------------------------------
# In-memory background job tracking (single process -- see module docstring)
# ---------------------------------------------------------------------------

_jobs: dict[tuple[str, str], dict] = {}


def _job_key(book_id: str, job_type: str) -> tuple[str, str]:
    return (book_id, job_type)


def start_job(book_id: str, job_type: str, coro) -> bool:
    """
    Run `coro` (an awaitable, not yet started) as a background task, tracked
    in _jobs so the frontend can poll instead of holding a connection open.
    Returns False without starting anything if a job of this type is
    already running for this book (the frontend should disable the
    triggering button while status == "running").
    """
    key = _job_key(book_id, job_type)
    if _jobs.get(key, {}).get("status") == "running":
        coro.close()
        return False
    _jobs[key] = {"status": "running", "result": None, "error": None, "progress": None, "started_at": time.time()}

    async def runner():
        try:
            result = await coro
            _jobs[key] = {"status": "done", "result": result, "error": None, "progress": None}
        except Exception as e:
            logger.exception("Background job %s failed", key)
            _jobs[key] = {"status": "error", "result": None, "error": str(e), "progress": None}

    asyncio.create_task(runner())
    return True


def set_job_progress(book_id: str, job_type: str, progress) -> None:
    key = _job_key(book_id, job_type)
    if key in _jobs:
        _jobs[key]["progress"] = progress


def get_job(book_id: str, job_type: str) -> dict | None:
    return _jobs.get(_job_key(book_id, job_type))


# ---------------------------------------------------------------------------
# Books: list / upload / detail / file
# ---------------------------------------------------------------------------

def _book_or_404(user_id: int, book_id: str) -> dict:
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
    books = library.list_books(user["id"])
    return JSONResponse({"books": [_public_book(bid, b) for bid, b in books.items()]})


async def get_book(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)
    return JSONResponse(_public_book(book_id, book))


async def rename_book(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    _book_or_404(user["id"], book_id)

    try:
        body = await request.json()
    except Exception:
        raise ApiError(status_code=400, detail="Invalid JSON body.")

    new_title = (body.get("title") or "").strip()
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

    try:
        body = await request.json()
    except Exception:
        raise ApiError(status_code=400, detail="Invalid JSON body.")

    color = body.get("color")
    if color is not None and not _VALID_HEX_COLOR.match(color):
        raise ApiError(status_code=400, detail="Color must be a hex value like '#8a5a3c', or null to reset.")

    library.set_cover_color(user["id"], book_id, color)
    return JSONResponse(_public_book(book_id, library.get_book(user["id"], book_id)))


async def delete_book(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    library.remove_book(user["id"], book_id, delete_file=True)
    pdf_qa.delete_index(book_id)
    library.delete_qa_sessions_for_book(user["id"], book_id)
    library.delete_quiz_attempts_for_book(user["id"], book_id)
    return JSONResponse({"deleted": True, "book_id": book_id})


async def set_bookmark(request: Request):
    user = require_user(request)
    book_id = request.path_params["book_id"]
    book = _book_or_404(user["id"], book_id)

    try:
        body = await request.json()
    except Exception:
        raise ApiError(status_code=400, detail="Invalid JSON body.")

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

    # Cheap early rejection when the client honestly reports Content-Length
    # (defense in depth only -- a missing/spoofed header falls through to
    # the chunked read-and-count loop below, which is the real guarantee).
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_SHELF_UPLOAD_BYTES + 1_000_000:
        raise ApiError(status_code=413, detail=f"That file is over the {MAX_SHELF_UPLOAD_BYTES // 1024 // 1024}MB limit.")

    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise ApiError(status_code=400, detail="No file uploaded (expected a 'file' form field).")
    title_field = form.get("title")

    library_dir = os.path.join(user_dir(user_id), "library")
    os.makedirs(library_dir, exist_ok=True)
    dest_path = unique_path(os.path.join(library_dir, safe_pdf_filename(upload.filename)))

    total = 0
    try:
        with open(dest_path, "wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_SHELF_UPLOAD_BYTES:
                    raise ApiError(
                        status_code=413,
                        detail=f"That file is over the {MAX_SHELF_UPLOAD_BYTES // 1024 // 1024}MB limit.",
                    )
                out.write(chunk)
    except ApiError:
        _safe_remove(dest_path)
        raise
    except Exception as e:
        _safe_remove(dest_path)
        raise ApiError(status_code=400, detail=f"Upload failed: {e}")

    if total == 0:
        _safe_remove(dest_path)
        raise ApiError(status_code=400, detail="That file was empty.")

    try:
        page_count = await asyncio.to_thread(count_pages, dest_path)
    except Exception as e:
        _safe_remove(dest_path)
        raise ApiError(status_code=400, detail=f"Couldn't read this as a PDF: {e}")

    if page_count > MAX_SHELF_UPLOAD_PAGES:
        _safe_remove(dest_path)
        raise ApiError(
            status_code=400,
            detail=f"This PDF has {page_count} pages, over the {MAX_SHELF_UPLOAD_PAGES}-page limit.",
        )

    book_title = (title_field or "").strip() or os.path.splitext(safe_pdf_filename(upload.filename))[0] or "Untitled book"
    book_id = library.add_book(user_id, book_title, dest_path, page_count, source="shelf")
    return JSONResponse(_public_book(book_id, library.get_book(user_id, book_id)))


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
        start = int(start_s) if start_s else 0
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

    page_count = await asyncio.to_thread(count_pages, pdf_path)
    if page_count > MAX_PAGES_PER_PASS:
        raise ChapterDetectionError(
            f"This book has {page_count} pages, over the {MAX_PAGES_PER_PASS}-page single-pass limit."
        )
    previews = await asyncio.to_thread(extract_page_previews, pdf_path, 350, progress)
    chapters = await asyncio.to_thread(detect_chapters, previews)
    ranges = compute_chapter_ranges(chapters, page_count)
    library.set_chapters(user_id, book_id, ranges)
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
                timeout=CHAPTER_DIVISION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            library.set_chapters_error(user["id"], book_id, "Chapter detection took too long and timed out.")
            raise
        except ChapterDetectionError as e:
            library.set_chapters_error(user["id"], book_id, str(e))
            raise
        except Exception as e:
            library.set_chapters_error(user["id"], book_id, f"Unexpected error: {e}")
            raise

    started = start_job(book_id, "chapters", job())
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
            except Exception as e:
                raise RuntimeError(f"Couldn't split this book into chapter files: {e}")

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

    try:
        body = await request.json()
    except Exception:
        raise ApiError(status_code=400, detail="Invalid JSON body.")

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
                    asyncio.to_thread(chapter_ai.summarize_chapter_full, chapter["title"], text, hard_truncated),
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
                        chapter_ai.summarize_whole_book, book["title"], book["pdf_path"], chapters, progress
                    ),
                    timeout=BOOK_SUMMARY_TIMEOUT_SECONDS,
                )
            except chapter_ai.ChapterAIError as e:
                raise RuntimeError(str(e))

        job_type = "summary:book"

    started = start_job(book_id, job_type, job())
    return JSONResponse({"started": started, "job_type": job_type})


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

    async def job():
        def progress(text):
            set_job_progress(book_id, "index", text)

        try:
            num_chunks = await asyncio.wait_for(
                pdf_qa.build_index(book["pdf_path"], book_id, book["title"], progress_cb=_async_progress(progress)),
                timeout=QA_INDEXING_TIMEOUT_SECONDS,
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

    try:
        body = await request.json()
    except Exception:
        raise ApiError(status_code=400, detail="Invalid JSON body.")
    question = (body.get("question") or "").strip()
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
        history.append({"question": q, "answer": a})

    try:
        result = await asyncio.wait_for(
            pdf_qa.answer_question(book_id, question, history=history), timeout=ASK_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        raise ApiError(status_code=504, detail="That took too long. Please try again.")
    except pdf_qa.IndexingError as e:
        raise ApiError(status_code=409, detail=str(e))
    except ApiError:
        raise
    except Exception:
        logger.exception("Book Q&A failed for book_id=%s", book_id)
        raise ApiError(status_code=500, detail="Something went wrong answering that. Please try again.")

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

    try:
        body = await request.json()
    except Exception:
        raise ApiError(status_code=400, detail="Invalid JSON body.")

    question = (body.get("question") or "").strip()
    answer = (body.get("answer") or "").strip()
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

    try:
        body = await request.json()
    except Exception:
        raise ApiError(status_code=400, detail="Invalid JSON body.")

    chapter_indices = body.get("chapter_indices")
    difficulty = body.get("difficulty")
    num_questions = body.get("num_questions")

    if not isinstance(chapter_indices, list) or not chapter_indices:
        raise ApiError(status_code=400, detail="Select at least one chapter.")
    try:
        selected = [chapters[i] for i in chapter_indices]
    except (IndexError, TypeError):
        raise ApiError(status_code=400, detail="Invalid chapter_indices.")

    async def job():
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    quiz_ai.generate_quiz, book["title"], book["pdf_path"], selected, difficulty, num_questions
                ),
                timeout=QUIZ_GENERATION_TIMEOUT_SECONDS,
            )
        except quiz_ai.QuizError as e:
            raise RuntimeError(str(e))

    started = start_job(book_id, "quiz", job())
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

    try:
        body = await request.json()
    except Exception:
        raise ApiError(status_code=400, detail="Invalid JSON body.")

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
# App wiring
# ---------------------------------------------------------------------------

_WEBAPP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp")

routes = [
    Route("/api/books", list_books, methods=["GET"]),
    Route("/api/upload", upload_book, methods=["POST"]),
    Route("/api/books/{book_id}", get_book, methods=["GET"]),
    Route("/api/books/{book_id}", delete_book, methods=["DELETE"]),
    Route("/api/books/{book_id}/rename", rename_book, methods=["POST"]),
    Route("/api/books/{book_id}/cover", set_cover_color, methods=["POST"]),
    Route("/api/books/{book_id}/bookmark", set_bookmark, methods=["POST"]),
    Route("/api/books/{book_id}/file", get_book_file, methods=["GET"]),
    Route("/api/books/{book_id}/jobs/{job_type}", get_job_status, methods=["GET"]),
    Route("/api/books/{book_id}/chapters", divide_into_chapters, methods=["POST"]),
    Route("/api/books/{book_id}/chapters/send", send_chapter_files, methods=["POST"]),
    Route("/api/books/{book_id}/summarize", summarize, methods=["POST"]),
    Route("/api/books/{book_id}/index", index_book, methods=["POST"]),
    Route("/api/books/{book_id}/ask", ask_book, methods=["POST"]),
    Route("/api/books/{book_id}/ask/export", export_answer_pdf, methods=["POST"]),
    Route("/api/books/{book_id}/qa/sessions", list_qa_sessions_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/qa/sessions/{session_id}", get_qa_session_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/quiz", create_quiz, methods=["POST"]),
    Route("/api/books/{book_id}/quiz/attempts", save_quiz_attempt_endpoint, methods=["POST"]),
    Route("/api/books/{book_id}/quiz/attempts", list_quiz_attempts_endpoint, methods=["GET"]),
    Route("/api/books/{book_id}/quiz/attempts/{attempt_id}", get_quiz_attempt_endpoint, methods=["GET"]),
    Mount("/webapp", app=StaticFiles(directory=_WEBAPP_DIR, html=True), name="webapp"),
]

app = Starlette(routes=routes, exception_handlers={HTTPException: _api_error_handler})

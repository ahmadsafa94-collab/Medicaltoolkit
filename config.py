import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
VOYAGE_API_KEY = os.environ["VOYAGE_API_KEY"]

# Public HTTPS URL the Book Shelf mini app is served from, e.g.
# "https://yourapp.herokuapp.com/webapp/" -- this is what gets registered
# with @BotFather as the bot's Menu Button / Mini App URL, and what the
# 📚 Book Shelf keyboard button opens. Telegram REQUIRES this to be a real
# HTTPS URL; it will not open a plain http:// or localhost address.
WEBAPP_URL = os.environ.get("WEBAPP_URL", "")

# Port the merged bot+webapp process listens on. Hosting platforms that
# assign a dynamic port (Heroku, Render, etc.) set $PORT themselves.
PORT = int(os.environ.get("PORT", 8000))

# Where uploaded PDFs and split chapters get stored, per user
STORAGE_DIR = os.environ.get("STORAGE_DIR", "./storage")

# Claude model to use for chapter-boundary detection and Q&A answers
CLAUDE_MODEL = "claude-sonnet-5"

# Embedding model for "Ask my book" semantic search (voyage-4 is current as
# of Sep 2026 -- verified against Voyage's own docs). voyage-4-lite is a
# cheaper/faster alternative in the same embedding space if you want to swap.
VOYAGE_MODEL = "voyage-4"

# Safety cap: don't try to AI-split books with more pages than this in one
# shot. This bounds two different costs: the Claude API cost of the chapter-
# detection prompt (scales with page count), AND -- the more important one in
# practice -- how long pdfplumber's per-page text extraction takes, which is
# NOT just a function of file size. A short, image-heavy 8MB PDF with a
# moderate page count is usually fine; a very long or layout-complex book can
# make extraction itself take many minutes even well under a naive page cap.
# If you need to raise this for a legitimately long textbook, also consider
# raising PDF_PROCESSING_TIMEOUT_SECONDS below so it isn't cut off mid-extraction.
# Raised 400 -> 1000 at the user's request; PDF_PROCESSING_TIMEOUT_SECONDS and
# CHAPTER_DIVISION_TIMEOUT_SECONDS below were scaled up proportionally (both
# gate this same extract-previews + detect-chapters pipeline) so a long book
# that now legitimately takes longer isn't cut off mid-extraction.
MAX_PAGES_PER_PASS = 1000

# Telegram's Bot API hard-caps file downloads at 20MB for regular bots --
# there is no way to download a bigger file via bot.get_file(), so we check
# this BEFORE attempting a download and tell the user clearly, rather than
# letting the download fail deep inside aiogram with no user-facing message.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# Hard ceiling on the whole "extract pages + ask Claude for chapter
# boundaries" pipeline. Without this, a slow/complex PDF (extraction is
# local CPU work with no natural timeout of its own) or a stalled network
# call could leave the user staring at "Reading pages..." indefinitely with
# no feedback and no way to know whether it's still working. Past this many
# seconds, the upload fails with a clear message instead of hanging silently.
PDF_PROCESSING_TIMEOUT_SECONDS = 750

# Anthropic/Voyage API client timeouts (seconds). Anthropic's SDK default is
# already ~10 minutes, which is far too long to sit silently for a Telegram
# bot -- set an explicit, shorter bound so a stalled request fails fast
# enough for PDF_PROCESSING_TIMEOUT_SECONDS above to actually be meaningful.
ANTHROPIC_CLIENT_TIMEOUT_SECONDS = 120
# Voyage's client has NO default timeout at all (confirmed against Voyage's
# own docs) and no retries by default -- both matter here since indexing a
# long book makes many sequential embedding calls.
VOYAGE_CLIENT_TIMEOUT_SECONDS = 60
VOYAGE_CLIENT_MAX_RETRIES = 2

# "Ask my book" (RAG) settings
QA_INDEX_DIR = os.environ.get("QA_INDEX_DIR", "./storage/qa_indexes")
QA_CHUNK_SIZE_CHARS = 1200      # ~250-300 tokens per chunk
QA_CHUNK_OVERLAP_CHARS = 200    # keeps sentences that straddle a chunk boundary searchable from both sides
QA_TOP_K = 6                    # how many chunks to feed Claude per question
QA_EMBED_BATCH_SIZE = 100       # texts per Voyage API call (Voyage's own cap is 1000/request)
# Overall cap on the indexing pipeline (extract -> chunk -> embed -> save).
# Embedding is many sequential network calls for a long book, so this is
# intentionally more generous than PDF_PROCESSING_TIMEOUT_SECONDS.
QA_INDEXING_TIMEOUT_SECONDS = 600

# --- Book Shelf mini app settings ---

# The mini app's own upload limit is deliberately larger than the chat's
# MAX_UPLOAD_BYTES (Telegram's Bot API hard-caps a bot's file downloads at
# 20MB -- see MAX_UPLOAD_BYTES above). The mini app's uploader posts the
# file straight from the browser to OUR OWN server over plain HTTPS, never
# through Telegram's Bot API at all, so that 20MB ceiling simply doesn't
# apply there -- 200MB is a real, working limit for this path.
MAX_SHELF_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_SHELF_UPLOAD_PAGES = 1500

# Background-job timeouts for the mini app's longer-running actions,
# following the same "never let the user stare at a spinner forever"
# principle as PDF_PROCESSING_TIMEOUT_SECONDS/QA_INDEXING_TIMEOUT_SECONDS.
CHAPTER_DIVISION_TIMEOUT_SECONDS = 750
BOOK_SUMMARY_TIMEOUT_SECONDS = 900  # whole-book summaries make one Claude call per chapter -- generous on purpose
CHAPTER_SUMMARY_TIMEOUT_SECONDS = 120
QUIZ_GENERATION_TIMEOUT_SECONDS = 180
ASK_TIMEOUT_SECONDS = 30

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
# Whole-book summaries now map-reduce EACH chapter that's long enough to need
# it (see chapter_ai.summarize_chapter_full), not just make one call per
# chapter -- raised from 900 to give a book with several long chapters
# enough room to actually finish instead of timing out partway through.
BOOK_SUMMARY_TIMEOUT_SECONDS = 1500
# Raised from 120: a single chapter's summary can now be several sequential
# Claude calls (map-reduce) for a long chapter instead of always being one
# call, since it covers the WHOLE chapter rather than truncating it.
CHAPTER_SUMMARY_TIMEOUT_SECONDS = 300
QUIZ_GENERATION_TIMEOUT_SECONDS = 180
ASK_TIMEOUT_SECONDS = 30
# "Send chapter files to my chat" from the Book Shelf mini app: splits the
# book into one PDF per chapter and sends each as a Telegram document, same
# work bot.py's chat-upload flow does synchronously -- generous since it
# scales with chapter count and each document send has its own network cost.
SEND_CHAPTER_FILES_TIMEOUT_SECONDS = 300

# --- Admin / premium subscription system ---

# Telegram numeric user IDs (not usernames -- Telegram doesn't hand a bot a
# stable username, only a numeric id) allowed into the admin panel. Comma-
# separated in the env var, e.g. "111111111,222222222". A user's numeric id
# is shown to them by @userinfobot, or appears in this bot's own /whoami.
ADMIN_USER_IDS = {
    int(x) for x in os.environ.get("ADMIN_USER_IDS", "").replace(" ", "").split(",") if x
}

# Free-tier monthly caps on the Claude-token-heavy features -- see
# subscriptions.py. Everything else (dose lookup, interactions, calculators,
# glossary) stays free and uncapped since it doesn't call Claude at all.
FREE_MONTHLY_SUMMARIES = 3
FREE_MONTHLY_QUIZZES = 3
FREE_MONTHLY_QUESTIONS = 20

# ECG/lab interpretation (see ecg_lab_ai.py) are Premium-only, but every free
# user gets exactly one free trial of EACH before being asked to upgrade.
ECG_FREE_TRIALS = 1
LAB_FREE_TRIALS = 1

# Telegram Stars pricing (currency code "XTR" in sendInvoice). 1 Star was
# roughly $0.016 to purchase as of Sept 2026 -- 300/2500 Stars land close to
# a $4.99/mo, $39.99/yr price point after that conversion. See the delivered
# roadmap doc for the full cost-vs-price reasoning.
PREMIUM_MONTHLY_STARS = 300
PREMIUM_YEARLY_STARS = 2500
PREMIUM_MONTH_DAYS = 30
PREMIUM_YEAR_DAYS = 365

# Both sides of a referral get this many bonus days of Premium once the
# REFERRED user completes their first successful Stars payment.
REFERRAL_BONUS_DAYS = 30

# Claude/Voyage per-million-token USD prices, used only to estimate spend
# for the admin cost dashboard (cost_ledger.py) -- NOT sent to either API.
# Update these if Anthropic/Voyage change their published rates.
CLAUDE_INPUT_PRICE_PER_MILLION_USD = 2.0
CLAUDE_OUTPUT_PRICE_PER_MILLION_USD = 10.0
VOYAGE_PRICE_PER_MILLION_USD = 0.06

# How many of the most recent server-side exceptions the admin panel's
# "Recent errors" view keeps around (see bot.py's global_error_handler).
MAX_RECENT_ERRORS = 50

# How many of the most recent 🐞 Report a problem submissions the admin
# panel's "Reported problems" view keeps around (see admin_log.py).
MAX_RECENT_REPORTS = 100

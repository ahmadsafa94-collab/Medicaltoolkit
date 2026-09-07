import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# Where uploaded PDFs and split chapters get stored, per user
STORAGE_DIR = os.environ.get("STORAGE_DIR", "./storage")

# Claude model to use for chapter-boundary detection
CLAUDE_MODEL = "claude-sonnet-5"

# Safety cap: don't try to AI-split books with more pages than this
# in one shot (protects you from huge API bills on giant PDFs)
# At ~350 chars/page preview, 1000 pages ≈ 90k input tokens ≈ $0.18/book
# on Sonnet 5 pricing ($2/$10 per MTok) -- still cheap, but scales linearly,
# so keep this cap if you ever raise page counts much further.
MAX_PAGES_PER_PASS = 1000

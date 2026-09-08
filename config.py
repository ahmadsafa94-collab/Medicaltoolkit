import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
VOYAGE_API_KEY = os.environ["VOYAGE_API_KEY"]

# Embedding model for "Ask my book" semantic search. voyage-4 is the current
# general-purpose model (Jan 2026 release, replaced voyage-3-large). Use
# voyage-4-lite instead if you want lower cost at slightly lower quality --
# it shares the same embedding space so nothing else needs to change.
VOYAGE_MODEL = "voyage-4"

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

# "Ask my book" (RAG) settings
QA_INDEX_DIR = os.environ.get("QA_INDEX_DIR", "./storage/qa_indexes")
QA_CHUNK_SIZE_CHARS = 1200      # ~250-300 tokens per chunk
QA_CHUNK_OVERLAP_CHARS = 200    # keeps sentences that straddle a chunk boundary searchable from both sides
QA_TOP_K = 6                    # how many chunks to feed Claude per question
QA_EMBED_BATCH_SIZE = 100       # texts per Voyage API call

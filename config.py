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
MAX_PAGES_PER_PASS = 400

# Telegram's Bot API hard-caps file downloads at 20MB for regular bots --
# there is no way to download a bigger file via bot.get_file(), so we check
# this BEFORE attempting a download and tell the user clearly, rather than
# letting the download fail deep inside aiogram with no user-facing message.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

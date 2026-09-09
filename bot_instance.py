"""
Single shared aiogram Bot instance.

Pulled out of bot.py so that BOTH the chat poller (bot.py) and the Book
Shelf mini app's backend (webapp_api.py) can send messages/documents
through the same bot -- webapp_api.py needs this for features like
"send this exported PDF to my chat" and "send the split chapter files to
my chat", which happen from an HTTP request handler, not a Telegram
update handler.

This has to live in its own module rather than just being read off
bot.py's `bot` global: bot.py imports webapp_api.app at module level (to
run the mini app's ASGI server alongside dp.start_polling(bot)), so
webapp_api.py importing bot.py back would be a circular import. A tiny
leaf module with no dependency on either of them sidesteps that.
"""

from aiogram import Bot

from config import TELEGRAM_BOT_TOKEN

bot = Bot(token=TELEGRAM_BOT_TOKEN)

"""
Sends every outgoing bot message in the recipient's chosen language.

Attached to the Bot's outgoing-request chain rather than wired into each
handler: the bot sends messages from dozens of modules and hundreds of
call sites -- errors, prompts, status lines, confirmations -- and touching
them all would be a large, error-prone diff that the next new message
would immediately fall out of date with. One middleware over sendMessage
and editMessageText covers every message that exists today and every one
added later, with no handler needing to know about it.

What it deliberately does NOT translate:

  - English users. The middleware returns untouched before doing anything.
  - Anything longer than ui_text.MAX_TRANSLATABLE_CHARS. That length means
    AI-generated content (a chapter summary, an ECG interpretation, a Q&A
    answer) or verbatim FDA label text. The AI content is ALREADY written
    in the user's language by the call that produced it -- passing it
    through a second translation would be slow, expensive, and could only
    degrade it.
  - Anything it fails to translate. Every failure path sends the original
    English rather than raising: a translation outage must never stop the
    bot from talking to people.

Cost and latency are bounded by ui_text's per-language cache -- a given
message costs one Claude call the first time anyone in that language sees
it, then a dict lookup. Most of the bot's text is fixed strings, so the
hit rate climbs fast; dynamic ones (a drug name in a status line) are the
minority that keep missing.
"""

import asyncio
import logging

from aiogram.methods import EditMessageText, SendMessage

import subscriptions
import ui_text

logger = logging.getLogger(__name__)


class TranslateOutgoingMiddleware:
    """Outgoing-request middleware: rewrites `text` into the recipient's language."""

    async def __call__(self, make_request, bot, method):
        try:
            await self._translate_in_place(method)
        except Exception:
            # Nothing here is worth failing a send over -- worst case the
            # user gets the English text they would have got anyway.
            logger.exception("Outgoing message translation failed (non-fatal)")
        return await make_request(bot, method)

    async def _translate_in_place(self, method) -> None:
        if not isinstance(method, (SendMessage, EditMessageText)):
            return
        text = getattr(method, "text", None)
        chat_id = getattr(method, "chat_id", None)
        if not text or not isinstance(chat_id, int):
            return

        # In a private chat -- which is every chat this bot has -- chat_id
        # is the user's id, so it's also the key their language is stored
        # under. A group chat would have a negative id and is skipped by
        # get_language returning the default.
        language = subscriptions.get_language(chat_id)
        if ui_text.is_english(language):
            return
        if len(text) > ui_text.MAX_TRANSLATABLE_CHARS:
            return

        # Cache hit is a dict lookup, so it runs inline. A miss means a
        # blocking Claude call, which MUST go to a worker thread: this
        # middleware sits on the event loop, and blocking it here would
        # stall every other user's updates for the length of the call, not
        # just this send.
        cached = ui_text.cached_message(language, text)
        if cached:
            method.text = cached
            return

        translated = await asyncio.to_thread(ui_text.translate_message, language, text)
        if translated:
            method.text = translated


def install(bot) -> None:
    bot.session.middleware(TranslateOutgoingMiddleware())

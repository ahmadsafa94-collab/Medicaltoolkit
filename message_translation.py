"""
Sends everything the user sees in their chosen language.

Attached to the Bot's outgoing-request chain rather than wired into each
handler: the bot sends from dozens of modules and hundreds of call sites --
errors, prompts, status lines, toasts, captions, button captions -- and
touching them all would be a large, error-prone diff that the next new
message would immediately fall out of date with. One middleware covers
every surface that exists today and every one added later.

Covered: message text, edited message text, callback-query toasts and
alerts, media captions, invoice title/description, and the captions of
INLINE keyboard buttons.

Reply-keyboard (the persistent main menu) buttons are deliberately NOT
touched here. Those are translated at build time by keyboards.main_menu_kb
through ui_text.t, and keyboards.ButtonText routes a tap by looking the
label up in that same table. Translating them here instead could produce a
different wording for the same button, which would leave it looking fine
and doing nothing.

What it deliberately leaves alone:

  - English users. The middleware returns before doing any work.
  - Anything inside a ui_text.untranslated() block: AI-generated content,
    already written in the user's language by the call that produced it,
    and verbatim FDA label text, which is medical source material.
  - Anything it fails to translate. Every failure path sends the original
    English rather than raising: a translation outage must never stop the
    bot from talking to people.

Cost and latency are bounded by ui_text's per-language cache -- a string
costs one Claude call the first time anyone in that language sees it, then
a dict lookup -- and by batching: everything translatable in one outgoing
request is translated together, so a fresh keyboard is one call, not one
per button.
"""

import asyncio
import logging

from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    SendAnimation,
    SendAudio,
    SendDocument,
    SendInvoice,
    SendMessage,
    SendPhoto,
    SendVideo,
)
from aiogram.types import InlineKeyboardMarkup

import bidi_text
import subscriptions
import ui_text

logger = logging.getLogger(__name__)

_TEXT_METHODS = (SendMessage, EditMessageText)
_CAPTION_METHODS = (SendDocument, SendPhoto, SendVideo, SendAudio, SendAnimation, EditMessageCaption)
_MARKUP_METHODS = _TEXT_METHODS + _CAPTION_METHODS + (EditMessageReplyMarkup, SendInvoice)


class TranslateOutgoingMiddleware:
    """Outgoing-request middleware: rewrites user-visible strings into the recipient's language."""

    async def __call__(self, make_request, bot, method):
        try:
            await self._translate_in_place(method)
        except Exception:
            # Nothing here is worth failing a send over -- worst case the
            # user gets the English text they would have got anyway.
            logger.exception("Outgoing translation failed (non-fatal)")
        return await make_request(bot, method)

    async def _translate_in_place(self, method) -> None:
        language = self._language_for(method)
        if ui_text.is_english(language):
            return

        # Bidi marks go on regardless of untranslated(): an AI answer
        # written in Persian still contains English (PR, QRS, V1-V3, ms)
        # and still renders out of order without them. That's a rendering
        # fix, not a translation, so being marked "already final" doesn't
        # exempt it. See bidi_text.
        if ui_text.should_skip():
            self._apply_bidi(method, language)
            return

        # Gather every translatable string on this request, translate them
        # in ONE batch, then write them back. Batching matters most for
        # keyboards: a dozen buttons would otherwise be a dozen calls.
        slots: list[tuple[object, str]] = []  # (owner, attribute)
        for attr in ("text", "caption", "title", "description"):
            value = getattr(method, attr, None)
            if isinstance(value, str) and value.strip():
                slots.append((method, attr))

        markup = getattr(method, "reply_markup", None) if isinstance(method, _MARKUP_METHODS) else None
        if isinstance(markup, InlineKeyboardMarkup):
            # Work on a COPY. The method object is ours (one per request),
            # but the markup belongs to the caller and may well be sent
            # more than once -- translating it in place would leave the
            # caller holding a translated keyboard, and the next send would
            # translate the translation ("FA:FA:Stats") or miss the cache.
            markup = markup.model_copy(deep=True)
            method.reply_markup = markup
            # Inline buttons route on callback_data, so their captions are
            # free to translate -- unlike reply-keyboard buttons, whose
            # caption IS the routing key (see this module's docstring).
            for row in markup.inline_keyboard:
                for button in row:
                    if isinstance(getattr(button, "text", None), str) and button.text.strip():
                        slots.append((button, "text"))

        originals = [getattr(owner, attr) for owner, attr in slots]
        if not originals:
            return

        # A cache hit is a dict lookup, so a fully-cached request never
        # leaves the event loop. A miss means a blocking Claude call, which
        # MUST go to a worker thread: this middleware runs on the loop, and
        # blocking here would stall every other user's updates, not just
        # this send.
        if all(ui_text.cached_message(language, text) for text in originals):
            translations = {text: ui_text.cached_message(language, text) for text in originals}
        else:
            translations = await asyncio.to_thread(ui_text.translate_batch, language, originals)

        for owner, attr in slots:
            translated = translations.get(getattr(owner, attr))
            if translated:
                setattr(owner, attr, translated)

        for owner, attr in slots:
            setattr(owner, attr, bidi_text.fix(getattr(owner, attr), language))

    @staticmethod
    def _apply_bidi(method, language: str | None) -> None:
        """Bidi marks only, for content that opted out of translation."""
        if not bidi_text.is_rtl_language(language):
            return
        for attr in ("text", "caption", "title", "description"):
            value = getattr(method, attr, None)
            if isinstance(value, str) and value.strip():
                setattr(method, attr, bidi_text.fix(value, language))

    @staticmethod
    def _language_for(method) -> str | None:
        """
        The recipient's language. chat_id is the user's id in a private
        chat -- which is every chat this bot has -- but some methods carry
        no chat at all (a callback-query toast names only the query), so
        those fall back to the language recorded for the update currently
        being handled. See CurrentUserLanguageMiddleware.
        """
        chat_id = getattr(method, "chat_id", None)
        if isinstance(chat_id, int) and chat_id > 0:
            return subscriptions.get_language(chat_id)
        if isinstance(method, AnswerCallbackQuery) or chat_id is None:
            return ui_text.current_language()
        return None


class CurrentUserLanguageMiddleware:
    """
    Records whose update is being handled, so replies that don't name a
    chat -- callback-query toasts and alerts -- can still be translated.

    Registered as an outer middleware on the dispatcher, so it runs once
    per update before any handler, and stores into a ContextVar that stays
    isolated to that update's task.
    """

    async def __call__(self, handler, event, data):
        user = None
        for attr in ("message", "edited_message", "callback_query", "inline_query", "pre_checkout_query"):
            carrier = getattr(event, attr, None)
            if carrier is not None and getattr(carrier, "from_user", None) is not None:
                user = carrier.from_user
                break
        if user is None:
            return await handler(event, data)

        token = ui_text.set_current_language(subscriptions.get_language(user.id))
        try:
            return await handler(event, data)
        finally:
            ui_text.reset_current_language(token)


def install(bot) -> None:
    bot.session.middleware(TranslateOutgoingMiddleware())


def install_dispatcher(dp) -> None:
    dp.update.outer_middleware(CurrentUserLanguageMiddleware())

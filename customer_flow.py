"""
Customer-facing "⭐ My Plan" panel: current plan/usage, upgrading to Premium
via Telegram Stars, a fallback "pay another way" path straight to the admin,
payment history, referrals, and language preference.

Stars payments use Telegram's native in-app invoice flow (sendInvoice with
currency "XTR", no payment provider token needed) -- Telegram itself renders
the payment UI (Apple/Google IAP on mobile, Telegram balance on desktop), so
no card data or payment processor ever touches this bot's server. See the
delivered roadmap doc for why Stars was chosen over Stripe/card checkout for
this first version.
"""

import asyncio
import logging
import time

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import admin_log
import book_requests
import language
import library
import subscriptions
import ui_text
from config import (
    ADMIN_USER_IDS,
    WEBAPP_URL,
    PREMIUM_PLANS,
    REFERRAL_BONUS_DAYS,
)
from keyboards import cancel_kb, main_menu_kb, my_plan_kb

logger = logging.getLogger(__name__)

router = Router(name="customer_flow")

_PLANS_BY_PAYLOAD = {plan["payload"]: plan for plan in PREMIUM_PLANS}


class CustomerStates(StatesGroup):
    awaiting_contact_admin_message = State()
    awaiting_feedback_message = State()
    awaiting_support_message = State()
    awaiting_paysupport_message = State()


def _plan_summary_text(user_id: int) -> str:
    sub = subscriptions.get_status(user_id)
    premium = subscriptions.is_premium(user_id)
    usage = subscriptions.usage_summary(user_id)

    lines = ["⭐ *My Plan*", "", f"Plan: {'Premium ⭐' if premium else 'Free'}"]
    if premium and sub.get("premium_until"):
        until = time.strftime("%Y-%m-%d", time.gmtime(sub["premium_until"]))
        lines.append(f"Renews/expires: {until}")
        lines.append("")
        lines.append("Unlimited summaries, quizzes, Ask-AI, drug tools, and Book Shelf.")
    else:
        lines.append("")
        lines.append("Usage today:")
        for feature, limit in usage["limits"].items():
            used = usage["usage"].get(feature, 0)
            lines.append(f"  {subscriptions.FEATURE_LABELS[feature]}: {used}/{limit}")
        # Listed apart from the block above because it isn't a monthly
        # allowance: it's a standing cap on books currently on the shelf,
        # so deleting one frees a slot immediately (see subscriptions.py).
        # Paid 📚 Requested Books are excluded from the count, so this can
        # read "1/2" on a shelf that visibly holds more than two books --
        # spelled out below rather than leaving that looking like a bug.
        shelf_cap = subscriptions.shelf_limit(user_id)
        if shelf_cap is not None:
            counted = library.count_toward_shelf_limit(user_id)
            lines.append("")
            lines.append(f"Book Shelf: {counted}/{shelf_cap} books")
            if len(library.list_books(user_id)) > counted:
                lines.append("  (books you purchased don't count toward this)")
    lines.append("")
    lines.append(f"Language: {sub.get('language', 'English')}")
    return "\n".join(lines)


@router.message(Command("myplan"))
async def cmd_my_plan(message: Message):
    await _show_plan(message.answer, message.from_user.id)


async def _show_plan(answer_fn, user_id: int):
    premium = subscriptions.is_premium(user_id)
    await answer_fn(
        _plan_summary_text(user_id),
        parse_mode="Markdown",
        reply_markup=my_plan_kb(premium, PREMIUM_PLANS),
    )


@router.callback_query(F.data.startswith("plan:buy:"))
async def handle_buy_plan(callback: CallbackQuery):
    await callback.answer()
    payload = callback.data.split(":", 2)[2]
    plan = _PLANS_BY_PAYLOAD.get(payload)
    if plan is None:
        # Only reachable from a stale keyboard in an old message, sent when
        # a plan that has since been removed was still on offer.
        await callback.message.answer("That plan isn't available anymore -- open ⭐ My Plan for the current options.")
        return
    await _send_premium_invoice(
        callback.message, plan["payload"], f"Premium -- {plan['label']}", plan["stars"]
    )


async def _send_premium_invoice(message: Message, payload: str, title: str, stars: int):
    from bot_instance import bot as tg_bot

    try:
        await tg_bot.send_invoice(
            chat_id=message.chat.id,
            title=f"Medical Student Toolkit -- {title}",
            description="Unlimited AI summaries, quizzes, Ask-AI, drug tools, and Book Shelf.",
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=stars)],
            provider_token="",
        )
    except TelegramAPIError:
        logger.exception("Failed to send Stars invoice")
        await message.answer("Couldn't start the payment right now. Please try again in a moment.")


def _checkout_error(payload: str, user_id: int, currency: str, total_amount: int) -> str | None:
    """
    Why a pre-checkout query must be refused, as the message Telegram shows
    the user, or None to approve it.

    pre_checkout_query is the last point at which the bot can stop Telegram
    from charging, and anything that reaches successful_payment has already
    taken the user's Stars. So everything that would make the payment
    un-honourable is checked here: an invoice for a plan that's since been
    removed or repriced, or a 📚 book request that isn't this user's, isn't
    awaiting payment any more (already paid from a second invoice, declined,
    refunded) or whose invoice amount doesn't match the quote.
    """
    if currency != "XTR":
        return "This invoice isn't in Telegram Stars -- please start the payment again from ⭐ My Plan."

    if payload.startswith("bookreq_"):
        request_id = payload[len("bookreq_"):]
        record = book_requests.get_request(request_id)
        if record is None or record.get("user_id") != user_id:
            return "This book request isn't available anymore -- you haven't been charged."
        if record.get("status") != "quoted" or record.get("paid_at") or record.get("charge_id"):
            return (
                f"This book request is '{record.get('status')}', not awaiting payment -- "
                "you haven't been charged."
            )
        if total_amount != record.get("stars"):
            return "This invoice doesn't match the current quote -- please use ✅ Accept & Pay on the quote again."
        return None

    plan = _PLANS_BY_PAYLOAD.get(payload)
    if plan is None:
        return "That plan isn't available anymore -- open ⭐ My Plan for the current options."
    if total_amount != plan["stars"]:
        return "This plan's price has changed since the invoice was sent -- open ⭐ My Plan for a new one."
    return None


@router.pre_checkout_query()
async def handle_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    """
    Telegram needs an answer within 10 seconds, so an unexpected error in
    the checks is turned into a refusal (no charge) rather than left to
    time out -- which also refuses, but with no explanation for the user.
    """
    try:
        error = _checkout_error(
            pre_checkout_query.invoice_payload,
            pre_checkout_query.from_user.id,
            pre_checkout_query.currency,
            pre_checkout_query.total_amount,
        )
    except Exception:
        logger.exception("Pre-checkout validation failed for payload %s", pre_checkout_query.invoice_payload)
        error = "Something went wrong checking this payment -- you haven't been charged. Please try again."

    if error is None:
        await pre_checkout_query.answer(ok=True)
        return
    logger.info(
        "Refused pre-checkout from %s for %s: %s",
        pre_checkout_query.from_user.id, pre_checkout_query.invoice_payload, error,
    )
    await pre_checkout_query.answer(ok=False, error_message=error)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message):
    """
    Grants what a Stars payment bought, once per telegram_payment_charge_id.

    Telegram re-delivers this update if the bot didn't acknowledge it (e.g.
    a restart mid-handler), so a charge id already in the user's payment
    history is a re-delivery of a payment that's already been honoured: it's
    acknowledged by simply returning, without granting again or messaging
    the user a second "Thanks!". subscriptions.apply_payment() repeats the
    check in the same write as the grant, so it holds even if two copies
    arrive together.
    """
    payment = message.successful_payment
    user_id = message.from_user.id
    charge_id = payment.telegram_payment_charge_id
    if subscriptions.find_payment(user_id, charge_id) is not None:
        logger.info("Ignoring re-delivered successful_payment %s for user %s", charge_id, user_id)
        return

    if payment.invoice_payload.startswith("bookreq_"):
        await _handle_book_payment(message, payment)
        return

    plan = _PLANS_BY_PAYLOAD.get(payment.invoice_payload)
    days = plan["days"] if plan else None
    if days is None:
        logger.warning("Unknown invoice payload on successful_payment: %s", payment.invoice_payload)
        # Still recorded (with its charge id) so the admin can see the
        # money arrived and /refund it, and so a re-delivery stays quiet.
        subscriptions.apply_payment(
            user_id, charge_id, stars=payment.total_amount, days=0, source=f"unknown_{payment.invoice_payload}"
        )
        await message.answer(
            "Payment received, but I didn't recognize what it was for -- please use /paysupport so the "
            "admin can sort this out manually."
        )
        return

    was_first_payment = not subscriptions.has_ever_paid(user_id)
    if not subscriptions.apply_payment(user_id, charge_id, stars=payment.total_amount, days=days, source="stars"):
        logger.info("Ignoring duplicate successful_payment %s for user %s", charge_id, user_id)
        return

    referral_note = ""
    if was_first_payment:
        credited_referrer = subscriptions.maybe_credit_referral(user_id)
        if credited_referrer is not None:
            referral_note = "\n\n🎁 Your referral bonus (and your friend's) has also been applied."
            try:
                from bot_instance import bot as tg_bot
                await tg_bot.send_message(
                    chat_id=credited_referrer,
                    text="🎁 Someone you referred just went Premium -- you've been credited a free month!",
                )
            except TelegramAPIError:
                logger.warning("Failed to notify referrer %s of their bonus", credited_referrer)

    await message.answer(
        f"✅ Thanks! You're now Premium for {days} days.{referral_note}",
    )


async def _handle_book_payment(message: Message, payment) -> None:
    """
    Second half of the 📚 Request a Book flow's Stars invoice (see
    handle_book_accept below for the first half, and book_requests.py for
    the full pending -> quoted -> paid -> delivered state machine). Doesn't
    grant anything itself -- it just marks the request paid and tells the
    admin to deliver it via /deliverbook.
    """
    request_id = payment.invoice_payload[len("bookreq_"):]
    user_id = message.from_user.id
    charge_id = payment.telegram_payment_charge_id
    source = f"bookreq_{request_id}"
    record = book_requests.get_request(request_id)
    if record is None:
        logger.warning("successful_payment for unknown book request %s", request_id)
        subscriptions.apply_payment(user_id, charge_id, stars=payment.total_amount, days=0, source=source)
        await message.answer(
            "Payment received, but I couldn't find that book request -- please use /paysupport so the "
            "admin can sort this out manually."
        )
        return

    from bot_instance import bot as tg_bot

    # The request is marked paid BEFORE the payment is recorded: if the bot
    # dies between the two, the re-delivered update finds no payment entry
    # (so isn't skipped as a duplicate) and this request already carrying
    # its charge id, and just finishes the job below.
    already_marked = record.get("charge_id") == charge_id
    if not already_marked and record["status"] != "quoted":
        # pre-checkout refuses this, but two invoices for the same request
        # can both pass it before either completes. The money is taken
        # either way, so record it and hand it to the admin to refund --
        # the request itself stays with the payment that got there first.
        if not subscriptions.apply_payment(user_id, charge_id, stars=payment.total_amount, days=0, source=source):
            return
        await message.answer(
            "This book request was already paid, so this second payment will be refunded -- "
            "the admin has been told. Use /paysupport if you have questions."
        )
        for admin_id in ADMIN_USER_IDS:
            try:
                await tg_bot.send_message(
                    chat_id=admin_id,
                    text=(
                        f"⚠️ Book request {request_id} (status '{record['status']}') was paid AGAIN "
                        f"({payment.total_amount} Stars) by {record['who']} (id {user_id}).\n\n"
                        f"Refund it with: /refund {user_id} {charge_id}"
                    ),
                )
            except TelegramAPIError:
                logger.warning("Failed to notify admin %s of double payment on %s", admin_id, request_id)
        return

    if not already_marked:
        book_requests.set_paid(request_id, charge_id=charge_id)
    if not subscriptions.apply_payment(user_id, charge_id, stars=payment.total_amount, days=0, source=source):
        logger.info("Ignoring duplicate successful_payment %s for book request %s", charge_id, request_id)
        return

    await message.answer("✅ Paid! The admin will deliver your book here shortly.")

    for admin_id in ADMIN_USER_IDS:
        try:
            await tg_bot.send_message(
                chat_id=admin_id,
                text=(
                    f"💰 Book request {request_id} was just paid ({payment.total_amount} Stars) by "
                    f"{record['who']} (id {record['user_id']}).\n\n{record['url']}\n\n"
                    f"Deliver it with: /deliverbook {request_id}"
                ),
            )
        except TelegramAPIError:
            logger.warning("Failed to notify admin %s of paid book request %s", admin_id, request_id)


@router.callback_query(F.data.startswith("bookreq:accept:"))
async def handle_book_accept(callback: CallbackQuery):
    await callback.answer()
    request_id = callback.data.split(":", 2)[2]
    record = book_requests.get_request(request_id)
    if record is None or record["user_id"] != callback.from_user.id:
        await callback.message.answer("This request isn't available anymore.")
        return
    if record["status"] != "quoted":
        await callback.message.answer(f"This request is '{record['status']}', not awaiting payment.")
        return

    from bot_instance import bot as tg_bot

    try:
        await tg_bot.send_invoice(
            chat_id=callback.from_user.id,
            title="Requested book",
            description=record["url"][:255],
            payload=f"bookreq_{request_id}",
            currency="XTR",
            prices=[LabeledPrice(label="Requested book", amount=record["stars"])],
            provider_token="",
        )
    except TelegramAPIError:
        logger.exception("Failed to send book-request Stars invoice for %s", request_id)
        await callback.message.answer("Couldn't start the payment right now. Please try again in a moment.")


@router.callback_query(F.data.startswith("bookreq:decline:"))
async def handle_book_decline(callback: CallbackQuery):
    await callback.answer()
    request_id = callback.data.split(":", 2)[2]
    record = book_requests.get_request(request_id)
    if record is None or record["user_id"] != callback.from_user.id:
        await callback.message.answer("This request isn't available anymore.")
        return
    if record["status"] != "quoted":
        await callback.message.answer(f"This request is '{record['status']}' -- nothing to decline.")
        return

    book_requests.set_declined(request_id)
    await callback.message.answer("Declined -- no charge.")

    from bot_instance import bot as tg_bot

    for admin_id in ADMIN_USER_IDS:
        try:
            await tg_bot.send_message(
                chat_id=admin_id,
                text=f"❌ {record['who']} declined the ${record['price_usd']:.2f} quote for book request {request_id}.",
            )
        except TelegramAPIError:
            logger.warning("Failed to notify admin %s of declined book request %s", admin_id, request_id)


@router.callback_query(F.data == "plan:contact_admin")
async def handle_contact_admin_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not ADMIN_USER_IDS:
        await callback.message.answer("This bot doesn't have an admin contact configured yet -- please try Stars again later.")
        return
    await state.set_state(CustomerStates.awaiting_contact_admin_message)
    await callback.message.answer(
        "Type a message describing how you'd like to pay (e.g. bank transfer, another app) and the admin "
        "will get back to you here.",
        reply_markup=cancel_kb(),
    )


@router.message(Command("cancel"), CustomerStates.awaiting_contact_admin_message)
async def handle_contact_admin_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.message(CustomerStates.awaiting_contact_admin_message)
async def handle_contact_admin_send(message: Message, state: FSMContext):
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("Empty message -- not sent.")
        return

    from bot_instance import bot as tg_bot

    user = message.from_user
    who = f"@{user.username}" if getattr(user, "username", None) else f"id {user.id}"
    sent_to_any = False
    for admin_id in ADMIN_USER_IDS:
        try:
            await tg_bot.send_message(
                chat_id=admin_id,
                text=(
                    f"💳 Payment request from {who} (id {user.id}):\n\n{text}\n\n"
                    f"Reply with: /replyuser {user.id} <your message>"
                ),
            )
            sent_to_any = True
        except TelegramAPIError:
            logger.warning("Failed to forward contact-admin message to admin %s", admin_id)

    if sent_to_any:
        await message.answer("Sent to the admin -- they'll message you here directly.")
    else:
        await message.answer("Couldn't reach the admin right now -- please try Stars instead, or try again later.")


async def _prompt_feedback(answer_fn, state: FSMContext):
    if not ADMIN_USER_IDS:
        await answer_fn("This bot doesn't have an admin contact configured yet -- please try again later.")
        return
    await state.set_state(CustomerStates.awaiting_feedback_message)
    await answer_fn(
        "🐞 Describe the problem you ran into, or the feature you'd like to see, in one message -- "
        "the admin will get it and can message you back here.",
        reply_markup=cancel_kb(),
    )


@router.message(Command("feedback"))
async def cmd_feedback(message: Message, state: FSMContext):
    await _prompt_feedback(message.answer, state)


@router.message(Command("cancel"), CustomerStates.awaiting_feedback_message)
async def handle_feedback_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.message(CustomerStates.awaiting_feedback_message)
async def handle_feedback_send(message: Message, state: FSMContext):
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("Empty message -- not sent.")
        return

    from bot_instance import bot as tg_bot

    user = message.from_user
    who = f"@{user.username}" if getattr(user, "username", None) else f"id {user.id}"

    admin_log.record_report(user.id, who, text)

    sent_to_any = False
    for admin_id in ADMIN_USER_IDS:
        try:
            await tg_bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🐞 Report/suggestion from {who} (id {user.id}):\n\n{text}\n\n"
                    f"Reply with: /replyuser {user.id} <your message>"
                ),
            )
            sent_to_any = True
        except TelegramAPIError:
            logger.warning("Failed to forward feedback message to admin %s", admin_id)

    if sent_to_any:
        await message.answer("Thanks -- sent to the admin.")
    else:
        await message.answer("Couldn't reach the admin right now, but it's logged for the admin to review -- please try again later too.")


async def _prompt_support(answer_fn, state: FSMContext):
    if not ADMIN_USER_IDS:
        await answer_fn("This bot doesn't have an admin contact configured yet -- please try again later.")
        return
    await state.set_state(CustomerStates.awaiting_support_message)
    await answer_fn(
        "🆘 Send your message and the admin will read it and reply to you here directly.",
        reply_markup=cancel_kb(),
    )


@router.message(Command("support"))
async def cmd_support(message: Message, state: FSMContext):
    await _prompt_support(message.answer, state)


@router.message(Command("cancel"), CustomerStates.awaiting_support_message)
async def handle_support_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.message(CustomerStates.awaiting_support_message)
async def handle_support_send(message: Message, state: FSMContext):
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("Empty message -- not sent.")
        return

    from bot_instance import bot as tg_bot

    user = message.from_user
    who = f"@{user.username}" if getattr(user, "username", None) else f"id {user.id}"
    sent_to_any = False
    for admin_id in ADMIN_USER_IDS:
        try:
            await tg_bot.send_message(
                chat_id=admin_id,
                text=(
                    f"🆘 Support request from {who} (id {user.id}):\n\n{text}\n\n"
                    f"Reply with: /replyuser {user.id} <your message>"
                ),
            )
            sent_to_any = True
        except TelegramAPIError:
            logger.warning("Failed to forward support message to admin %s", admin_id)

    if sent_to_any:
        await message.answer("Sent to the admin -- they'll message you here directly.")
    else:
        await message.answer("Couldn't reach the admin right now -- please try again later.")


# ---------------------------------------------------------------------------
# Payment support, terms, refunds -- what Telegram's Stars rules require of a
# bot that sells digital goods: a /paysupport command, a /terms command, and
# the ability to refund (Bot API refundStarPayment).
# ---------------------------------------------------------------------------

TERMS_TEXT = (
    "📄 Terms of Service\n\n"
    "1. Premium is a digital subscription to this bot's features for the period you buy. It isn't "
    "renewed automatically -- you buy each period yourself.\n"
    "2. Payments are made in Telegram Stars through Telegram. This bot never sees your card or "
    "payment details.\n"
    "3. This bot is an AI study aid for medical students. Its summaries, quizzes, answers and "
    "calculators can be wrong, and they are NOT medical advice -- never use them to diagnose or "
    "treat a patient; check against authoritative sources and your supervisors.\n"
    "4. 📚 Requested Books are priced individually and delivered by the admin after payment.\n"
    "5. Refunds: if something went wrong with a purchase, ask through /paysupport within a "
    "reasonable time (ideally within 14 days). Refunds are given at the admin's discretion and are "
    "returned as Stars to your Telegram account; refunded Premium time is removed.\n"
    "6. Access may be suspended for abuse of the service.\n\n"
    "Questions about a payment? Use /paysupport."
)


@router.message(Command("terms"))
async def cmd_terms(message: Message):
    await message.answer(TERMS_TEXT)


@router.message(Command("paysupport"))
async def cmd_pay_support(message: Message, state: FSMContext):
    """
    Same shape as /support (one message, forwarded to every admin, answered
    with /replyuser), but in its own state so the admin sees it's about a
    payment and gets the user's recent charge ids ready to pass to /refund
    -- the one thing they'd otherwise have to ask the user for.
    """
    if not ADMIN_USER_IDS:
        await message.answer("This bot doesn't have an admin contact configured yet -- please try again later.")
        return
    await state.set_state(CustomerStates.awaiting_paysupport_message)
    await message.answer(
        "💳 Payment support\n\n"
        "Describe the problem with your payment in one message -- what you bought, roughly when, and "
        "what went wrong (e.g. paid but Premium didn't activate, charged twice, want a refund). "
        "The admin will read it and reply to you here.\n\n"
        "Refund rules are in /terms.",
        reply_markup=cancel_kb(),
    )


@router.message(Command("cancel"), CustomerStates.awaiting_paysupport_message)
async def handle_pay_support_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


def _recent_charges_text(user_id: int, limit: int = 5) -> str:
    """The user's latest Stars payments with their charge ids, for the admin's /paysupport copy."""
    payments = [
        p for p in subscriptions.get_status(user_id).get("payments", [])
        if p.get("charge_id") and p.get("source") != "refund"
    ]
    if not payments:
        return "No Stars payments with a charge id on file."
    lines = ["Recent payments:"]
    for p in sorted(payments, key=lambda p: p["ts"], reverse=True)[:limit]:
        when = time.strftime("%Y-%m-%d", time.gmtime(p["ts"]))
        refunded = " [REFUNDED]" if p.get("refunded_at") else ""
        lines.append(f"  {when} {p['stars']} Stars ({p.get('source', 'stars')}){refunded}\n    {p['charge_id']}")
    return "\n".join(lines)


@router.message(CustomerStates.awaiting_paysupport_message)
async def handle_pay_support_send(message: Message, state: FSMContext):
    await state.clear()
    text = (message.text or "").strip()
    if not text:
        await message.answer("Empty message -- not sent.")
        return

    from bot_instance import bot as tg_bot

    user = message.from_user
    who = f"@{user.username}" if getattr(user, "username", None) else f"id {user.id}"
    sent_to_any = False
    for admin_id in ADMIN_USER_IDS:
        try:
            await tg_bot.send_message(
                chat_id=admin_id,
                text=(
                    f"💳 Payment support request from {who} (id {user.id}):\n\n{text}\n\n"
                    f"{_recent_charges_text(user.id)}\n\n"
                    f"Reply with: /replyuser {user.id} <your message>\n"
                    f"Refund with: /refund {user.id} <charge_id>"
                ),
            )
            sent_to_any = True
        except TelegramAPIError:
            logger.warning("Failed to forward payment-support message to admin %s", admin_id)

    if sent_to_any:
        await message.answer("Sent to the admin -- they'll message you here directly.")
    else:
        await message.answer("Couldn't reach the admin right now -- please try again later.")


@router.message(Command("refund"))
async def cmd_refund(message: Message):
    """
    /refund <user_id> <telegram_payment_charge_id> -- admin only. Refunds a
    Stars payment through Telegram first, and only once Telegram confirms
    takes back what it bought (subscriptions.record_refund: the Premium days
    for a plan; a 📚 book request goes to "refunded"), so a refund Telegram
    rejects (wrong id, already refunded) never costs the user anything.

    A charge id that isn't in the user's history is still sent to Telegram
    -- payments from before charge ids were stored aren't on file -- but
    nothing can be taken back automatically then, and the reply says so.
    """
    if not subscriptions.is_admin(message.from_user.id):
        await message.answer("This isn't available to you.")
        return

    parts = (message.text or "").split()
    if len(parts) != 3:
        await message.answer("Usage: /refund <user_id> <telegram_payment_charge_id>")
        return
    try:
        user_id = int(parts[1])
    except ValueError:
        await message.answer("The user id must be a number. Usage: /refund <user_id> <telegram_payment_charge_id>")
        return
    charge_id = parts[2]

    original = subscriptions.find_payment(user_id, charge_id)
    if original is not None and original.get("refunded_at"):
        when = time.strftime("%Y-%m-%d", time.gmtime(original["refunded_at"]))
        await message.answer(f"That payment was already refunded on {when}.")
        return

    from bot_instance import bot as tg_bot

    try:
        await tg_bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
    except TelegramAPIError as e:
        logger.warning("refund_star_payment failed for user %s charge %s: %s", user_id, charge_id, e)
        await message.answer(f"Telegram refused the refund: {e}\n\nNothing was changed on the user's plan.")
        return

    original = subscriptions.record_refund(user_id, charge_id)
    if original is None:
        summary = (
            f"✅ Refunded charge {charge_id} for user {user_id}. That charge isn't in their payment "
            "history, so nothing was revoked automatically -- adjust their plan by hand if needed."
        )
    else:
        source = original.get("source", "")
        summary = f"✅ Refunded {original.get('stars')} Stars to user {user_id}."
        if source.startswith("bookreq_"):
            request_id = source[len("bookreq_"):]
            record = book_requests.get_request(request_id)
            # Only the payment that actually moved the request to "paid"
            # refunds the request; refunding an accidental second payment
            # leaves the first one's paid/delivered status alone.
            if record is not None and record.get("charge_id") == charge_id:
                book_requests.set_refunded(request_id)
                summary += f" Book request {request_id} is now marked refunded."
                if record.get("status") == "delivered":
                    summary += " (It was already delivered -- the PDF stays on their shelf.)"
            else:
                summary += f" Book request {request_id} was left as it was (this wasn't its paying charge)."
        elif original.get("days", 0) > 0:
            summary += f" Removed {original['days']} days of Premium."
            if not subscriptions.is_premium(user_id):
                summary += " They're now on the free plan."

    await message.answer(summary)

    try:
        await tg_bot.send_message(
            chat_id=user_id,
            text="💳 Your payment has been refunded -- the Stars are back in your Telegram account.",
        )
    except TelegramAPIError:
        logger.warning("Failed to notify user %s of their refund", user_id)


@router.callback_query(F.data == "plan:referral")
async def handle_referral(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    payload = subscriptions.get_referral_link_payload(user_id)

    from bot_instance import bot as tg_bot
    bot_info = await tg_bot.get_me()

    link = f"https://t.me/{bot_info.username}?start={payload}"
    stats = subscriptions.get_referral_stats(user_id)
    await callback.message.answer(
        "🤝 *Affiliate Program*\n\n"
        f"Share your link: {link}\n\n"
        f"Every friend who signs up through it and becomes Premium for the first time earns you BOTH "
        f"{REFERRAL_BONUS_DAYS} free days of Premium -- no limit on how many times this can happen, so "
        "the more people you bring in, the more free Premium you build up.\n\n"
        "📊 *Your stats*\n"
        f"Joined via your link: {stats['referred_count']}\n"
        f"Converted to Premium: {stats['converted_count']}\n"
        f"Bonus days earned so far: {stats['bonus_days_earned']}",
        parse_mode="Markdown",
    )


@router.callback_query(F.data == "plan:history")
async def handle_history(callback: CallbackQuery):
    await callback.answer()
    sub = subscriptions.get_status(callback.from_user.id)
    payments = sub.get("payments", [])
    if not payments:
        await callback.message.answer("🧾 No payments on file yet.")
        return
    lines = ["🧾 *Payment history*", ""]
    for p in sorted(payments, key=lambda p: p["ts"], reverse=True)[:20]:
        when = time.strftime("%Y-%m-%d", time.gmtime(p["ts"]))
        source = p.get("source", "stars")
        if source == "refund":
            lines.append(f"{when} -- refund of {-p['stars']} Stars")
        elif source.startswith("bookreq_"):
            lines.append(f"{when} -- {p['stars']} Stars (requested book)")
        else:
            lines.append(f"{when} -- {p['stars']} Stars ({p['days']} days, {source})")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")


async def _prompt_language(answer_fn):
    """Shared by the 🌐 Language main-menu button (bot.py) and the legacy plan:language callback below."""
    await answer_fn(
        "🌐 Pick the language AI-generated content (summaries, quizzes, Ask-AI, drug answers) "
        "should be written in:",
        reply_markup=language.language_picker_kb(),
    )


@router.callback_query(F.data == "plan:language")
async def handle_language_prompt(callback: CallbackQuery):
    """
    Kept after 🌐 Language moved to the main menu: My Plan messages already
    sent carry this button, and an unhandled callback just spins and dies
    with no feedback at all. New My Plan panels no longer offer it.
    """
    await callback.answer()
    await _prompt_language(callback.message.answer)


@router.callback_query(F.data.startswith("lang:set:"))
async def handle_language_set(callback: CallbackQuery):
    lang = callback.data.split(":", 2)[2]
    subscriptions.set_language(callback.from_user.id, lang)
    await callback.answer(f"Language set to {lang}.")

    # Translate the menu labels now, while the user is watching and a brief
    # wait makes sense, rather than on their next tap. Cached per language,
    # so only the first person to choose one ever waits; English is a no-op.
    if not ui_text.is_english(lang):
        status = await callback.message.answer("Changing the Language")
        # The menu labels are translated now, so the keyboard below is
        # already in the new language when it arrives.
        await asyncio.to_thread(ui_text.ensure_language, lang)
        try:
            await status.delete()
        except Exception:
            pass  # cosmetic only -- never worth failing the language change over

        # Everything else -- every fixed message and button caption in the
        # bot -- is translated in the background. Not awaited: it's a few
        # hundred strings and the user should not sit through it. Anything
        # they reach before its batch lands still translates on demand, so
        # the only difference is how long the bot feels slow for.
        asyncio.create_task(asyncio.to_thread(ui_text.warm_all_messages, lang))

    # The persistent keyboard only changes when a message carries a new one,
    # so it is re-sent here; otherwise the old language's buttons would sit
    # at the bottom of the chat until something else happened to replace them.
    await callback.message.answer(
        f"✅ The bot will now use {lang}.",
        reply_markup=main_menu_kb(
            WEBAPP_URL, is_admin=subscriptions.is_admin(callback.from_user.id), language=lang
        ),
    )


def register_customer_handlers(dp) -> None:
    dp.include_router(router)

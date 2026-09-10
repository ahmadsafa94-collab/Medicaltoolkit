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

import logging
import time

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

import language
import subscriptions
from config import ADMIN_USER_IDS, PREMIUM_MONTHLY_STARS, PREMIUM_YEARLY_STARS, PREMIUM_MONTH_DAYS, PREMIUM_YEAR_DAYS
from keyboards import my_plan_kb

logger = logging.getLogger(__name__)

router = Router(name="customer_flow")

_PLAN_PAYLOAD_TO_DAYS = {"premium_month": PREMIUM_MONTH_DAYS, "premium_year": PREMIUM_YEAR_DAYS}


class CustomerStates(StatesGroup):
    awaiting_contact_admin_message = State()
    awaiting_lab_text = State()


def _plan_summary_text(user_id: int) -> str:
    sub = subscriptions.get_status(user_id)
    premium = subscriptions.is_premium(user_id)
    usage = subscriptions.usage_summary(user_id)

    lines = ["⭐ *My Plan*", "", f"Plan: {'Premium ⭐' if premium else 'Free'}"]
    if premium and sub.get("premium_until"):
        until = time.strftime("%Y-%m-%d", time.gmtime(sub["premium_until"]))
        lines.append(f"Renews/expires: {until}")
        lines.append("")
        lines.append("Unlimited summaries, quizzes, Ask-AI, and ECG/lab interpretation.")
    else:
        lines.append("")
        lines.append("Usage this month:")
        for feature, limit in usage["limits"].items():
            used = usage["usage"].get(feature, 0)
            lines.append(f"  {subscriptions.FEATURE_LABELS[feature]}: {used}/{limit}")
        trial_used = usage["trial_used"]
        lines.append(
            f"  ECG interpretation trial: {'used' if trial_used.get('ecg') else 'available'}"
        )
        lines.append(
            f"  Lab interpretation trial: {'used' if trial_used.get('lab') else 'available'}"
        )
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
        reply_markup=my_plan_kb(premium, PREMIUM_MONTHLY_STARS, PREMIUM_YEARLY_STARS),
    )


@router.callback_query(F.data == "plan:buy:month")
async def handle_buy_month(callback: CallbackQuery):
    await callback.answer()
    await _send_premium_invoice(callback.message, "premium_month", "Premium -- 1 month", PREMIUM_MONTHLY_STARS)


@router.callback_query(F.data == "plan:buy:year")
async def handle_buy_year(callback: CallbackQuery):
    await callback.answer()
    await _send_premium_invoice(callback.message, "premium_year", "Premium -- 1 year", PREMIUM_YEARLY_STARS)


async def _send_premium_invoice(message: Message, payload: str, title: str, stars: int):
    from bot_instance import bot as tg_bot

    try:
        await tg_bot.send_invoice(
            chat_id=message.chat.id,
            title=f"Medical Student Toolkit -- {title}",
            description="Unlimited AI summaries, quizzes, Ask-AI, and ECG/lab interpretation.",
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=stars)],
            provider_token="",
        )
    except TelegramAPIError:
        logger.exception("Failed to send Stars invoice")
        await message.answer("Couldn't start the payment right now. Please try again in a moment.")


@router.pre_checkout_query()
async def handle_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    # No inventory/stock to check for a digital subscription -- always
    # approve so Telegram proceeds to actually charge the user.
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message):
    payment = message.successful_payment
    user_id = message.from_user.id
    days = _PLAN_PAYLOAD_TO_DAYS.get(payment.invoice_payload)
    if days is None:
        logger.warning("Unknown invoice payload on successful_payment: %s", payment.invoice_payload)
        await message.answer(
            "Payment received, but I didn't recognize what it was for -- please contact the admin via "
            "⭐ My Plan so this can be sorted out manually."
        )
        return

    was_first_payment = not subscriptions.has_ever_paid(user_id)
    subscriptions.grant_premium(user_id, days=days, source="stars")
    subscriptions.record_payment(user_id, stars=payment.total_amount, days=days, source="stars")

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


@router.callback_query(F.data == "plan:contact_admin")
async def handle_contact_admin_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not ADMIN_USER_IDS:
        await callback.message.answer("This bot doesn't have an admin contact configured yet -- please try Stars again later.")
        return
    await state.set_state(CustomerStates.awaiting_contact_admin_message)
    await callback.message.answer(
        "Type a message describing how you'd like to pay (e.g. bank transfer, another app) and the admin "
        "will get back to you here. /cancel to abort."
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


@router.callback_query(F.data == "plan:referral")
async def handle_referral(callback: CallbackQuery):
    await callback.answer()
    payload = subscriptions.get_referral_link_payload(callback.from_user.id)

    from bot_instance import bot as tg_bot
    bot_info = await tg_bot.get_me()

    link = f"https://t.me/{bot_info.username}?start={payload}"
    await callback.message.answer(
        "🎁 *Invite a friend*\n\n"
        f"Share this link: {link}\n\n"
        "When they sign up and become Premium for the first time, you BOTH get a free month.",
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
        lines.append(f"{when} -- {p['stars']} Stars ({p['days']} days, {p.get('source', 'stars')})")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")


@router.callback_query(F.data == "plan:language")
async def handle_language_prompt(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "🌐 Pick the language AI-generated content (summaries, quizzes, Ask-AI, ECG/lab interpretation) "
        "should be written in:",
        reply_markup=language.language_picker_kb(),
    )


@router.callback_query(F.data.startswith("lang:set:"))
async def handle_language_set(callback: CallbackQuery):
    lang = callback.data.split(":", 2)[2]
    subscriptions.set_language(callback.from_user.id, lang)
    await callback.answer(f"Language set to {lang}.")
    await callback.message.answer(f"✅ AI-generated content will now be written in {lang}.")


def register_customer_handlers(dp) -> None:
    dp.include_router(router)

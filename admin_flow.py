"""
Admin panel -- opens only for Telegram user ids listed in config.ADMIN_USER_IDS.
Every handler in this module re-checks subscriptions.is_admin() itself (not
just at the menu-open step) so a stale/forwarded callback_data from an old
message can never be replayed by a non-admin to reach an admin action.

Covers: bot-wide stats, subscription lookup/grant/revoke, a running API cost
dashboard, broadcast, block/unblock, a "test functionality" smoke-test menu,
recent server-side errors, and user-submitted 🐞 problem reports -- see
keyboards.py's admin_menu_kb() for the top-level menu these all hang off of.
"""

import asyncio
import base64
import logging
import time

from aiogram import Router, F
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import admin_log
import chapter_ai
import cost_ledger
import ecg_lab_ai
import subscriptions
from keyboards import admin_menu_kb, admin_lookup_result_kb, admin_test_menu_kb

logger = logging.getLogger(__name__)

router = Router(name="admin_flow")

# A tiny 1x1 white PNG, used only to smoke-test the vision call path end to
# end (network/model access, not image quality) -- "Test functionality"
# doesn't need a real ECG on disk to prove the plumbing works.
_TEST_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
_TEST_CHAPTER_TEXT = (
    "Chapter 1: The Cardiac Cycle\n\n"
    "The cardiac cycle consists of systole (ventricular contraction and ejection) and diastole "
    "(ventricular relaxation and filling). Normal resting heart rate is 60-100 beats per minute. "
    "The cycle is divided into isovolumetric contraction, ejection, isovolumetric relaxation, and "
    "filling phases, each corresponding to specific heart sounds and pressure-volume changes."
)


class AdminStates(StatesGroup):
    awaiting_lookup = State()
    awaiting_broadcast = State()


async def _require_admin_message(message: Message) -> bool:
    if not subscriptions.is_admin(message.from_user.id):
        await message.answer("This isn't available to you.")
        return False
    return True


async def _require_admin_callback(callback: CallbackQuery) -> bool:
    if not subscriptions.is_admin(callback.from_user.id):
        await callback.answer("This isn't available to you.", show_alert=True)
        return False
    return True


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not await _require_admin_message(message):
        return
    await state.clear()
    await message.answer("🛠 *Admin Panel*", parse_mode="Markdown", reply_markup=admin_menu_kb())


@router.callback_query(F.data == "admin:stats")
async def handle_stats(callback: CallbackQuery):
    if not await _require_admin_callback(callback):
        return
    await callback.answer()
    stats = subscriptions.get_bot_stats()
    revenue = subscriptions.get_revenue_stats()
    text = (
        "📊 *Bot-wide stats*\n\n"
        f"Total users: {stats['total_users']}\n"
        f"Premium users: {stats['premium_users']}\n"
        f"Active (7d): {stats['active_7d']}\n"
        f"Active (30d): {stats['active_30d']}\n\n"
        f"This month ({revenue['period']}): {revenue['stars_total']} Stars across {revenue['payments_count']} payment(s)"
    )
    await callback.message.answer(text, parse_mode="Markdown")


@router.callback_query(F.data == "admin:cost")
async def handle_cost(callback: CallbackQuery):
    if not await _require_admin_callback(callback):
        return
    await callback.answer()
    summary = cost_ledger.summarize_costs(days=30)
    lines = [f"💰 *API cost dashboard (last 30 days)*", "", f"Total: ${summary['total_usd']:.2f} across {summary['calls']} call(s)", ""]
    if summary["by_feature"]:
        lines.append("By feature:")
        for feature, usd in sorted(summary["by_feature"].items(), key=lambda kv: -kv[1]):
            lines.append(f"  {feature}: ${usd:.2f}")
    else:
        lines.append("_No cost data logged yet._")
    await callback.message.answer("\n".join(lines), parse_mode="Markdown")


@router.callback_query(F.data == "admin:errors")
async def handle_errors(callback: CallbackQuery):
    if not await _require_admin_callback(callback):
        return
    await callback.answer()
    errors = admin_log.recent_errors(10)
    if not errors:
        await callback.message.answer("🪵 No errors recorded recently.")
        return
    lines = ["🪵 *Recent errors* (most recent first)", ""]
    for e in errors:
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(e["ts"]))
        lines.append(f"`{when}` [{e.get('context', '?')}] {e['error']}")
    await callback.message.answer("\n".join(lines)[:4000], parse_mode="Markdown")


@router.callback_query(F.data == "admin:reports")
async def handle_reports(callback: CallbackQuery):
    if not await _require_admin_callback(callback):
        return
    await callback.answer()
    reports = admin_log.recent_reports(10)
    if not reports:
        await callback.message.answer("🐞 No problems reported recently.")
        return
    lines = ["🐞 *Reported problems* (most recent first)", ""]
    for r in reports:
        when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(r["ts"]))
        lines.append(f"`{when}` {r['who']} (id {r['user_id']}):\n{r['text']}\n")
    await callback.message.answer("\n".join(lines)[:4000], parse_mode="Markdown")


@router.callback_query(F.data == "admin:subs")
async def handle_subs_prompt(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin_callback(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.awaiting_lookup)
    await callback.message.answer("Send the Telegram user id, or @username, to look up.")


@router.message(AdminStates.awaiting_lookup)
async def handle_lookup_input(message: Message, state: FSMContext):
    if not await _require_admin_message(message):
        await state.clear()
        return
    await state.clear()

    raw = (message.text or "").strip()
    target_id = None
    if raw.startswith("@"):
        target_id = subscriptions.find_user_id_by_username(raw)
        if target_id is None:
            await message.answer(f"No known user with username {raw}. They need to have used the bot at least once.")
            return
    else:
        try:
            target_id = int(raw)
        except ValueError:
            await message.answer("Send a numeric Telegram user id, or an @username.")
            return

    await _send_lookup_result(message.answer, target_id)


async def _send_lookup_result(answer_fn, target_id: int):
    sub = subscriptions.get_status(target_id)
    premium = subscriptions.is_premium(target_id)
    lines = [f"👤 *User {target_id}*", "", f"Plan: {'Premium ⭐' if premium else 'Free'}"]
    if premium and sub.get("premium_until"):
        until = time.strftime("%Y-%m-%d", time.gmtime(sub["premium_until"]))
        lines.append(f"Premium until: {until} (source: {sub.get('premium_source')})")
    lines.append(f"Usage this month: {sub['usage']}")
    lines.append(f"ECG/Lab trials used: {sub['trial_used']}")
    lines.append(f"Blocked: {'yes' if sub.get('blocked') else 'no'}")
    lines.append(f"Payments on file: {len(sub.get('payments', []))}")
    await answer_fn("\n".join(lines), parse_mode="Markdown", reply_markup=admin_lookup_result_kb(target_id, sub.get("blocked", False)))


@router.callback_query(F.data.startswith("admin:grant:"))
async def handle_grant(callback: CallbackQuery):
    if not await _require_admin_callback(callback):
        return
    _, _, target_id_str, days_str = callback.data.split(":", 3)
    target_id, days = int(target_id_str), int(days_str)
    subscriptions.grant_premium(target_id, days=days, source="admin")
    await callback.answer(f"Granted {days} day(s) of Premium.")
    await _send_lookup_result(callback.message.answer, target_id)


@router.callback_query(F.data.startswith("admin:revoke:"))
async def handle_revoke(callback: CallbackQuery):
    if not await _require_admin_callback(callback):
        return
    target_id = int(callback.data.split(":", 2)[2])
    subscriptions.revoke_premium(target_id)
    await callback.answer("Premium revoked.")
    await _send_lookup_result(callback.message.answer, target_id)


@router.callback_query(F.data.startswith("admin:block:"))
async def handle_block_toggle(callback: CallbackQuery):
    if not await _require_admin_callback(callback):
        return
    _, _, target_id_str, action = callback.data.split(":", 3)
    target_id = int(target_id_str)
    subscriptions.set_blocked(target_id, blocked=(action == "on"))
    await callback.answer("Blocked." if action == "on" else "Unblocked.")
    await _send_lookup_result(callback.message.answer, target_id)


@router.callback_query(F.data == "admin:broadcast")
async def handle_broadcast_prompt(callback: CallbackQuery, state: FSMContext):
    if not await _require_admin_callback(callback):
        return
    await callback.answer()
    await state.set_state(AdminStates.awaiting_broadcast)
    await callback.message.answer("Send the message to broadcast to every user. /cancel to abort.")


@router.message(Command("cancel"), AdminStates.awaiting_broadcast)
async def handle_broadcast_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Broadcast cancelled.")


@router.message(AdminStates.awaiting_broadcast)
async def handle_broadcast_send(message: Message, state: FSMContext):
    if not await _require_admin_message(message):
        await state.clear()
        return
    await state.clear()
    text = message.text or ""
    if not text.strip():
        await message.answer("Empty message -- broadcast cancelled.")
        return

    from bot_instance import bot as tg_bot  # local import: avoids a circular import with bot.py at module load time

    ids = subscriptions.all_user_ids()
    sent = 0
    for uid in ids:
        try:
            await tg_bot.send_message(chat_id=uid, text=f"📢 {text}")
            sent += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
            try:
                await tg_bot.send_message(chat_id=uid, text=f"📢 {text}")
                sent += 1
            except TelegramAPIError:
                logger.warning("Broadcast failed for user %s after retry", uid)
        except TelegramAPIError:
            logger.warning("Broadcast failed for user %s (likely blocked the bot)", uid)
        await asyncio.sleep(0.05)

    await message.answer(f"Broadcast sent to {sent}/{len(ids)} user(s).")


@router.callback_query(F.data == "admin:test")
async def handle_test_menu(callback: CallbackQuery):
    if not await _require_admin_callback(callback):
        return
    await callback.answer()
    await callback.message.answer("🧪 Tap a feature to fire a live test call against it.", reply_markup=admin_test_menu_kb())


@router.callback_query(F.data.startswith("admin:testrun:"))
async def handle_test_run(callback: CallbackQuery):
    if not await _require_admin_callback(callback):
        return
    feature = callback.data.split(":", 2)[2]
    await callback.answer("Running...")
    start = time.monotonic()
    try:
        if feature == "summary":
            result = await asyncio.to_thread(chapter_ai.summarize_chapter_full, "Test Chapter", _TEST_CHAPTER_TEXT)
        elif feature == "quiz":
            result = await asyncio.to_thread(chapter_ai.quiz_chapter, "Test Chapter", _TEST_CHAPTER_TEXT, False, 2)
        elif feature == "mnemonics":
            result = await asyncio.to_thread(chapter_ai.generate_mnemonics, "Test Chapter", _TEST_CHAPTER_TEXT)
        elif feature == "lab":
            result = await asyncio.to_thread(ecg_lab_ai.interpret_lab_text, "Na 148, K 2.9, Cr 1.8")
        elif feature == "ecg":
            image_bytes = base64.b64decode(_TEST_PNG_B64)
            result = await asyncio.to_thread(ecg_lab_ai.interpret_ecg, image_bytes, "image/png")
        else:
            await callback.message.answer("Unknown test.")
            return
    except Exception as e:
        elapsed = time.monotonic() - start
        await callback.message.answer(f"❌ {feature} test FAILED after {elapsed:.1f}s: {e}")
        return

    elapsed = time.monotonic() - start
    preview = (result or "")[:300]
    await callback.message.answer(f"✅ {feature} test OK in {elapsed:.1f}s.\n\nPreview:\n{preview}")


@router.message(Command("replyuser"))
async def cmd_reply_user(message: Message):
    """
    /replyuser <user_id> <message> -- the admin's reply half of every
    user-initiated contact flow in customer_flow.py: "pay another way"
    (handle_contact_admin_send), "🐞 Report a problem" (handle_feedback_send),
    and "🆘 Support" (handle_support_send). All three forward the user's
    message to every admin and tell them to expect a reply via this exact
    command. Deliberately a plain command rather than an FSM step: an admin
    might field several of these at once, interleaved with other chat
    activity, and a stateful "who am I replying to right now" flow would
    make that awkward.
    """
    if not await _require_admin_message(message):
        return

    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Usage: /replyuser <user_id> <message>")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.answer("The first argument must be a numeric Telegram user id.")
        return

    from bot_instance import bot as tg_bot

    try:
        await tg_bot.send_message(chat_id=target_id, text=f"💬 Message from the admin:\n\n{parts[2]}")
        await message.answer(f"Sent to user {target_id}.")
    except TelegramAPIError:
        logger.exception("Failed to send admin reply to user %s", target_id)
        await message.answer(f"Couldn't reach user {target_id} (they may have blocked the bot).")


def register_admin_handlers(dp) -> None:
    dp.include_router(router)

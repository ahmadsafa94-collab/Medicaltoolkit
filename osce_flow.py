"""
Chat flow for Study Tools -> 🩺 OSCE Practice. Starting a case costs one
"quiz" quota unit (a full case, with the debrief at the end, is comparable
in depth/cost to a generated quiz) -- the back-and-forth WITHIN a case isn't
metered per message, so a student isn't punished for asking more questions,
which is the whole point of the exercise.
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import osce_ai
import subscriptions
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="osce_flow")

_GENERATION_TIMEOUT_SECONDS = 60
_NOT_A_COMMAND = F.text & ~F.text.startswith("/")


class OsceStates(StatesGroup):
    awaiting_topic = State()
    in_case = State()


def _end_case_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏁 End case (get debrief)", callback_data="osce:end")]])


@router.callback_query(F.data == "study:osce")
async def handle_study_osce(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        subscriptions.check_and_consume(callback.from_user.id, "quizzes")
    except subscriptions.QuotaExceeded as e:
        await callback.message.answer(str(e))
        return

    await state.set_state(OsceStates.awaiting_topic)
    await callback.message.answer(
        "🩺 What chief complaint would you like to practice? (e.g. 'chest pain', 'abdominal pain', "
        "'shortness of breath'). Type it, or send 'random' for a surprise case. /cancel to abort.\n\n"
        "This is a FICTIONAL practice patient for study purposes only."
    )


@router.message(Command("cancel"), OsceStates.awaiting_topic)
@router.message(Command("cancel"), OsceStates.in_case)
async def handle_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Case ended (no debrief).")


@router.message(OsceStates.awaiting_topic, _NOT_A_COMMAND)
async def handle_topic(message: Message, state: FSMContext):
    topic = message.text.strip()
    if topic.lower() == "random":
        topic = "an undifferentiated chief complaint of your choosing, typical of a general medicine OSCE station"

    language = subscriptions.get_language(message.from_user.id)
    status = await message.answer("Setting up the case...")
    try:
        opening = await asyncio.wait_for(
            asyncio.to_thread(osce_ai.start_case, topic, language), timeout=_GENERATION_TIMEOUT_SECONDS
        )
    except Exception as e:
        logger.exception("OSCE case start failed")
        await status.edit_text(f"Couldn't start the case: {e}")
        await state.clear()
        return

    history = [
        {"role": "user", "content": "(Begin the encounter now with your opening statement to the doctor.)"},
        {"role": "assistant", "content": opening},
    ]
    await state.set_state(OsceStates.in_case)
    await state.update_data(topic=topic, history=history)
    await status.edit_text("Case started.")
    await message.answer(f"🗣 {opening}", reply_markup=_end_case_kb())


@router.message(OsceStates.in_case, _NOT_A_COMMAND)
async def handle_case_turn(message: Message, state: FSMContext):
    data = await state.get_data()
    topic = data.get("topic")
    history = data.get("history") or []
    if not topic:
        await state.clear()
        await message.answer("This case expired. Tap 🩺 OSCE Practice to start a new one.")
        return

    language = subscriptions.get_language(message.from_user.id)
    try:
        reply = await asyncio.wait_for(
            asyncio.to_thread(osce_ai.continue_case, topic, history, message.text, language),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.exception("OSCE case turn failed")
        await message.answer(f"Something went wrong: {e}")
        return

    history = history + [{"role": "user", "content": message.text}, {"role": "assistant", "content": reply}]
    await state.update_data(history=history)
    await message.answer(f"🗣 {reply}", reply_markup=_end_case_kb())


@router.callback_query(F.data == "osce:end", OsceStates.in_case)
async def handle_end_case(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    topic = data.get("topic")
    history = data.get("history") or []
    await state.clear()
    if not topic:
        await callback.message.answer("This case already expired.")
        return

    language = subscriptions.get_language(callback.from_user.id)
    status = await callback.message.answer("Preparing debrief...")
    try:
        debrief = await asyncio.wait_for(
            asyncio.to_thread(osce_ai.debrief_case, topic, history, language), timeout=_GENERATION_TIMEOUT_SECONDS
        )
    except Exception as e:
        logger.exception("OSCE debrief failed")
        await status.edit_text(f"Couldn't generate the debrief: {e}")
        return

    await status.edit_text("Debrief ready.")
    ok = await send_long_text(callback.message.answer, f"🏁 *Case Debrief*\n\n{debrief}")
    if not ok:
        await callback.message.answer("Couldn't send the debrief (Telegram rejected the message).")


def register_osce_handlers(dp) -> None:
    dp.include_router(router)

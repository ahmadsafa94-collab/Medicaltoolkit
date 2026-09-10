"""
Chat flow for the Study Tools 🫀 ECG Interpretation / 🧪 Lab Interpretation
buttons. Handles the "ask permission before asking for input" ordering:
subscriptions.can_use_trial_or_premium() is checked (read-only) BEFORE
prompting for a photo/text, so a user who's already used their one free
trial sees the upgrade prompt immediately instead of uploading a photo
first only to be told no -- the trial itself is only actually consumed
(subscriptions.check_and_consume_trial()) once real input is submitted.
"""

import asyncio
import logging
import os
import uuid

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

import ecg_lab_ai
import subscriptions
from keyboards import lab_input_mode_kb
from paths import user_dir
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="ecg_lab_flow")

_GENERATION_TIMEOUT_SECONDS = 60


class EcgLabStates(StatesGroup):
    awaiting_ecg_image = State()
    awaiting_lab_text = State()
    awaiting_lab_image = State()


def _upgrade_message(feature_label: str) -> str:
    return (
        f"You've already used your one free {feature_label} trial. This is a Premium feature -- "
        "tap ⭐ My Plan to upgrade and keep using it."
    )


@router.callback_query(F.data == "study:ecg")
async def handle_study_ecg(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not subscriptions.can_use_trial_or_premium(callback.from_user.id, "ecg"):
        await callback.message.answer(_upgrade_message("ECG interpretation"))
        return
    await state.set_state(EcgLabStates.awaiting_ecg_image)
    await callback.message.answer(
        "🫀 Send a photo of the ECG (as a photo or an image file). This is for study/pattern-recognition "
        "practice only, not a diagnosis -- please use a de-identified or practice/textbook tracing. /cancel to abort."
    )


@router.callback_query(F.data == "study:lab")
async def handle_study_lab(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not subscriptions.can_use_trial_or_premium(callback.from_user.id, "lab"):
        await callback.message.answer(_upgrade_message("lab interpretation"))
        return
    await callback.message.answer(
        "🧪 How would you like to give me the lab values?", reply_markup=lab_input_mode_kb()
    )


@router.callback_query(F.data == "lab:mode:text")
async def handle_lab_mode_text(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not subscriptions.can_use_trial_or_premium(callback.from_user.id, "lab"):
        await callback.message.answer(_upgrade_message("lab interpretation"))
        return
    await state.set_state(EcgLabStates.awaiting_lab_text)
    await callback.message.answer("Type the lab values, e.g. 'Na 148, K 2.9, Cr 1.8'. /cancel to abort.")


@router.callback_query(F.data == "lab:mode:photo")
async def handle_lab_mode_photo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not subscriptions.can_use_trial_or_premium(callback.from_user.id, "lab"):
        await callback.message.answer(_upgrade_message("lab interpretation"))
        return
    await state.set_state(EcgLabStates.awaiting_lab_image)
    await callback.message.answer(
        "📷 Send a photo of the lab report. Please use de-identified or practice material. /cancel to abort."
    )


@router.message(Command("cancel"), EcgLabStates.awaiting_ecg_image)
@router.message(Command("cancel"), EcgLabStates.awaiting_lab_text)
@router.message(Command("cancel"), EcgLabStates.awaiting_lab_image)
async def handle_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


async def _download_image_bytes(bot, message: Message) -> tuple[bytes, str] | None:
    """Returns (image_bytes, media_type), or None (and answers the user) if the message has no usable image."""
    file_id = None
    media_type = "image/jpeg"

    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "image/jpeg"
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
        media_type = message.document.mime_type

    if not file_id:
        await message.answer("Please send an image (as a photo or an image file).")
        return None

    workdir = user_dir(message.from_user.id)
    tmp_path = os.path.join(workdir, f"_tmp_img_{uuid.uuid4().hex[:8]}")
    try:
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
        with open(tmp_path, "rb") as f:
            return f.read(), media_type
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@router.message(EcgLabStates.awaiting_ecg_image, F.photo | F.document)
async def handle_ecg_image(message: Message, state: FSMContext):
    from bot_instance import bot as tg_bot

    downloaded = await _download_image_bytes(tg_bot, message)
    if downloaded is None:
        return
    image_bytes, media_type = downloaded

    try:
        subscriptions.check_and_consume_trial(message.from_user.id, "ecg")
    except subscriptions.PremiumRequired as e:
        await state.clear()
        await message.answer(str(e))
        return

    await state.clear()
    status = await message.answer("Analyzing the ECG...")
    language = subscriptions.get_language(message.from_user.id)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(ecg_lab_ai.interpret_ecg, image_bytes, media_type, language),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.exception("ECG interpretation failed")
        await status.edit_text(f"Couldn't interpret that image: {e}")
        return

    await status.edit_text("Done.")
    ok = await send_long_text(message.answer, f"🫀 *ECG Interpretation*\n\n{result}")
    if not ok:
        await message.answer("Couldn't send the interpretation (Telegram rejected the message).")


@router.message(EcgLabStates.awaiting_ecg_image)
async def handle_ecg_image_wrong_type(message: Message):
    await message.answer("Please send an image (as a photo or an image file), or /cancel.")


@router.message(EcgLabStates.awaiting_lab_text, F.text & ~F.text.startswith("/"))
async def handle_lab_text(message: Message, state: FSMContext):
    try:
        subscriptions.check_and_consume_trial(message.from_user.id, "lab")
    except subscriptions.PremiumRequired as e:
        await state.clear()
        await message.answer(str(e))
        return

    await state.clear()
    status = await message.answer("Interpreting...")
    language = subscriptions.get_language(message.from_user.id)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(ecg_lab_ai.interpret_lab_text, message.text, language),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.exception("Lab interpretation (text) failed")
        await status.edit_text(f"Couldn't interpret that: {e}")
        return

    await status.edit_text("Done.")
    ok = await send_long_text(message.answer, f"🧪 *Lab Interpretation*\n\n{result}")
    if not ok:
        await message.answer("Couldn't send the interpretation (Telegram rejected the message).")


@router.message(EcgLabStates.awaiting_lab_image, F.photo | F.document)
async def handle_lab_image(message: Message, state: FSMContext):
    from bot_instance import bot as tg_bot

    downloaded = await _download_image_bytes(tg_bot, message)
    if downloaded is None:
        return
    image_bytes, media_type = downloaded

    try:
        subscriptions.check_and_consume_trial(message.from_user.id, "lab")
    except subscriptions.PremiumRequired as e:
        await state.clear()
        await message.answer(str(e))
        return

    await state.clear()
    status = await message.answer("Reading and interpreting...")
    language = subscriptions.get_language(message.from_user.id)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(ecg_lab_ai.interpret_lab_image, image_bytes, media_type, language),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.exception("Lab interpretation (image) failed")
        await status.edit_text(f"Couldn't interpret that image: {e}")
        return

    await status.edit_text("Done.")
    ok = await send_long_text(message.answer, f"🧪 *Lab Interpretation*\n\n{result}")
    if not ok:
        await message.answer("Couldn't send the interpretation (Telegram rejected the message).")


@router.message(EcgLabStates.awaiting_lab_image)
async def handle_lab_image_wrong_type(message: Message):
    await message.answer("Please send an image (as a photo or an image file), or /cancel.")


def register_ecg_lab_handlers(dp) -> None:
    dp.include_router(router)

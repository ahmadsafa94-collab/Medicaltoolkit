"""
Chat flow for Study Tools -> 🩻 Image Quiz (Radiology/Histology). Gated by
the same "quizzes" free-tier quota as every other quiz-generating feature --
one quiz generated from one uploaded image counts as one quiz.
"""

import asyncio
import logging
import os
import uuid

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import image_quiz_ai
import subscriptions
from paths import user_dir
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="radiology_flow")

_GENERATION_TIMEOUT_SECONDS = 60


class ImageQuizStates(StatesGroup):
    awaiting_image = State()


@router.callback_query(F.data == "study:imagequiz")
async def handle_study_image_quiz(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    try:
        subscriptions.check_and_consume(callback.from_user.id, "quizzes")
    except subscriptions.QuotaExceeded as e:
        await callback.message.answer(str(e))
        return

    await state.set_state(ImageQuizStates.awaiting_image)
    await callback.message.answer(
        "🩻 Send a radiology or histology image (photo or image file) and I'll generate a short "
        "self-test quiz about it. /cancel to abort."
    )


@router.message(Command("cancel"), ImageQuizStates.awaiting_image)
async def handle_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.message(ImageQuizStates.awaiting_image, F.photo | F.document)
async def handle_image(message: Message, state: FSMContext):
    from bot_instance import bot as tg_bot

    file_id = None
    media_type = "image/jpeg"
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and (message.document.mime_type or "").startswith("image/"):
        file_id = message.document.file_id
        media_type = message.document.mime_type

    if not file_id:
        await message.answer("Please send an image (as a photo or an image file).")
        return

    await state.clear()
    workdir = user_dir(message.from_user.id)
    tmp_path = os.path.join(workdir, f"_tmp_imgquiz_{uuid.uuid4().hex[:8]}")
    status = await message.answer("Analyzing the image and writing questions...")
    language = subscriptions.get_language(message.from_user.id)
    try:
        file = await tg_bot.get_file(file_id)
        await tg_bot.download_file(file.file_path, destination=tmp_path)
        with open(tmp_path, "rb") as f:
            image_bytes = f.read()

        quiz_text = await asyncio.wait_for(
            asyncio.to_thread(image_quiz_ai.generate_image_quiz, image_bytes, media_type, 3, language),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except Exception as e:
        logger.exception("Image quiz generation failed")
        await status.edit_text(f"Couldn't generate a quiz for that image: {e}")
        return
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    await status.edit_text("Done.")
    ok = await send_long_text(message.answer, f"🩻 *Image Quiz*\n\n{quiz_text}")
    if not ok:
        await message.answer("Couldn't send the quiz (Telegram rejected the message).")


@router.message(ImageQuizStates.awaiting_image)
async def handle_wrong_type(message: Message):
    await message.answer("Please send an image (as a photo or an image file), or /cancel.")


def register_radiology_handlers(dp) -> None:
    dp.include_router(router)

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
import ui_text
import os
import uuid

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery

import ecg_lab_ai
import image_intake
import subscriptions
from keyboards import cancel_kb, lab_input_mode_kb
from paths import user_dir
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="ecg_lab_flow")

# Raised from 60: ecg_lab_ai.py now runs a draft PLUS an independent verify
# pass (two sequential Claude calls, one of them often a vision call) for
# every interpretation, so the old single-call budget is no longer enough.
_GENERATION_TIMEOUT_SECONDS = 100

# The ECG path needs far longer than the others and gets its own budget.
# Each of its two passes now sends five images -- the full sheet plus four
# magnified quadrants, so a bundle branch block is actually resolvable (see
# ecg_tiles) -- against a much longer prompt, and the token ceiling was
# raised because a pass was spending thousands of tokens before writing its
# first visible line. All of that is time, and at 100 seconds the read was
# timing out: the user got "Couldn't interpret that image:" with nothing
# after the colon, because asyncio.TimeoutError stringifies to "".
_ECG_TIMEOUT_SECONDS = 300
_LAB_IMAGE_TIMEOUT_SECONDS = 180

# Shown in rotation while an ECG read is in flight. A read can now run for
# minutes, and a status line that has not changed for that long is
# indistinguishable from a bot that has died.
#
# Declared under ui_strings' TRANSLATABLE_MESSAGES name so they are
# pre-translated with every other fixed string. Without that they would be
# translated live, one call per update, mid-read -- which would make the
# progress display itself a source of the delay it is reporting on. The
# literals live in this list rather than in an alias because ui_strings
# reads the source, not the imported module.
TRANSLATABLE_MESSAGES = [
    "Reading the tracing...",
    "Measuring rate, intervals and axis...",
    "Checking the named-pattern list...",
    "Double-checking the read against the ECG textbooks...",
    "Still working -- nearly there...",
]
_ECG_PROGRESS_STEPS = TRANSLATABLE_MESSAGES
_PROGRESS_INTERVAL_SECONDS = 25


def _describe_error(exc: Exception) -> str:
    """
    A non-empty description of a failure, for a message the user reads.

    Several exceptions worth reporting carry no message at all --
    asyncio.TimeoutError is the one that bit here, and str() on it returns
    the empty string -- so interpolating str(exc) straight into "Couldn't
    interpret that image: {e}" produced a sentence that ended at the colon
    and told the user (and me) nothing.
    """
    text = str(exc).strip()
    if text:
        return text
    return f"the request failed with {type(exc).__name__} and no further detail"


async def _run_with_progress(coro, timeout: float, status_msg, steps: list[str]):
    """
    Await `coro` while cycling `steps` through `status_msg`.

    The ticker is a separate task so a slow read keeps showing signs of
    life, and it is always cancelled before this returns -- including when
    the work raises -- so it can never outlive the request and keep editing
    a message about work that has already finished.
    """

    async def tick():
        try:
            for step in steps:
                await asyncio.sleep(_PROGRESS_INTERVAL_SECONDS)
                try:
                    await status_msg.edit_text(step)
                except Exception:
                    pass  # edit rate-limited, or content unchanged -- cosmetic either way
        except asyncio.CancelledError:
            pass

    ticker = asyncio.create_task(tick())
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    finally:
        ticker.cancel()


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
        # "Send it as a File" is not a nicety: Telegram re-compresses anything
        # sent as a photo down to roughly 1280 px on the long edge, and across
        # a 12-lead sheet that leaves about 6 pixels per small box. Features
        # that are one small box wide -- the terminal R' of a right bundle
        # branch block, a small Q in III -- are destroyed before the bot ever
        # receives them, and no amount of processing on our side brings them
        # back. Sent as a file the image arrives at full resolution.
        "🫀 Send the ECG. For the most accurate read, send it as a FILE rather than as a photo: "
        "Telegram shrinks photos, and fine detail like a bundle-branch pattern or a small Q wave can "
        "be lost in the compression. A photo still works, just less reliably for fine morphology.\n"
        "Any common image format is fine as a file -- JPG, PNG, WebP, JPEG 2000 (.jp2/.jpf), TIFF, "
        "BMP or HEIC.\n\n"
        "This is for study/pattern-recognition practice only, not a diagnosis -- please use a "
        "de-identified or practice/textbook tracing."
    , reply_markup=cancel_kb())


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
    await callback.message.answer("Type the lab values, e.g. 'Na 148, K 2.9, Cr 1.8'.", reply_markup=cancel_kb())


@router.callback_query(F.data == "lab:mode:photo")
async def handle_lab_mode_photo(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    if not subscriptions.can_use_trial_or_premium(callback.from_user.id, "lab"):
        await callback.message.answer(_upgrade_message("lab interpretation"))
        return
    await state.set_state(EcgLabStates.awaiting_lab_image)
    await callback.message.answer(
        "📷 Send a photo of the lab report. Please use de-identified or practice material."
    , reply_markup=cancel_kb())


@router.message(Command("cancel"), EcgLabStates.awaiting_ecg_image)
@router.message(Command("cancel"), EcgLabStates.awaiting_lab_text)
@router.message(Command("cancel"), EcgLabStates.awaiting_lab_image)
async def handle_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


async def _download_image_bytes(bot, message: Message) -> tuple[bytes, str] | None:
    """
    Returns (image_bytes, media_type), or None (and answers the user) if the
    message has no usable image.

    A document is accepted on the strength of whether it DECODES, not on the
    MIME type attached to it: Telegram labels anything it does not recognise
    application/octet-stream, so the previous mime_type.startswith("image/")
    test rejected perfectly good uploads unread -- JPEG 2000 (.jpf/.jp2) off
    a scanner, HEIC off an iPhone -- and did so precisely because the ECG
    prompt now asks for a file rather than a compressed photo. image_intake
    does the decoding and converts anything the vision API will not take.
    """
    file_id = None
    media_type = "image/jpeg"
    file_name = None

    if message.photo:
        file_id = message.photo[-1].file_id
        media_type = "image/jpeg"
    elif message.document:
        file_id = message.document.file_id
        media_type = message.document.mime_type or ""
        file_name = message.document.file_name

    if not file_id:
        await message.answer("Please send an image (as a photo or an image file).")
        return None

    workdir = user_dir(message.from_user.id)
    tmp_path = os.path.join(workdir, f"_tmp_img_{uuid.uuid4().hex[:8]}")
    try:
        file = await bot.get_file(file_id)
        await bot.download_file(file.file_path, destination=tmp_path)
        with open(tmp_path, "rb") as f:
            raw = f.read()
        try:
            return image_intake.normalize(raw, media_type, file_name)
        except image_intake.UnsupportedImage as e:
            await message.answer(str(e))
            return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# The F.document half of this filter is unreachable in practice: bot.py
# registers handle_ecg_document/handle_lab_document on `dp` itself, which
# aiogram tries before descending into this router (that is the whole
# reason they exist there -- see their docstrings). Kept anyway so these
# handlers stay correct on their own terms if that registration ever moves,
# and both paths run the same code either way.
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
    status = await message.answer("Analyzing the ECG, then double-checking the read...")
    language = subscriptions.get_language(message.from_user.id)
    try:
        result = await _run_with_progress(
            asyncio.to_thread(ecg_lab_ai.interpret_ecg, image_bytes, media_type, language),
            _ECG_TIMEOUT_SECONDS,
            status,
            _ECG_PROGRESS_STEPS,
        )
    except asyncio.TimeoutError:
        # Its own branch because it is not an interpretation failure and the
        # useful thing to say is entirely different: nothing was wrong with
        # the tracing, the read simply ran out of time.
        logger.error("ECG interpretation timed out after %ss", _ECG_TIMEOUT_SECONDS)
        await status.edit_text(
            "That ECG took too long to read and timed out. Please try again -- and if it keeps "
            "happening, a tighter crop of just the tracing reads faster."
        )
        return
    except Exception as e:
        logger.exception("ECG interpretation failed")
        await status.edit_text(f"Couldn't interpret that image: {_describe_error(e)}")
        return

    await status.edit_text("Done.")
    with ui_text.untranslated():  # already written in the user's language by interpret_ecg
        ok = await send_long_text(message.answer, f"🫀 *ECG Interpretation*\n\n{result}")
    if not ok:
        await message.answer("Couldn't send the interpretation (Telegram rejected the message).")


@router.message(EcgLabStates.awaiting_ecg_image)
async def handle_ecg_image_wrong_type(message: Message):
    await message.answer("Please send an image (as a photo or an image file).", reply_markup=cancel_kb())


@router.message(EcgLabStates.awaiting_lab_text, F.text & ~F.text.startswith("/"))
async def handle_lab_text(message: Message, state: FSMContext):
    try:
        subscriptions.check_and_consume_trial(message.from_user.id, "lab")
    except subscriptions.PremiumRequired as e:
        await state.clear()
        await message.answer(str(e))
        return

    await state.clear()
    status = await message.answer("Interpreting, then double-checking...")
    language = subscriptions.get_language(message.from_user.id)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(ecg_lab_ai.interpret_lab_text, message.text, language),
            timeout=_GENERATION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("Lab text interpretation timed out after %ss", _GENERATION_TIMEOUT_SECONDS)
        await status.edit_text("That took too long to interpret and timed out. Please try again.")
        return
    except Exception as e:
        logger.exception("Lab interpretation (text) failed")
        await status.edit_text(f"Couldn't interpret that: {_describe_error(e)}")
        return

    await status.edit_text("Done.")
    with ui_text.untranslated():  # already written in the user's language by interpret_lab_*
        ok = await send_long_text(message.answer, f"🧪 *Lab Interpretation*\n\n{result}")
    if not ok:
        await message.answer("Couldn't send the interpretation (Telegram rejected the message).")


# The F.document half of this filter is unreachable in practice: bot.py
# registers handle_ecg_document/handle_lab_document on `dp` itself, which
# aiogram tries before descending into this router (that is the whole
# reason they exist there -- see their docstrings). Kept anyway so these
# handlers stay correct on their own terms if that registration ever moves,
# and both paths run the same code either way.
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
    status = await message.answer("Reading and interpreting, then double-checking...")
    language = subscriptions.get_language(message.from_user.id)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(ecg_lab_ai.interpret_lab_image, image_bytes, media_type, language),
            timeout=_LAB_IMAGE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("Lab image interpretation timed out after %ss", _LAB_IMAGE_TIMEOUT_SECONDS)
        await status.edit_text("That report took too long to read and timed out. Please try again.")
        return
    except Exception as e:
        logger.exception("Lab interpretation (image) failed")
        await status.edit_text(f"Couldn't interpret that image: {_describe_error(e)}")
        return

    await status.edit_text("Done.")
    with ui_text.untranslated():  # already written in the user's language by interpret_lab_*
        ok = await send_long_text(message.answer, f"🧪 *Lab Interpretation*\n\n{result}")
    if not ok:
        await message.answer("Couldn't send the interpretation (Telegram rejected the message).")


@router.message(EcgLabStates.awaiting_lab_image)
async def handle_lab_image_wrong_type(message: Message):
    await message.answer("Please send an image (as a photo or an image file).", reply_markup=cancel_kb())


def register_ecg_lab_handlers(dp) -> None:
    dp.include_router(router)

"""
Medical Student Toolkit Bot -- Step 1: AI-powered PDF chapter splitting.

Run with:
    python bot.py

Requires a .env file (see .env.example) with:
    TELEGRAM_BOT_TOKEN=...
    ANTHROPIC_API_KEY=...
"""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, FSInputFile, BufferedInputFile

from config import TELEGRAM_BOT_TOKEN, STORAGE_DIR
from pdf_processor import process_pdf, ChapterDetectionError
from drug_lookup import lookup_drug, format_drug_info, DrugNotFoundError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()


def user_dir(user_id: int) -> str:
    path = os.path.join(STORAGE_DIR, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path


@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "Welcome to the Medical Student Toolkit bot.\n\n"
        "Send me a textbook PDF and I'll split it into chapters using AI.\n\n"
        "Commands:\n"
        "/start - this message\n"
        "/help - how to use the bot\n"
        "/dose <drug> - FDA label dosing & reference info"
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Just send a .pdf file as a document (not a photo) and I'll:\n"
        "1. Read it\n"
        "2. Ask Claude to find the chapter boundaries\n"
        "3. Send you back one PDF per chapter\n\n"
        "Note: very large PDFs (400+ pages) aren't supported in this first "
        "version.\n\n"
        "/dose <drug name> - pulls dosing, contraindications, interactions, "
        "etc. straight from the FDA label database. Reference only, not a "
        "substitute for a current formulary."
    )


@dp.message(Command("dose"))
async def cmd_dose(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(
            "Usage: /dose <drug name>\nExample: /dose sertraline"
        )
        return

    drug_name = args[1].strip()
    status_msg = await message.answer(f"Looking up {drug_name}...")

    try:
        sections = await lookup_drug(drug_name)
    except DrugNotFoundError as e:
        await status_msg.edit_text(str(e))
        return
    except Exception as e:
        logger.exception("Drug lookup failed")
        await status_msg.edit_text(f"Lookup failed: {e}")
        return

    reply = format_drug_info(sections)
    # Telegram caps messages at 4096 chars; split if needed
    for i in range(0, len(reply), 4000):
        await message.answer(reply[i:i + 4000], parse_mode="Markdown")

    await status_msg.delete()


@dp.message(F.document)
async def handle_pdf_upload(message: Message):
    doc = message.document

    if doc.mime_type != "application/pdf" and not doc.file_name.lower().endswith(".pdf"):
        await message.answer("That doesn't look like a PDF. Please send a .pdf file.")
        return

    status_msg = await message.answer("Got it. Downloading...")

    workdir = user_dir(message.from_user.id)
    local_pdf_path = os.path.join(workdir, doc.file_name)

    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, destination=local_pdf_path)

    await status_msg.edit_text("Downloaded. Reading pages and detecting chapters with AI...")

    output_dir = os.path.join(workdir, "chapters")

    try:
        chapters, output_paths = await asyncio.to_thread(
            process_pdf, local_pdf_path, output_dir
        )
    except ChapterDetectionError as e:
        await status_msg.edit_text(f"Couldn't split this PDF: {e}")
        return
    except Exception as e:
        logger.exception("Unexpected error processing PDF")
        await status_msg.edit_text(f"Something went wrong: {e}")
        return

    await status_msg.edit_text(
        f"Found {len(chapters)} chapter(s). Sending them now..."
    )

    for chapter, path in zip(chapters, output_paths):
        caption = f"{chapter['title']} (from page {chapter['start_page']})"
        await message.answer_document(FSInputFile(path), caption=caption[:1024])

    await message.answer("Done. Send another PDF anytime.")


async def main():
    os.makedirs(STORAGE_DIR, exist_ok=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

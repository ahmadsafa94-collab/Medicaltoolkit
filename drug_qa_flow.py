"""
Chat flow for "Ask AI about a drug" -- free-form Q&A grounded in a single
drug's real FDA label (see drug_qa.py for the grounding/answer logic).

Two entry points feed the same awaiting_question loop:
  1. 🧠 Study Tools -> 💊 Ask About Drugs -- asks for a drug name first, a
     fresh lookup via the same lookup_drug() call /dose itself uses.
  2. The "🤖 Ask AI about this drug" button attached to a /dose lookup's
     section menu (see keyboards.py's drug_sections_kb) -- reuses that
     lookup's already-fetched sections straight from session_cache, no
     second openFDA round-trip needed.

Quota-gated on the same "questions" bucket as "Ask My Books" (pdf_qa.py) --
this is the same shape of feature (free-form Q&A grounded in a document
Claude is hand a context, not a fixed-format single call like a chapter
summary), so it shares that monthly budget rather than inventing a new one.
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

import drug_qa
import session_cache
import subscriptions
from drug_lookup import lookup_drug, DrugNotFoundError, DrugLookupRateLimitedError
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="drug_qa_flow")

_NOT_A_COMMAND = F.text & ~F.text.startswith("/")
_LOOKUP_TIMEOUT_SECONDS = 25
_ANSWER_TIMEOUT_SECONDS = 30


class DrugQAStates(StatesGroup):
    awaiting_drug_name = State()
    awaiting_question = State()


@router.callback_query(F.data == "study:drugqa")
async def handle_study_drugqa(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(DrugQAStates.awaiting_drug_name)
    await callback.message.answer(
        "💊 Which drug would you like to ask about? Type the generic name (e.g. 'metformin'). /cancel to abort."
    )


@router.message(Command("cancel"), DrugQAStates.awaiting_drug_name)
@router.message(Command("cancel"), DrugQAStates.awaiting_question)
async def handle_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.message(DrugQAStates.awaiting_drug_name, _NOT_A_COMMAND)
async def handle_drug_name(message: Message, state: FSMContext):
    drug_name = message.text.strip()
    status = await message.answer(f"Looking up {drug_name}...")

    try:
        sections = await asyncio.wait_for(lookup_drug(drug_name), timeout=_LOOKUP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await status.edit_text("The FDA database took too long to respond. Please try again in a moment.")
        return
    except DrugLookupRateLimitedError as e:
        await status.edit_text(str(e))
        return
    except DrugNotFoundError as e:
        await status.edit_text(str(e) + "\n\nTry another name, or /cancel to stop.")
        return
    except Exception as e:
        logger.exception("Drug lookup failed (Ask AI about drugs flow)")
        await status.edit_text(f"Lookup failed: {e}\n\nTry another name, or /cancel to stop.")
        return

    name = sections.get("_name", drug_name)
    await state.set_state(DrugQAStates.awaiting_question)
    await state.update_data(drug_name=name, sections=sections, history=[])
    await status.edit_text(f"Found {name}. Ask your question -- dose, side effects, interactions, anything the label covers (or /cancel to stop):")


@router.callback_query(F.data.startswith("dqa:ask:"))
async def handle_ask_from_dose_lookup(callback: CallbackQuery, state: FSMContext):
    cache_id = callback.data.split(":", 2)[2]
    sections = session_cache.get(cache_id)
    if sections is None:
        await callback.answer("This lookup expired. Please run /dose again.", show_alert=True)
        return

    name = sections.get("_name", "this drug")
    await callback.answer()
    await state.set_state(DrugQAStates.awaiting_question)
    await state.update_data(drug_name=name, sections=sections, history=[])
    await callback.message.answer(f"💊 Ask a question about *{name}* (or /cancel to stop):", parse_mode="Markdown")


@router.message(DrugQAStates.awaiting_question, _NOT_A_COMMAND)
async def handle_question(message: Message, state: FSMContext):
    data = await state.get_data()
    drug_name = data.get("drug_name")
    sections = data.get("sections")
    history = data.get("history") or []
    if not drug_name or not sections:
        await state.clear()
        await message.answer("This session expired. Start again from 🧠 Study Tools -> 💊 Ask About Drugs.")
        return

    question = message.text.strip()
    if not question:
        await message.answer("Please send your question as text.")
        return

    try:
        subscriptions.check_and_consume(message.from_user.id, "questions")
    except subscriptions.QuotaExceeded as e:
        await message.answer(str(e))
        return

    status = await message.answer("Searching the label...")
    language = subscriptions.get_language(message.from_user.id)
    try:
        answer = await asyncio.wait_for(
            asyncio.to_thread(drug_qa.answer_question, drug_name, sections, question, history, language),
            timeout=_ANSWER_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await status.edit_text("That took too long. Please try again.")
        return
    except drug_qa.DrugQAError as e:
        await status.edit_text(str(e))
        return
    except Exception:
        logger.exception("Drug Q&A failed for %s", drug_name)
        await status.edit_text("Something went wrong answering that. Please try again.")
        return

    try:
        await status.delete()
    except Exception:
        pass  # not critical if the "Searching..." message can't be deleted (e.g. already gone)

    ok = await send_long_text(message.answer, answer)
    if not ok:
        await message.answer("Couldn't send that answer (Telegram rejected the message).")

    # Stay in the same state so follow-up questions about the same drug
    # don't require re-picking/re-fetching it every time.
    history = history + [{"question": question, "answer": answer}]
    await state.update_data(drug_name=drug_name, sections=sections, history=history)


def register_drug_qa_handlers(dp) -> None:
    dp.include_router(router)

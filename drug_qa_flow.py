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
import name_resolver
import session_cache
import subscriptions
from drug_lookup import lookup_drug, DrugNotFoundError, DrugLookupRateLimitedError
from keyboards import drug_suggestion_kb
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="drug_qa_flow")

_NOT_A_COMMAND = F.text & ~F.text.startswith("/")
_LOOKUP_TIMEOUT_SECONDS = 25
_ANSWER_TIMEOUT_SECONDS = 30
_RESOLVE_TIMEOUT_SECONDS = 20  # the "what did they mean?" Claude call, on a lookup miss


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
    except DrugNotFoundError:
        # A direct miss is usually a typo or a brand name the FDA record
        # doesn't carry -- ask Claude what was meant and offer it back,
        # rather than telling the user their drug doesn't exist.
        await _offer_name_suggestions(status, state, drug_name)
        return
    except Exception as e:
        logger.exception("Drug lookup failed (Ask AI about drugs flow)")
        await status.edit_text(f"Lookup failed: {e}\n\nTry another name, or /cancel to stop.")
        return

    await _begin_questions(status.edit_text, state, sections, drug_name)


async def _begin_questions(answer_fn, state: FSMContext, sections: dict, fallback_name: str):
    """Shared by a direct hit and a confirmed suggestion -- both land in the same question loop."""
    name = sections.get("_name", fallback_name)
    await state.set_state(DrugQAStates.awaiting_question)
    await state.update_data(drug_name=name, sections=sections, history=[])
    await answer_fn(
        f"Found {name}. Ask your question -- dose, side effects, interactions, anything the label "
        "covers (or /cancel to stop):"
    )


async def _resolve_candidate(name: str) -> tuple[str, dict] | None:
    """Look a suggested name up; None if it doesn't resolve after all."""
    try:
        sections = await asyncio.wait_for(lookup_drug(name), timeout=_LOOKUP_TIMEOUT_SECONDS)
    except Exception:
        return None
    return sections.get("_name", name), sections


async def _offer_name_suggestions(status: Message, state: FSMContext, raw_name: str):
    await status.edit_text(f"No FDA label found for '{raw_name}'. Checking what you might have meant...")

    try:
        candidates = await asyncio.wait_for(
            asyncio.to_thread(name_resolver.resolve_drug_names, raw_name), timeout=_RESOLVE_TIMEOUT_SECONDS
        )
    except Exception:
        logger.exception("name_resolver.resolve_drug_names failed for '%s'", raw_name)
        candidates = []

    # Every candidate is verified against openFDA before being offered:
    # Claude naming a real drug is no guarantee the database carries a label
    # under that name, and a button that fails when tapped is worse than not
    # offering it. Concurrently, so N candidates cost one round-trip of
    # latency rather than N.
    resolved = await asyncio.gather(*(_resolve_candidate(c) for c in candidates))
    options = []
    seen = set()
    for item in resolved:
        if item is None:
            continue
        display_name, sections = item
        if display_name.lower() in seen:
            continue  # two spellings that resolve to the same label
        seen.add(display_name.lower())
        options.append((session_cache.put(sections), display_name))

    if not options:
        await status.edit_text(
            f"No FDA label found for '{raw_name}', and I couldn't work out what you meant.\n\n"
            "Try the plain generic name (e.g. 'amoxicillin' rather than 'Amoxil 500mg'), or /cancel to stop."
        )
        return

    # Plain text, no parse_mode: drug names come from openFDA and can carry
    # characters Telegram's legacy Markdown treats as formatting, which
    # would make it reject the edit outright and leave the user staring at
    # the "Checking..." message with no buttons.
    if len(options) == 1:
        prompt = f"Did you mean {options[0][1]}?"
    else:
        prompt = f"I couldn't find '{raw_name}'. Did you mean one of these?"
    await status.edit_text(prompt, reply_markup=drug_suggestion_kb(options, "dqa:pick"))


@router.callback_query(F.data == "dqa:pick:none", DrugQAStates.awaiting_drug_name)
async def handle_suggestion_rejected(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(
        "Okay -- type the name again, spelled differently, or try the generic name. /cancel to stop."
    )


@router.callback_query(F.data.startswith("dqa:pick:"), DrugQAStates.awaiting_drug_name)
async def handle_suggestion_picked(callback: CallbackQuery, state: FSMContext):
    cache_id = callback.data.split(":", 2)[2]
    sections = session_cache.get(cache_id)
    if sections is None:
        await callback.answer("That suggestion expired. Type the drug name again.", show_alert=True)
        return
    await callback.answer()
    await _begin_questions(callback.message.answer, state, sections, "this drug")


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

"""
Multi-drug interaction checker.

Lets the user add as many drugs as they want -- via lookup_drug, the same
openFDA-backed lookup /dose uses, with an AI fallback (interaction_ai.py's
resolve_drug_name) when a typed name doesn't match directly, so a typo or an
unusual brand name still resolves (with a "Did you mean X?" confirmation
rather than silently guessing) -- then asks Claude to read each drug's own
"Drug Interactions" and "Contraindications" label sections and describe what
they say (or don't say) about the others.

This is grounded ONLY in each drug's own real FDA label text (openFDA, the
same source /dose shows verbatim) -- Claude is never asked to recall an
interaction from its own training, only to read and summarize the fetched
excerpts (see interaction_ai.py's system prompt). It is deliberately NOT a
curated drug-interaction database (like Lexicomp/Micromedex/an
interaction-checker API), and the bot says so up front and again in every
result: a label not mentioning another drug does not mean there's no
interaction (many are described by drug CLASS -- "other CNS depressants",
"strong CYP3A4 inhibitors" -- rather than by naming every specific drug).

Same FSM-in-its-own-router pattern as renal_flow.py / calc_flow.py.
"""

import asyncio
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import interaction_ai
from drug_lookup import lookup_drug, DrugNotFoundError, DrugLookupRateLimitedError
from keyboards import interaction_menu_kb, interaction_confirm_kb
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="interaction_flow")


class InteractionStates(StatesGroup):
    collecting = State()


_NOT_A_COMMAND = F.text & ~F.text.startswith("/")

# Not a hard technical limit -- just a point past which the interaction
# analysis prompt gets long enough that quality/latency suffer. Explained to
# the user rather than silently enforced.
MAX_DRUGS = 15

_LOOKUP_TIMEOUT_SECONDS = 25
_RESOLVE_TIMEOUT_SECONDS = 15
_ANALYZE_TIMEOUT_SECONDS = 45


@router.message(Command("interactions"))
async def cmd_interactions(message: Message, state: FSMContext):
    await state.set_state(InteractionStates.collecting)
    await state.update_data(drugs=[], pending=None)
    await message.answer(
        "🔀 *Drug Interaction Checker*\n\n"
        "Type a drug name to add it to the list -- generic or brand name, and it's fine if the "
        "spelling isn't perfect. When you have at least 2, tap *Check Interactions* and Claude will "
        "read each drug's own FDA label (Drug Interactions + Contraindications sections) and describe "
        "what each one says about the others.\n\n"
        "⚠️ This is grounded in each drug's own label text, not a curated interaction database. A drug "
        "NOT being mentioned does *not* mean there's no interaction (many are described by drug class, "
        "not by name), and a mention doesn't automatically mean the interaction is clinically "
        "significant. Always confirm with a dedicated interaction checker or a pharmacist before "
        "acting on this.",
        parse_mode="Markdown",
        reply_markup=interaction_menu_kb(0),
    )


@router.message(Command("cancel"), InteractionStates.collecting)
async def cmd_cancel_interactions(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.callback_query(F.data == "ix:cancel")
async def handle_ix_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer("Cancelled.")


async def _require_flow_data(callback_or_message, state: FSMContext) -> dict | None:
    data = await state.get_data()
    if data.get("drugs") is None:
        await state.clear()
        text = "This session expired. Run /interactions again."
        if isinstance(callback_or_message, CallbackQuery):
            await callback_or_message.message.answer(text)
        else:
            await callback_or_message.answer(text)
        return None
    return data


@router.callback_query(F.data == "ix:remove_last")
async def handle_ix_remove_last(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await _require_flow_data(callback, state)
    if data is None:
        return

    drugs = data["drugs"]
    if not drugs:
        await callback.message.answer("No drugs to remove.")
        return

    removed = drugs.pop()
    await state.update_data(drugs=drugs)
    await callback.message.answer(
        f"Removed {removed['name']}. {_list_line(drugs)}",
        reply_markup=interaction_menu_kb(len(drugs)),
    )


@router.callback_query(F.data == "ix:check")
async def handle_ix_check(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await _require_flow_data(callback, state)
    if data is None:
        return

    drugs = data["drugs"]
    if len(drugs) < 2:
        await callback.message.answer("Add at least 2 drugs before checking interactions.")
        return

    status = await callback.message.answer("Asking Claude to read each label...")
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(interaction_ai.analyze_interactions, drugs),
            timeout=_ANALYZE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await status.edit_text("That took too long. Please try again.")
        return
    except interaction_ai.InteractionAIError as e:
        logger.warning("Interaction analysis failed: %s", e)
        await status.edit_text(f"Couldn't analyze interactions right now: {e}")
        return
    except Exception:
        logger.exception("Unexpected error analyzing interactions for %s", [d["name"] for d in drugs])
        await status.edit_text("Something went wrong analyzing interactions. Please try again.")
        return

    try:
        await status.delete()
    except Exception:
        pass  # not critical if the "Asking Claude..." message can't be deleted (e.g. already gone)

    ok = await send_long_text(callback.message.answer, text)
    if not ok:
        await callback.message.answer("Couldn't send the results (Telegram rejected the message).")


async def _lookup_and_add(answer_fn, edit_fn, state: FSMContext, drugs: list[dict], name: str, generic: str | None, sections: dict) -> None:
    """Shared "actually add this confirmed drug" step, used by both the direct-match and the AI-confirmed paths."""
    if any(d["name"].lower() == name.lower() for d in drugs):
        await edit_fn(f"{name} is already in the list.")
        return

    drugs.append({"name": name, "generic": generic, "sections": sections})
    await state.update_data(drugs=drugs, pending=None)

    await edit_fn(f"✅ Added {name}. {_list_line(drugs)}")
    prompt = "Add another, or tap an option below:"
    if len(drugs) >= 2:
        prompt = "Add another, or tap 'Check Interactions' when ready:"
    await answer_fn(prompt, reply_markup=interaction_menu_kb(len(drugs)))


@router.message(InteractionStates.collecting, _NOT_A_COMMAND)
async def handle_ix_add_drug(message: Message, state: FSMContext):
    data = await _require_flow_data(message, state)
    if data is None:
        return

    drugs = data["drugs"]
    raw_name = message.text.strip()
    if not raw_name:
        return

    if len(drugs) >= MAX_DRUGS:
        await message.answer(
            f"That's {MAX_DRUGS} drugs already -- checking every one beyond that produces a wall "
            "of text nobody will read. Remove one first if you want to add another, or check interactions now.",
            reply_markup=interaction_menu_kb(len(drugs)),
        )
        return

    status_msg = await message.answer(f"Looking up {raw_name}...")

    try:
        sections = await asyncio.wait_for(lookup_drug(raw_name), timeout=_LOOKUP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        await status_msg.edit_text("The FDA database took too long to respond. Please try again.")
        return
    except DrugLookupRateLimitedError as e:
        await status_msg.edit_text(str(e))
        return
    except DrugNotFoundError:
        # Direct match failed -- fall back to asking Claude what real drug this
        # was likely meant to be (typo fix, or an uncommon brand name) before
        # giving up. Never auto-added: the user confirms the AI's guess first.
        await status_msg.edit_text(f"Couldn't match '{raw_name}' directly. Checking for a likely match...")
        try:
            candidate = await asyncio.wait_for(
                asyncio.to_thread(interaction_ai.resolve_drug_name, raw_name),
                timeout=_RESOLVE_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.exception("interaction_ai.resolve_drug_name failed for '%s'", raw_name)
            candidate = None

        if candidate is None:
            await status_msg.edit_text(
                f"No FDA label found for '{raw_name}', and I couldn't confidently guess what you meant. "
                "Try the plain generic name (e.g. 'amoxicillin' rather than 'Amoxil 500mg')."
            )
            return

        try:
            candidate_sections = await asyncio.wait_for(lookup_drug(candidate), timeout=_LOOKUP_TIMEOUT_SECONDS)
        except Exception:
            await status_msg.edit_text(
                f"No FDA label found for '{raw_name}'. Try the plain generic name "
                "(e.g. 'amoxicillin' rather than 'Amoxil 500mg')."
            )
            return

        candidate_name = candidate_sections.get("_name", candidate)
        await state.update_data(
            pending={
                "name": candidate_name,
                "generic": candidate_sections.get("_generic"),
                "sections": candidate_sections,
            }
        )
        await status_msg.edit_text(
            f"Did you mean *{candidate_name}*?", parse_mode="Markdown", reply_markup=interaction_confirm_kb()
        )
        return
    except Exception:
        logger.exception("Interaction-checker lookup failed for '%s'", raw_name)
        await status_msg.edit_text(f"Lookup failed for {raw_name}. Please try again.")
        return

    name = sections.get("_name", raw_name)
    generic = sections.get("_generic")

    async def _edit(text: str, **kwargs):
        await status_msg.edit_text(text, **kwargs)

    await _lookup_and_add(message.answer, _edit, state, drugs, name, generic, sections)


@router.callback_query(F.data == "ix:confirm:yes", InteractionStates.collecting)
async def handle_ix_confirm_yes(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await _require_flow_data(callback, state)
    if data is None:
        return

    pending = data.get("pending")
    if not pending:
        await callback.message.answer("Nothing pending to confirm -- type a drug name to add it.")
        return

    drugs = data["drugs"]

    async def _edit(text: str, **kwargs):
        await callback.message.answer(text, **kwargs)

    await _lookup_and_add(
        callback.message.answer, _edit, state, drugs, pending["name"], pending.get("generic"), pending["sections"]
    )


@router.callback_query(F.data == "ix:confirm:no", InteractionStates.collecting)
async def handle_ix_confirm_no(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(pending=None)
    await callback.message.answer("Okay -- type the drug name again, spelled differently, or try a generic name.")


def _list_line(drugs: list[dict]) -> str:
    if not drugs:
        return "List is now empty."
    return "Current list: " + ", ".join(d["name"] for d in drugs)


def register_interaction_handlers(dp) -> None:
    dp.include_router(router)

"""
Multi-drug interaction checker.

Lets the user add as many drugs as they want (via lookup_drug, the same
openFDA-backed lookup /dose uses), then cross-checks each drug's own
"Drug Interactions" and "Contraindications" label sections for mentions of
every OTHER drug in the list, by name.

This is deliberately just a text search over each drug's own FDA label --
NOT a curated drug-interaction database (like Lexicomp/Micromedex/an
interaction-checker API), and the bot says so up front and again in every
result. Two important asymmetries this implies:

  1. A hit means drug A's label happens to mention drug B by name. It does
     NOT mean the interaction is clinically significant, nor does the
     absence of a hit mean there is no interaction -- many real
     interactions are described by drug CLASS ("other CNS depressants",
     "strong CYP3A4 inhibitors") rather than by naming every specific drug,
     and a name search can't catch those.
  2. Interactions are usually mentioned from only one side's label (e.g.
     warfarin's label is far more likely to name a specific NSAID than that
     NSAID's label is to name warfarin back) -- so results are intentionally
     checked in both directions per pair, and the direction of each hit is
     shown rather than collapsed.

Same FSM-in-its-own-router pattern as renal_flow.py / calc_flow.py.
"""

import asyncio
import logging
import re

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from drug_lookup import lookup_drug, DrugNotFoundError, DrugLookupRateLimitedError
from keyboards import interaction_menu_kb
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="interaction_flow")


class InteractionStates(StatesGroup):
    collecting = State()


_NOT_A_COMMAND = F.text & ~F.text.startswith("/")

# Not a hard technical limit -- just a point past which an N^2 cross-check
# produces a wall of text nobody will actually read. Explained to the user
# rather than silently enforced.
MAX_DRUGS = 15

_RELEVANT_FIELD_LABELS = ["Drug Interactions", "Contraindications"]
_MAX_EXCERPTS_PER_PAIR = 4
_MIN_NAME_LEN_TO_MATCH = 3  # avoid matching on stray short tokens


@router.message(Command("interactions"))
async def cmd_interactions(message: Message, state: FSMContext):
    await state.set_state(InteractionStates.collecting)
    await state.update_data(drugs=[])
    await message.answer(
        "🔀 *Drug Interaction Checker*\n\n"
        "Type a drug name to add it to the list -- add as many as you want. "
        "When you have at least 2, tap *Check Interactions* and I'll search each drug's own FDA "
        "label (Drug Interactions + Contraindications sections) for mentions of the others, in both directions.\n\n"
        "⚠️ This is a *text search*, not a curated interaction database. A drug NOT being mentioned "
        "does *not* mean there's no interaction (many are described by drug class, not by name), "
        "and a mention doesn't automatically mean the interaction is clinically significant. "
        "Always confirm with a dedicated interaction checker or a pharmacist before acting on this.",
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

    pair_findings = _cross_check(drugs)
    text = _format_findings(drugs, pair_findings)
    ok = await send_long_text(callback.message.answer, text)
    if not ok:
        await callback.message.answer("Couldn't send the results (Telegram rejected the message).")


@router.message(InteractionStates.collecting, _NOT_A_COMMAND)
async def handle_ix_add_drug(message: Message, state: FSMContext):
    data = await _require_flow_data(message, state)
    if data is None:
        return

    drugs = data["drugs"]
    drug_name = message.text.strip()
    if not drug_name:
        return

    if len(drugs) >= MAX_DRUGS:
        await message.answer(
            f"That's {MAX_DRUGS} drugs already -- cross-checking every pair beyond that produces a wall "
            "of text nobody will read. Remove one first if you want to add another, or check interactions now.",
            reply_markup=interaction_menu_kb(len(drugs)),
        )
        return

    status_msg = await message.answer(f"Looking up {drug_name}...")

    try:
        sections = await asyncio.wait_for(lookup_drug(drug_name), timeout=25)
    except asyncio.TimeoutError:
        await status_msg.edit_text("The FDA database took too long to respond. Please try again.")
        return
    except DrugLookupRateLimitedError as e:
        await status_msg.edit_text(str(e))
        return
    except DrugNotFoundError as e:
        await status_msg.edit_text(str(e))
        return
    except Exception:
        logger.exception("Interaction-checker lookup failed for '%s'", drug_name)
        await status_msg.edit_text(f"Lookup failed for {drug_name}. Please try again.")
        return

    name = sections.get("_name", drug_name)
    generic = sections.get("_generic")

    if any(d["name"].lower() == name.lower() for d in drugs):
        await status_msg.edit_text(f"{name} is already in the list.")
        return

    relevant_chunks = _extract_relevant_chunks(sections)
    drugs.append({"name": name, "generic": generic, "chunks": relevant_chunks})
    await state.update_data(drugs=drugs)

    await status_msg.edit_text(f"✅ Added {name}. {_list_line(drugs)}")
    prompt = "Add another, or tap an option below:"
    if len(drugs) >= 2:
        prompt = "Add another, or tap 'Check Interactions' when ready:"
    await message.answer(prompt, reply_markup=interaction_menu_kb(len(drugs)))


def _list_line(drugs: list[dict]) -> str:
    if not drugs:
        return "List is now empty."
    return "Current list: " + ", ".join(d["name"] for d in drugs)


def _extract_relevant_chunks(sections: dict) -> list[tuple[str, str]]:
    """Returns [(field_label, text_chunk), ...] for the interaction-relevant sections of a lookup_drug() result."""
    chunks = []
    for label in _RELEVANT_FIELD_LABELS:
        data = sections.get(label)
        if not data:
            continue
        for bullet in data.get("bullets", []):
            chunks.append((label, bullet))
        for table in data.get("tables", []):
            chunks.append((label, table))
    return chunks


def _names_to_match(drug: dict) -> set:
    names = {drug["name"]}
    if drug.get("generic"):
        names.add(drug["generic"])
    return {n.strip() for n in names if n and len(n.strip()) >= _MIN_NAME_LEN_TO_MATCH}


def _cross_check(drugs: list[dict]) -> dict:
    """
    For every ordered pair (source, other), search source's interaction-relevant
    chunks for a mention of other's name/generic. Returns
    {(source_name, other_name): [(field_label, excerpt), ...]} for pairs with
    at least one hit -- both directions of a pair are checked and reported
    separately, since labels commonly mention an interacting drug from only
    one side.
    """
    results = {}
    for source in drugs:
        for other in drugs:
            if source is other:
                continue
            other_names = _names_to_match(other)
            if not other_names:
                continue
            pattern = re.compile("|".join(re.escape(n) for n in other_names), re.IGNORECASE)

            hits = []
            for field_label, chunk in source["chunks"]:
                if pattern.search(chunk):
                    excerpt = chunk if len(chunk) <= 300 else chunk[:300].rsplit(" ", 1)[0] + " [...]"
                    hits.append((field_label, excerpt))
                    if len(hits) >= _MAX_EXCERPTS_PER_PAIR:
                        break
            if hits:
                results[(source["name"], other["name"])] = hits
    return results


def _format_findings(drugs: list[dict], pair_findings: dict) -> str:
    if not pair_findings:
        return (
            "No direct textual mentions found between any of these drugs' own Drug Interactions / "
            "Contraindications sections: " + ", ".join(d["name"] for d in drugs) + ".\n\n"
            "⚠️ This does *not* mean there's no interaction -- it only means none of these labels "
            "happened to name another one of these drugs by text. Interactions described generically "
            "(by drug class, e.g. 'other CNS depressants' or 'strong CYP3A4 inhibitors') won't be caught "
            "by a name-matching search like this. Use a dedicated interaction checker or pharmacist "
            "consult before acting on this."
        )

    lines = [f"🔀 *Possible interactions among: {', '.join(d['name'] for d in drugs)}*", ""]
    for (source_name, other_name), hits in pair_findings.items():
        lines.append(f"*{source_name}*'s label mentions *{other_name}*:")
        for field_label, excerpt in hits:
            lines.append(f"  [{field_label}] “{excerpt}”")
        lines.append("")

    lines.append(
        "⚠️ These are raw text mentions pulled from each drug's own FDA label, *not* a verified "
        "interaction database -- and the absence of a mention above does *not* rule out an interaction "
        "either (see the note when you started this checker). Confirm anything you're actually going to "
        "act on with a dedicated interaction checker or a pharmacist."
    )
    return "\n".join(lines)


def register_interaction_handlers(dp) -> None:
    dp.include_router(router)

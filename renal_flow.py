"""
"Calculate dose by renal function" conversation flow.

Collects either a direct eGFR/CrCl value or the raw inputs needed to
calculate one (age, sex, creatinine, and weight for CrCl), computes it with
the standard formulas in renal_calc.py, and then shows that value next to
whatever the drug's own FDA label says about renal dosing (via
drug_lookup.find_renal_relevant_content).

IMPORTANT, by design: this flow never tells the user "give this dose". It
calculates the lab value (an exact, unambiguous computation) and surfaces
the label's own renal-dosing text/table, but leaves matching the value to
the applicable row to the user. Automatically parsing a drug's free-text
dosing table and asserting a specific dose would mean re-interpreting
FDA text well beyond what can be done reliably across the whole drug
database -- see drug_lookup.py's module docstring and
_extract_structured_blocks for the same reasoning applied to table
rendering.

Kept in its own Router (rather than added to bot.py's Dispatcher directly)
so the FSM state machine is self-contained; bot.py just calls
register_renal_handlers(dp) once at startup.
"""

import logging

from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import renal_calc
import session_cache
from drug_lookup import find_renal_relevant_content
from keyboards import renal_mode_kb, renal_sex_kb, renal_unit_kb, renal_cancel_kb
from telegram_helpers import send_long_text, send_table_entries

logger = logging.getLogger(__name__)

router = Router(name="renal_flow")


class RenalCalcStates(StatesGroup):
    choosing_mode = State()
    entering_direct_value = State()
    entering_age = State()
    choosing_sex = State()
    entering_weight = State()
    choosing_creatinine_unit = State()
    entering_creatinine = State()


_ALL_STATES = StateFilter(
    RenalCalcStates.choosing_mode,
    RenalCalcStates.entering_direct_value,
    RenalCalcStates.entering_age,
    RenalCalcStates.choosing_sex,
    RenalCalcStates.entering_weight,
    RenalCalcStates.choosing_creatinine_unit,
    RenalCalcStates.entering_creatinine,
)

# Text-entry handlers require F.text (so a stray photo/sticker doesn't crash
# them) and exclude anything starting with "/" (so /start, /dose, /cancel
# etc. fall through to their real handlers in bot.py instead of being
# swallowed and misread as e.g. an age).
_NOT_A_COMMAND = F.text & ~F.text.startswith("/")


@router.message(Command("cancel"), _ALL_STATES)
async def cmd_cancel_renal(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.callback_query(F.data == "rc:cancel")
async def handle_renal_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer("Cancelled.")


@router.callback_query(F.data.startswith("rc:start:"))
async def handle_renal_start(callback: CallbackQuery, state: FSMContext):
    try:
        _, _, cache_id = callback.data.split(":", 2)
    except ValueError:
        await callback.answer("Something went wrong with that button.", show_alert=True)
        return

    sections = session_cache.get(cache_id)
    if sections is None:
        await callback.answer("This lookup expired. Please run /dose again.", show_alert=True)
        return

    await callback.answer()
    await state.set_state(RenalCalcStates.choosing_mode)
    await state.update_data(cache_id=cache_id, drug_name=sections.get("_name", "this drug"))

    await callback.message.answer(
        f"How would you like to provide renal function for *{sections.get('_name', 'this drug')}*?\n\n"
        "eGFR and CrCl are different measurements (CrCl needs weight, eGFR doesn't) -- "
        "pick whichever matches what you have, or have me calculate one.",
        parse_mode="Markdown",
        reply_markup=renal_mode_kb(),
    )


async def _require_flow_data(callback_or_message, state: FSMContext) -> dict | None:
    """
    Guard against a stale/duplicate button tap (e.g. after /cancel, after
    the flow already finished, or after a bot restart wiped in-memory FSM
    storage). Returns the FSM data dict, or None (having already told the
    user) if the flow isn't actually active.
    """
    data = await state.get_data()
    if not data.get("cache_id"):
        await state.clear()
        text = "That button has expired. Please tap 'Calculate dose by renal function' again from /dose."
        if isinstance(callback_or_message, CallbackQuery):
            await callback_or_message.message.answer(text)
        else:
            await callback_or_message.answer(text)
        return None
    return data


@router.callback_query(F.data.startswith("rc:mode:"))
async def handle_renal_mode(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await _require_flow_data(callback, state)
    if data is None:
        return

    mode = callback.data.split(":", 2)[2]

    if mode == "egfr_direct":
        await state.update_data(value_type="eGFR", unit="mL/min/1.73m²")
        await state.set_state(RenalCalcStates.entering_direct_value)
        await callback.message.answer(
            "Enter the eGFR value in mL/min/1.73m² (e.g. 62):", reply_markup=renal_cancel_kb()
        )
    elif mode == "crcl_direct":
        await state.update_data(value_type="CrCl", unit="mL/min")
        await state.set_state(RenalCalcStates.entering_direct_value)
        await callback.message.answer(
            "Enter the CrCl value in mL/min (e.g. 55):", reply_markup=renal_cancel_kb()
        )
    elif mode == "egfr_calc":
        await state.update_data(calc_type="egfr")
        await state.set_state(RenalCalcStates.entering_age)
        await callback.message.answer(
            "Calculating eGFR (CKD-EPI 2021). Enter the patient's age in years:",
            reply_markup=renal_cancel_kb(),
        )
    elif mode == "crcl_calc":
        await state.update_data(calc_type="crcl")
        await state.set_state(RenalCalcStates.entering_age)
        await callback.message.answer(
            "Calculating CrCl (Cockcroft-Gault). Enter the patient's age in years:",
            reply_markup=renal_cancel_kb(),
        )
    else:
        await callback.message.answer("Unrecognized option -- please start over.")
        await state.clear()


@router.message(RenalCalcStates.entering_direct_value, _NOT_A_COMMAND)
async def handle_renal_direct_value(message: Message, state: FSMContext):
    data = await _require_flow_data(message, state)
    if data is None:
        return

    value_type = data.get("value_type", "value")
    unit = data.get("unit", "")
    try:
        value = renal_calc.validate_direct_value(message.text, value_type)
    except renal_calc.RenalInputError as e:
        await message.answer(f"⚠️ {e}\nPlease re-enter the {value_type} value:", reply_markup=renal_cancel_kb())
        return

    await _show_renal_results(message, state, value, value_type, unit, inputs_summary="entered directly")


@router.message(RenalCalcStates.entering_age, _NOT_A_COMMAND)
async def handle_renal_age(message: Message, state: FSMContext):
    data = await _require_flow_data(message, state)
    if data is None:
        return

    try:
        age = renal_calc.validate_age(message.text)
    except renal_calc.RenalInputError as e:
        await message.answer(f"⚠️ {e}\nPlease enter the age again:", reply_markup=renal_cancel_kb())
        return

    await state.update_data(age=age)
    await state.set_state(RenalCalcStates.choosing_sex)
    await message.answer("Sex? (this is a required input to the formula itself)", reply_markup=renal_sex_kb())


@router.callback_query(F.data.startswith("rc:sex:"))
async def handle_renal_sex(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await _require_flow_data(callback, state)
    if data is None:
        return

    sex = callback.data.split(":", 2)[2]
    await state.update_data(sex=sex)

    if data.get("calc_type") == "crcl":
        await state.set_state(RenalCalcStates.entering_weight)
        await callback.message.answer("Enter the patient's weight in kg:", reply_markup=renal_cancel_kb())
    else:
        await state.set_state(RenalCalcStates.choosing_creatinine_unit)
        await callback.message.answer(
            "What unit is the serum creatinine value in?", reply_markup=renal_unit_kb()
        )


@router.message(RenalCalcStates.entering_weight, _NOT_A_COMMAND)
async def handle_renal_weight(message: Message, state: FSMContext):
    data = await _require_flow_data(message, state)
    if data is None:
        return

    try:
        weight = renal_calc.validate_weight_kg(message.text)
    except renal_calc.RenalInputError as e:
        await message.answer(f"⚠️ {e}\nPlease enter the weight again (kg):", reply_markup=renal_cancel_kb())
        return

    await state.update_data(weight_kg=weight)
    await state.set_state(RenalCalcStates.choosing_creatinine_unit)
    await message.answer("What unit is the serum creatinine value in?", reply_markup=renal_unit_kb())


@router.callback_query(F.data.startswith("rc:unit:"))
async def handle_renal_unit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await _require_flow_data(callback, state)
    if data is None:
        return

    unit = callback.data.split(":", 2)[2]  # "mgdl" or "umol"
    await state.update_data(creatinine_unit=unit)
    await state.set_state(RenalCalcStates.entering_creatinine)

    unit_label = "µmol/L" if unit == "umol" else "mg/dL"
    await callback.message.answer(
        f"Enter the serum creatinine value ({unit_label}):", reply_markup=renal_cancel_kb()
    )


@router.message(RenalCalcStates.entering_creatinine, _NOT_A_COMMAND)
async def handle_renal_creatinine(message: Message, state: FSMContext):
    data = await _require_flow_data(message, state)
    if data is None:
        return

    try:
        raw_value = float(message.text.strip())
    except (TypeError, ValueError):
        await message.answer(
            "⚠️ Please enter a number for serum creatinine (e.g. 1.1). Try again:",
            reply_markup=renal_cancel_kb(),
        )
        return

    unit = data.get("creatinine_unit", "mgdl")
    unit_label = "µmol/L" if unit == "umol" else "mg/dL"
    mgdl_value = renal_calc.umol_to_mgdl(raw_value) if unit == "umol" else raw_value

    try:
        mgdl_value = renal_calc.validate_creatinine_mgdl(mgdl_value)
    except renal_calc.RenalInputError as e:
        await message.answer(f"⚠️ {e}\nPlease re-enter the serum creatinine value:", reply_markup=renal_cancel_kb())
        return

    age = data["age"]
    sex = data["sex"]
    calc_type = data.get("calc_type")

    try:
        if calc_type == "egfr":
            value = renal_calc.calculate_egfr_ckdepi_2021(age=age, sex=sex, creatinine_mg_dl=mgdl_value)
            value_type, unit_out = "eGFR", "mL/min/1.73m²"
            inputs_summary = f"age {age:g}, sex {sex}, creatinine {raw_value:g} {unit_label} (CKD-EPI 2021)"
        else:
            weight = data["weight_kg"]
            value = renal_calc.calculate_crcl_cockcroft_gault(
                age=age, sex=sex, weight_kg=weight, creatinine_mg_dl=mgdl_value
            )
            value_type, unit_out = "CrCl", "mL/min"
            inputs_summary = (
                f"age {age:g}, sex {sex}, weight {weight:g} kg, creatinine {raw_value:g} {unit_label} "
                "(Cockcroft-Gault, actual body weight)"
            )
    except renal_calc.RenalInputError as e:
        await message.answer(f"⚠️ Couldn't calculate: {e}\nPlease start over from the button.")
        await state.clear()
        return
    except Exception:
        logger.exception("Renal calculation failed unexpectedly")
        await message.answer("Something went wrong computing that. Please start over from the button.")
        await state.clear()
        return

    await _show_renal_results(message, state, value, value_type, unit_out, inputs_summary)


@router.message(StateFilter(RenalCalcStates.choosing_mode, RenalCalcStates.choosing_sex, RenalCalcStates.choosing_creatinine_unit), _NOT_A_COMMAND)
async def handle_renal_stray_text(message: Message):
    """User typed instead of tapping a button during a choice step."""
    await message.answer("Please tap one of the buttons above (or /cancel to stop).")


async def _show_renal_results(
    message: Message, state: FSMContext, value: float, value_type: str, unit: str, inputs_summary: str
) -> None:
    data = await state.get_data()
    cache_id = data.get("cache_id")
    drug_name = data.get("drug_name", "this drug")
    sections = session_cache.get(cache_id) if cache_id else None

    if value_type == "eGFR":
        category_line = f"KDIGO stage: *{renal_calc.ckd_egfr_stage(value)}*"
    else:
        category_line = f"Category: *{renal_calc.crcl_category(value)}*"

    lines = [
        f"🧮 *Renal function for {drug_name}*",
        "",
        f"*{value_type}: {value:.0f} {unit}*",
        category_line,
        f"_(calculated from: {inputs_summary})_" if inputs_summary != "entered directly" else "_(entered directly)_",
        "",
        "This is a calculated estimate only -- the bot does *not* select a "
        "dose for you. Match this value to the applicable row in the "
        "label's own renal-dosing information below, using clinical "
        "judgment and a current formulary.",
    ]

    ok = await send_long_text(message.answer, "\n".join(lines))
    if not ok:
        await message.answer("Couldn't send the calculated value (Telegram rejected the message).")

    if sections is None:
        await message.answer(
            "This lookup has expired, so I can't show the drug's renal-dosing info alongside it -- "
            "run /dose again if you need that too."
        )
    else:
        bullets, tables = find_renal_relevant_content(sections)
        if not bullets and not tables:
            await message.answer(
                f"No renal-specific dosing information was found in {drug_name}'s FDA label. "
                "Check a current formulary before dosing in renal impairment."
            )
        else:
            if bullets:
                await send_long_text(
                    message.answer,
                    f"📋 *Renal-relevant label content for {drug_name}:*\n\n" + "\n\n".join(bullets),
                )
            if tables:
                await send_table_entries(message, drug_name, tables)

    await state.clear()


def register_renal_handlers(dp) -> None:
    dp.include_router(router)

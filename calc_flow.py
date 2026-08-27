"""
Generic, declarative engine for all the "plain math" clinical calculators in
clinical_calc.py (BMI/BSA, corrected calcium/sodium, anion gap, maintenance
fluids, QTc, the three MELD variants, CHA2DS2-VASc, and both Wells' scores).

Rather than hand-writing a separate FSM conversation per calculator (as
renal_flow.py does for the one calculator that also needs to cross-reference
a drug's FDA label), each calculator is declared once as a small list of
"fields" plus a compute() function, and a single generic FSM state machine
walks the user through whichever calculator they picked.

Field kinds:
  - NumberField:  free-text numeric entry, validated by one of
                   clinical_calc's `validate_*` functions.
  - ChoiceField:  a fixed set of button options (label, value) pairs.
  - YesNoField:   a ChoiceField preset to Yes/No -> True/False.

A field can set `skip_if=lambda values: bool` to be conditionally skipped
based on answers already given (used by anion_gap's optional albumin
correction) -- skipped fields are never asked and never appear in `values`.

Every calculator here is exact math with no clinical judgment applied by the
bot -- see clinical_calc.py's module docstring. compute() functions only
format the numbers that formula produces; any qualitative label (KDIGO-style
stage, "prolonged", risk tier, etc.) is a static, published lookup already
implemented in clinical_calc.py, never something inferred here.
"""

import logging

from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import clinical_calc as cc
from keyboards import calc_menu_kb, calc_cancel_kb, calc_choice_kb
from telegram_helpers import send_long_text

logger = logging.getLogger(__name__)

router = Router(name="calc_flow")


class CalcFlowStates(StatesGroup):
    in_progress = State()


_NOT_A_COMMAND = F.text & ~F.text.startswith("/")


# ---------------------------------------------------------------------------
# Field types
# ---------------------------------------------------------------------------

class NumberField:
    kind = "number"

    def __init__(self, key, prompt, validator, skip_if=None):
        self.key = key
        self.prompt = prompt
        self.validator = validator  # str -> float, raises cc.CalcInputError
        self.skip_if = skip_if


class ChoiceField:
    kind = "choice"

    def __init__(self, key, prompt, options, skip_if=None):
        self.key = key
        self.prompt = prompt
        self.options = options  # list of (label, value)
        self.skip_if = skip_if


class YesNoField(ChoiceField):
    def __init__(self, key, prompt, skip_if=None):
        super().__init__(key, prompt, options=[("Yes", True), ("No", False)], skip_if=skip_if)


_SEX_FIELD = lambda key="sex", prompt="Patient sex?": ChoiceField(  # noqa: E731
    key, prompt, options=[("Male", "M"), ("Female", "F")]
)


class Calculator:
    def __init__(self, calc_id, title, emoji, fields, compute, intro=None):
        self.id = calc_id
        self.title = title
        self.emoji = emoji
        self.fields = fields
        self.compute = compute  # dict -> list[str] (result lines)
        self.intro = intro  # optional one-line note shown when the flow starts


# ---------------------------------------------------------------------------
# Compute functions -- format clinical_calc.py's outputs into result lines.
# All inputs arriving here have already passed their field validators.
# ---------------------------------------------------------------------------

def _compute_bmi_bsa(v):
    bmi = cc.calculate_bmi(v["weight_kg"], v["height_cm"])
    bsa_m = cc.calculate_bsa_mosteller(v["weight_kg"], v["height_cm"])
    bsa_d = cc.calculate_bsa_dubois(v["weight_kg"], v["height_cm"])
    return [
        f"*BMI:* {bmi:.1f} kg/m² -- {cc.bmi_category(bmi)}",
        f"*BSA (Mosteller):* {bsa_m:.2f} m²",
        f"*BSA (DuBois):* {bsa_d:.2f} m²",
        "_(the two BSA formulas normally agree within ~0.05 m²; shown both since different institutions default to different ones)_",
    ]


def _compute_corrected_calcium(v):
    corrected = cc.corrected_calcium(v["calcium_mgdl"], v["albumin_gdl"])
    return [
        f"*Corrected calcium:* {corrected:.2f} mg/dL",
        f"_(measured {v['calcium_mgdl']:.2f} mg/dL, albumin {v['albumin_gdl']:.2f} g/dL, Payne formula)_",
        "This correction is an estimate -- a directly measured ionized calcium is more reliable when available.",
    ]


def _compute_corrected_sodium(v):
    katz = cc.corrected_sodium_katz(v["sodium_meql"], v["glucose_mgdl"])
    hillier = cc.corrected_sodium_hillier(v["sodium_meql"], v["glucose_mgdl"])
    return [
        f"*Corrected sodium (Katz, factor 1.6):* {katz:.1f} mEq/L",
        f"*Corrected sodium (Hillier, factor 2.4):* {hillier:.1f} mEq/L",
        "_(Hillier's higher factor was derived from data including more severe hyperglycemia; "
        "Katz's 1.6 is the more commonly cited default. Neither is universally 'correct'.)_",
    ]


def _compute_anion_gap(v):
    ag = cc.anion_gap(v["sodium_meql"], v["chloride_meql"], v["bicarbonate_meql"])
    lines = [f"*Anion gap:* {ag:.1f} mEq/L (reference range ~8-12, assay-dependent)"]
    if v.get("correct_for_albumin"):
        corrected = cc.corrected_anion_gap(ag, v["albumin_gdl"])
        lines.append(
            f"*Albumin-corrected anion gap:* {corrected:.1f} mEq/L "
            f"(albumin {v['albumin_gdl']:.2f} g/dL -- hypoalbuminemia masks a raised gap)"
        )
    return lines


def _compute_fluids(v):
    rate = cc.maintenance_fluid_rate_ml_per_hr(v["weight_kg"])
    return [
        f"*Maintenance IV fluid rate:* {rate:.0f} mL/hr ({rate * 24:.0f} mL/day)",
        "_(Holliday-Segar '4-2-1' rule -- most validated in children; adjust for fever, "
        "losses, cardiac/renal status, and use clinical judgment for adults.)_",
    ]


def _compute_qtc(v):
    bazett = cc.qtc_bazett(v["qt_ms"], v["heart_rate"])
    frid = cc.qtc_fridericia(v["qt_ms"], v["heart_rate"])
    return [
        f"*QTc (Bazett):* {bazett:.0f} ms -- {cc.qtc_interpretation(bazett, v['sex'])}",
        f"*QTc (Fridericia):* {frid:.0f} ms -- {cc.qtc_interpretation(frid, v['sex'])}",
        "_(Bazett over-corrects at heart rates far from 60 bpm; Fridericia is usually preferred at extremes. "
        f"Thresholds used: >440ms male / >460ms female = prolonged.)_",
    ]


def _meld_result_lines(label, score, extra_note=""):
    return [
        f"*{label}: {score:.0f}*",
        "_(range 6-40; used for transplant-allocation prioritization -- always cross-check "
        "against your institution's calculator before using this for anything clinical.)_",
    ] + ([extra_note] if extra_note else [])


def _compute_meld_classic(v):
    score = cc.calculate_meld(v["bilirubin_mgdl"], v["inr"], v["creatinine_mgdl"], v["on_dialysis"])
    return _meld_result_lines("MELD (classic)", score)


def _compute_meld_na(v):
    score = cc.calculate_meld_na(v["bilirubin_mgdl"], v["inr"], v["creatinine_mgdl"], v["sodium_meql"], v["on_dialysis"])
    return _meld_result_lines("MELD-Na", score)


def _compute_meld3(v):
    score = cc.calculate_meld3(
        v["sex"], v["bilirubin_mgdl"], v["inr"], v["creatinine_mgdl"], v["sodium_meql"], v["albumin_gdl"], v["on_dialysis"]
    )
    return _meld_result_lines(
        "MELD 3.0", score,
        extra_note="⚠️ This formula was implemented from memory without a live source to check it against -- "
                    "please verify it against an official MDCalc/OPTN calculator before relying on it for anything beyond a rough estimate.",
    )


def _compute_cha2ds2_vasc(v):
    score = cc.cha2ds2_vasc_score(
        chf=v["chf"], hypertension=v["hypertension"], age=v["age"], diabetes=v["diabetes"],
        stroke_history=v["stroke_history"], vascular_disease=v["vascular_disease"], sex_female=(v["sex"] == "F"),
    )
    tier = cc.cha2ds2_vasc_risk_tier(score)
    return [
        f"*CHA₂DS₂-VASc score: {score}* -- {tier} stroke-risk category",
        "_(qualitative tier only -- specific annual stroke-risk percentages vary between published "
        "cohorts, so none is quoted here; check current guideline-recommended anticoagulation thresholds.)_",
    ]


def _compute_wells_pe(v):
    score = cc.wells_pe_score(
        dvt_signs=v["dvt_signs"], pe_most_likely=v["pe_most_likely"], hr_over_100=v["hr_over_100"],
        immobilization_or_surgery=v["immobilization_or_surgery"], prior_dvt_pe=v["prior_dvt_pe"],
        hemoptysis=v["hemoptysis"], malignancy=v["malignancy"],
    )
    return [
        f"*Wells' Criteria for PE: {score:g} points*",
        cc.wells_pe_interpretation(score),
    ]


def _compute_wells_dvt(v):
    score = cc.wells_dvt_score(
        active_cancer=v["active_cancer"], paralysis_or_immobilization=v["paralysis_or_immobilization"],
        bedridden_or_surgery=v["bedridden_or_surgery"], tenderness_deep_veins=v["tenderness_deep_veins"],
        entire_leg_swollen=v["entire_leg_swollen"], calf_swelling_3cm=v["calf_swelling_3cm"],
        pitting_edema=v["pitting_edema"], collateral_veins=v["collateral_veins"], prior_dvt=v["prior_dvt"],
        alternative_diagnosis_as_likely=v["alternative_diagnosis_as_likely"],
    )
    return [
        f"*Wells' Criteria for DVT: {score} points*",
        cc.wells_dvt_interpretation(score),
    ]


# ---------------------------------------------------------------------------
# Calculator registry
# ---------------------------------------------------------------------------

CALCULATORS: dict[str, Calculator] = {}


def _register(calc: Calculator) -> None:
    CALCULATORS[calc.id] = calc


_register(Calculator(
    "bmi_bsa", "BMI & BSA", "📏",
    fields=[
        NumberField("weight_kg", "Enter weight in kg:", cc.validate_weight_kg),
        NumberField("height_cm", "Enter height in cm:", cc.validate_height_cm),
    ],
    compute=_compute_bmi_bsa,
))

_register(Calculator(
    "corrected_calcium", "Corrected Calcium", "🦴",
    fields=[
        NumberField("calcium_mgdl", "Enter serum calcium (mg/dL):", cc.validate_calcium_mgdl),
        NumberField("albumin_gdl", "Enter serum albumin (g/dL):", cc.validate_albumin_gdl),
    ],
    compute=_compute_corrected_calcium,
))

_register(Calculator(
    "corrected_sodium", "Corrected Sodium", "🧂",
    fields=[
        NumberField("sodium_meql", "Enter measured serum sodium (mEq/L):", cc.validate_sodium_meql),
        NumberField("glucose_mgdl", "Enter serum glucose (mg/dL):", cc.validate_glucose_mgdl),
    ],
    compute=_compute_corrected_sodium,
    intro="For hyperglycemia-related pseudo-hyponatremia. Shows both commonly-used correction factors.",
))

_register(Calculator(
    "anion_gap", "Anion Gap", "⚗️",
    fields=[
        NumberField("sodium_meql", "Enter serum sodium (mEq/L):", cc.validate_sodium_meql),
        NumberField("chloride_meql", "Enter serum chloride (mEq/L):", cc.validate_chloride_meql),
        NumberField("bicarbonate_meql", "Enter serum bicarbonate/HCO3 (mEq/L):", cc.validate_bicarbonate_meql),
        YesNoField("correct_for_albumin", "Correct for hypoalbuminemia? (adds 2.5 x (4 - albumin) to the gap)"),
        NumberField(
            "albumin_gdl", "Enter serum albumin (g/dL):", cc.validate_albumin_gdl,
            skip_if=lambda v: not v.get("correct_for_albumin"),
        ),
    ],
    compute=_compute_anion_gap,
))

_register(Calculator(
    "maintenance_fluids", "Maintenance IV Fluids", "💧",
    fields=[
        NumberField("weight_kg", "Enter weight in kg:", cc.validate_weight_kg),
    ],
    compute=_compute_fluids,
    intro="Holliday-Segar '4-2-1' rule.",
))

_register(Calculator(
    "qtc", "QTc (Bazett & Fridericia)", "❤️",
    fields=[
        NumberField("qt_ms", "Enter the measured QT interval (ms):", cc.validate_qt_ms),
        NumberField("heart_rate", "Enter heart rate (bpm):", cc.validate_heart_rate),
        _SEX_FIELD(),
    ],
    compute=_compute_qtc,
))

_register(Calculator(
    "meld_classic", "MELD (Classic)", "🧪",
    fields=[
        NumberField("bilirubin_mgdl", "Enter total bilirubin (mg/dL):", cc.validate_bilirubin_mgdl),
        NumberField("inr", "Enter INR:", cc.validate_inr),
        NumberField("creatinine_mgdl", "Enter serum creatinine (mg/dL):", cc.validate_creatinine_mgdl),
        YesNoField("on_dialysis", "Has the patient had >=2 dialysis sessions in the past week (or >=24h CVVHD)?"),
    ],
    compute=_compute_meld_classic,
))

_register(Calculator(
    "meld_na", "MELD-Na", "🧪",
    fields=[
        NumberField("bilirubin_mgdl", "Enter total bilirubin (mg/dL):", cc.validate_bilirubin_mgdl),
        NumberField("inr", "Enter INR:", cc.validate_inr),
        NumberField("creatinine_mgdl", "Enter serum creatinine (mg/dL):", cc.validate_creatinine_mgdl),
        NumberField("sodium_meql", "Enter serum sodium (mEq/L):", cc.validate_sodium_meql),
        YesNoField("on_dialysis", "Has the patient had >=2 dialysis sessions in the past week (or >=24h CVVHD)?"),
    ],
    compute=_compute_meld_na,
))

_register(Calculator(
    "meld3", "MELD 3.0", "🧪",
    fields=[
        _SEX_FIELD(),
        NumberField("bilirubin_mgdl", "Enter total bilirubin (mg/dL):", cc.validate_bilirubin_mgdl),
        NumberField("inr", "Enter INR:", cc.validate_inr),
        NumberField("creatinine_mgdl", "Enter serum creatinine (mg/dL):", cc.validate_creatinine_mgdl),
        NumberField("sodium_meql", "Enter serum sodium (mEq/L):", cc.validate_sodium_meql),
        NumberField("albumin_gdl", "Enter serum albumin (g/dL):", cc.validate_albumin_gdl),
        YesNoField("on_dialysis", "Has the patient had >=2 dialysis sessions in the past week (or >=24h CVVHD)?"),
    ],
    compute=_compute_meld3,
    intro="⚠️ Implemented from memory without a live reference to check -- treat as a rough estimate and verify independently.",
))

_register(Calculator(
    "cha2ds2_vasc", "CHA₂DS₂-VASc", "🩺",
    fields=[
        NumberField("age", "Enter age in years:", cc.validate_age),
        _SEX_FIELD(),
        YesNoField("chf", "History of congestive heart failure / LV dysfunction?"),
        YesNoField("hypertension", "History of hypertension?"),
        YesNoField("diabetes", "History of diabetes mellitus?"),
        YesNoField("stroke_history", "Prior stroke, TIA, or thromboembolism?"),
        YesNoField("vascular_disease", "Vascular disease (prior MI, PAD, or aortic plaque)?"),
    ],
    compute=_compute_cha2ds2_vasc,
))

_register(Calculator(
    "wells_pe", "Wells' Criteria (PE)", "🫁",
    fields=[
        YesNoField("dvt_signs", "Clinical signs/symptoms of DVT (leg swelling, pain on palpation)?"),
        YesNoField("pe_most_likely", "Is PE the #1 most likely diagnosis (more likely than any alternative)?"),
        YesNoField("hr_over_100", "Heart rate over 100 bpm?"),
        YesNoField("immobilization_or_surgery", "Immobilization >=3 days, or surgery in the past 4 weeks?"),
        YesNoField("prior_dvt_pe", "Previous objectively diagnosed DVT or PE?"),
        YesNoField("hemoptysis", "Hemoptysis?"),
        YesNoField("malignancy", "Malignancy (treated within 6 months, or palliative)?"),
    ],
    compute=_compute_wells_pe,
    intro="Wells' score for pulmonary embolism (this is a different scoring system from Wells' DVT below).",
))

_register(Calculator(
    "wells_dvt", "Wells' Criteria (DVT)", "🦵",
    fields=[
        YesNoField("active_cancer", "Active cancer (treatment ongoing, within 6 months, or palliative)?"),
        YesNoField("paralysis_or_immobilization", "Paralysis, paresis, or recent lower-extremity immobilization (cast)?"),
        YesNoField("bedridden_or_surgery", "Recently bedridden >=3 days, or major surgery within 12 weeks?"),
        YesNoField("tenderness_deep_veins", "Localized tenderness along the deep venous system?"),
        YesNoField("entire_leg_swollen", "Entire leg swollen?"),
        YesNoField("calf_swelling_3cm", "Calf swelling >3cm compared to the asymptomatic leg?"),
        YesNoField("pitting_edema", "Pitting edema confined to the symptomatic leg?"),
        YesNoField("collateral_veins", "Collateral (non-varicose) superficial veins present?"),
        YesNoField("prior_dvt", "Previously documented DVT?"),
        YesNoField("alternative_diagnosis_as_likely", "Is an alternative diagnosis at least as likely as DVT? (subtracts 2 points)"),
    ],
    compute=_compute_wells_dvt,
    intro="Wells' score for DVT (this is a different scoring system from Wells' PE above).",
))


_MENU_ORDER = [
    "bmi_bsa", "corrected_calcium", "corrected_sodium", "anion_gap", "maintenance_fluids", "qtc",
    "meld_classic", "meld_na", "meld3", "cha2ds2_vasc", "wells_pe", "wells_dvt",
]


def calculators_menu_kb():
    return calc_menu_kb([(cid, CALCULATORS[cid].title, CALCULATORS[cid].emoji) for cid in _MENU_ORDER])


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

async def _require_flow_data(callback_or_message, state: FSMContext) -> dict | None:
    data = await state.get_data()
    if not data.get("calc_id"):
        await state.clear()
        text = "That button has expired. Please open /calculators again."
        if isinstance(callback_or_message, CallbackQuery):
            await callback_or_message.message.answer(text)
        else:
            await callback_or_message.answer(text)
        return None
    return data


def _skip_forward(calc: Calculator, idx: int, values: dict) -> int:
    """Advance idx past any fields whose skip_if(values) is true."""
    while idx < len(calc.fields) and calc.fields[idx].skip_if and calc.fields[idx].skip_if(values):
        idx += 1
    return idx


async def _ask_field(answer_fn, field, idx: int) -> None:
    if field.kind == "number":
        await answer_fn(field.prompt, reply_markup=calc_cancel_kb())
    else:
        await answer_fn(field.prompt, reply_markup=calc_choice_kb(idx, field.options))


async def _start_or_continue(answer_fn, state: FSMContext, calc: Calculator, idx: int, values: dict) -> None:
    idx = _skip_forward(calc, idx, values)
    if idx >= len(calc.fields):
        try:
            lines = calc.compute(values)
        except Exception:
            logger.exception("Calculator '%s' compute() failed on values=%r", calc.id, values)
            await answer_fn("Something went wrong computing that result. Please start over from /calculators.")
            await state.clear()
            return
        header = f"{calc.emoji} *{calc.title} -- result*"
        ok = await send_long_text(answer_fn, header + "\n\n" + "\n".join(lines))
        if not ok:
            await answer_fn("Couldn't send the result (Telegram rejected the message).")
        await state.clear()
        return

    await state.update_data(field_index=idx, values=values)
    await _ask_field(answer_fn, calc.fields[idx], idx)


@router.message(Command("calculators"))
async def cmd_calculators(message: Message):
    await message.answer(
        "🧮 Pick a calculator. Each one is a standard published formula -- "
        "no clinical judgment is applied by the bot; use the result with your own assessment.",
        reply_markup=calculators_menu_kb(),
    )


@router.message(Command("cancel"), CalcFlowStates.in_progress)
async def cmd_cancel_calc(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Cancelled.")


@router.callback_query(F.data == "cf:cancel")
async def handle_calc_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer()
    await callback.message.answer("Cancelled.")


@router.callback_query(F.data.startswith("cf:start:"))
async def handle_calc_start(callback: CallbackQuery, state: FSMContext):
    calc_id = callback.data.split(":", 2)[2]
    calc = CALCULATORS.get(calc_id)
    if calc is None:
        await callback.answer("Unknown calculator.", show_alert=True)
        return

    await callback.answer()
    await state.set_state(CalcFlowStates.in_progress)
    await state.update_data(calc_id=calc.id)
    if calc.intro:
        await callback.message.answer(f"{calc.emoji} *{calc.title}*\n_{calc.intro}_", parse_mode="Markdown")
    await _start_or_continue(callback.message.answer, state, calc, 0, {})


@router.message(CalcFlowStates.in_progress, _NOT_A_COMMAND)
async def handle_calc_text(message: Message, state: FSMContext):
    data = await _require_flow_data(message, state)
    if data is None:
        return

    calc = CALCULATORS.get(data["calc_id"])
    idx = data["field_index"]
    if calc is None or idx >= len(calc.fields):
        await message.answer("Something went out of sync -- please start over from /calculators.")
        await state.clear()
        return

    field = calc.fields[idx]
    if field.kind != "number":
        await message.answer("Please tap one of the buttons above (or /cancel to stop).")
        return

    try:
        value = field.validator(message.text)
    except cc.CalcInputError as e:
        await message.answer(f"⚠️ {e}\nPlease re-enter that value:", reply_markup=calc_cancel_kb())
        return

    values = dict(data["values"])
    values[field.key] = value
    await _start_or_continue(message.answer, state, calc, idx + 1, values)


@router.callback_query(F.data.startswith("cf:ans:"))
async def handle_calc_choice(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await _require_flow_data(callback, state)
    if data is None:
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.message.answer("Something went wrong with that button.")
        return
    _, _, field_index_str, opt_index_str = parts

    calc = CALCULATORS.get(data["calc_id"])
    idx = data["field_index"]
    if calc is None or int(field_index_str) != idx:
        await callback.message.answer(
            "That button is from an earlier step and no longer applies. "
            "Please continue from the current prompt, or /cancel to restart."
        )
        return

    if idx >= len(calc.fields) or calc.fields[idx].kind != "choice":
        await callback.message.answer("Something went out of sync -- please start over from /calculators.")
        await state.clear()
        return

    field = calc.fields[idx]
    try:
        opt_index = int(opt_index_str)
        _label, value = field.options[opt_index]
    except (ValueError, IndexError):
        await callback.message.answer("Something went wrong with that button.")
        return

    values = dict(data["values"])
    values[field.key] = value
    await _start_or_continue(callback.message.answer, state, calc, idx + 1, values)


def register_calc_handlers(dp) -> None:
    dp.include_router(router)

"""
Pure-math clinical calculators: BMI/BSA, corrected calcium, corrected
sodium, anion gap, maintenance IV fluids, QTc, MELD (three versions),
CHA2DS2-VASc, and Wells' criteria (PE and DVT).

Same philosophy as renal_calc.py: these are exact, published, standard
formulas -- no clinical judgment or interpretation is exercised by this
module. Every function returns the RAW number(s); any category label
(e.g. "moderately decreased") is a widely-published, static lookup table,
never a generated/inferred judgment.

Confidence note on MELD 3.0 specifically: it's a newer formula (Kim WR et
al., Gastroenterology 2021) with more terms/coefficients than classic MELD
or MELD-Na, and this implementation was written from training-data
recollection without being able to check a live reference. Classic MELD
and MELD-Na are extremely standardized and long-established; MELD 3.0 is
the one entry here to independently verify against an institutional
calculator before trusting it for anything beyond a rough estimate.
"""

import math


class CalcInputError(ValueError):
    """Raised when an input is missing, non-numeric, or physiologically implausible."""


def _num(value, name: str, low: float, high: float) -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise CalcInputError(f"{name} is required.")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise CalcInputError(f"{name} must be a number.")
    if value != value or value in (float("inf"), float("-inf")):
        raise CalcInputError(f"{name} must be a real number.")
    if not (low <= value <= high):
        raise CalcInputError(
            f"{name} of {value:g} is outside the plausible range ({low:g}-{high:g}) -- please double-check it."
        )
    return value


# ---------------------------------------------------------------------------
# Validators (one per distinct kind of input, shared across calculators)
# ---------------------------------------------------------------------------

def validate_weight_kg(v) -> float:
    return _num(v, "Weight", 1, 400)


def validate_height_cm(v) -> float:
    return _num(v, "Height", 30, 250)


def validate_calcium_mgdl(v) -> float:
    return _num(v, "Serum calcium", 3, 20)


def validate_albumin_gdl(v) -> float:
    return _num(v, "Serum albumin", 0.5, 6)


def validate_sodium_meql(v) -> float:
    return _num(v, "Serum sodium", 100, 180)


def validate_glucose_mgdl(v) -> float:
    return _num(v, "Glucose", 20, 2000)


def validate_chloride_meql(v) -> float:
    return _num(v, "Serum chloride", 60, 150)


def validate_bicarbonate_meql(v) -> float:
    return _num(v, "Serum bicarbonate (HCO3)", 2, 50)


def validate_qt_ms(v) -> float:
    return _num(v, "QT interval", 100, 800)


def validate_heart_rate(v) -> float:
    return _num(v, "Heart rate", 20, 300)


def validate_bilirubin_mgdl(v) -> float:
    return _num(v, "Total bilirubin", 0.1, 50)


def validate_inr(v) -> float:
    return _num(v, "INR", 0.5, 20)


def validate_creatinine_mgdl(v) -> float:
    return _num(v, "Serum creatinine", 0.1, 25)


def validate_age(v) -> float:
    return _num(v, "Age", 0, 120)


# ---------------------------------------------------------------------------
# BMI + BSA
# ---------------------------------------------------------------------------

def calculate_bmi(weight_kg: float, height_cm: float) -> float:
    height_m = height_cm / 100
    return weight_kg / (height_m ** 2)


def bmi_category(bmi: float) -> str:
    if bmi < 18.5:
        return "underweight"
    if bmi < 25:
        return "normal weight"
    if bmi < 30:
        return "overweight"
    if bmi < 35:
        return "obese (class I)"
    if bmi < 40:
        return "obese (class II)"
    return "obese (class III)"


def calculate_bsa_mosteller(weight_kg: float, height_cm: float) -> float:
    return math.sqrt((height_cm * weight_kg) / 3600)


def calculate_bsa_dubois(weight_kg: float, height_cm: float) -> float:
    return 0.007184 * (height_cm ** 0.725) * (weight_kg ** 0.425)


# ---------------------------------------------------------------------------
# Corrected calcium / corrected sodium
# ---------------------------------------------------------------------------

def corrected_calcium(calcium_mgdl: float, albumin_gdl: float) -> float:
    """Payne formula: corrected Ca = measured Ca + 0.8 * (4.0 - albumin)."""
    return calcium_mgdl + 0.8 * (4.0 - albumin_gdl)


def corrected_sodium_katz(sodium_meql: float, glucose_mgdl: float) -> float:
    """Katz correction, factor 1.6 -- the more commonly taught default."""
    return sodium_meql + 1.6 * ((glucose_mgdl - 100) / 100)


def corrected_sodium_hillier(sodium_meql: float, glucose_mgdl: float) -> float:
    """Hillier correction, factor 2.4 -- derived empirically, gives a larger correction at high glucose."""
    return sodium_meql + 2.4 * ((glucose_mgdl - 100) / 100)


# ---------------------------------------------------------------------------
# Anion gap
# ---------------------------------------------------------------------------

def anion_gap(sodium: float, chloride: float, bicarbonate: float) -> float:
    return sodium - (chloride + bicarbonate)


def corrected_anion_gap(ag: float, albumin_gdl: float) -> float:
    """Adjusts the anion gap for hypoalbuminemia, which lowers the measured AG independent of acid-base status."""
    return ag + 2.5 * (4.0 - albumin_gdl)


# ---------------------------------------------------------------------------
# Maintenance IV fluids (Holliday-Segar "4-2-1" rule)
# ---------------------------------------------------------------------------

def maintenance_fluid_rate_ml_per_hr(weight_kg: float) -> float:
    if weight_kg <= 10:
        return 4 * weight_kg
    if weight_kg <= 20:
        return 40 + 2 * (weight_kg - 10)
    return 60 + 1 * (weight_kg - 20)


# ---------------------------------------------------------------------------
# QTc
# ---------------------------------------------------------------------------

def qtc_bazett(qt_ms: float, heart_rate_bpm: float) -> float:
    rr_seconds = 60 / heart_rate_bpm
    return qt_ms / math.sqrt(rr_seconds)


def qtc_fridericia(qt_ms: float, heart_rate_bpm: float) -> float:
    rr_seconds = 60 / heart_rate_bpm
    return qt_ms / (rr_seconds ** (1 / 3))


def qtc_interpretation(qtc_ms: float, sex: str) -> str:
    threshold = 460 if sex.upper() == "F" else 440
    return "prolonged" if qtc_ms > threshold else "normal"


# ---------------------------------------------------------------------------
# MELD (three versions)
# ---------------------------------------------------------------------------

def _meld_clamp_labs(bilirubin: float, inr: float, creatinine: float, on_dialysis: bool, cr_cap: float) -> tuple:
    bilirubin = max(bilirubin, 1.0)
    inr = max(inr, 1.0)
    creatinine = cr_cap if on_dialysis else min(max(creatinine, 1.0), cr_cap)
    return bilirubin, inr, creatinine


def calculate_meld(bilirubin_mgdl: float, inr: float, creatinine_mgdl: float, on_dialysis: bool) -> float:
    """
    Classic MELD (OPTN formula). If on_dialysis (>=2 dialysis sessions in
    the prior week, or >=24h CVVHD), creatinine is fixed at 4.0 regardless
    of the measured value, per OPTN policy.
    """
    bilirubin, inr, creatinine = _meld_clamp_labs(bilirubin_mgdl, inr, creatinine_mgdl, on_dialysis, cr_cap=4.0)
    score = 10 * (0.957 * math.log(creatinine) + 0.378 * math.log(bilirubin) + 1.120 * math.log(inr) + 0.643)
    return min(max(round(score), 6), 40)


def calculate_meld_na(
    bilirubin_mgdl: float, inr: float, creatinine_mgdl: float, sodium_meql: float, on_dialysis: bool
) -> float:
    """MELD-Na: applies a sodium adjustment to the classic MELD score when MELD > 11."""
    meld = calculate_meld(bilirubin_mgdl, inr, creatinine_mgdl, on_dialysis)
    if meld <= 11:
        return meld
    na = min(max(sodium_meql, 125), 137)
    meld_na = meld + 1.32 * (137 - na) - (0.033 * meld * (137 - na))
    return min(max(round(meld_na), 6), 40)


def calculate_meld3(
    sex: str,
    bilirubin_mgdl: float,
    inr: float,
    creatinine_mgdl: float,
    sodium_meql: float,
    albumin_gdl: float,
    on_dialysis: bool,
) -> float:
    """
    MELD 3.0 (Kim WR et al., Gastroenterology 2021). See module docstring --
    this is the one formula here to independently verify before relying on
    it; it was implemented from recollection of a newer, more complex
    formula without being able to check a live source.
    """
    bilirubin = max(bilirubin_mgdl, 1.0)
    inr_v = max(inr, 1.0)
    creatinine = 3.0 if on_dialysis else min(max(creatinine_mgdl, 1.0), 3.0)
    na = min(max(sodium_meql, 125), 137)
    albumin = min(max(albumin_gdl, 1.5), 3.5)
    is_female = sex.upper() == "F"

    score = (
        (1.33 if is_female else 0)
        + 4.56 * math.log(bilirubin)
        + 0.82 * (137 - na)
        - 0.24 * (137 - na) * math.log(bilirubin)
        + 9.09 * math.log(inr_v)
        + 11.14 * math.log(creatinine)
        + 1.85 * (3.5 - albumin)
        - 1.83 * (3.5 - albumin) * math.log(creatinine)
        + 6
    )
    return min(max(round(score), 6), 40)


# ---------------------------------------------------------------------------
# CHA2DS2-VASc
# ---------------------------------------------------------------------------

def cha2ds2_vasc_score(
    chf: bool, hypertension: bool, age: float, diabetes: bool, stroke_history: bool,
    vascular_disease: bool, sex_female: bool,
) -> int:
    score = 0
    score += 1 if chf else 0
    score += 1 if hypertension else 0
    score += 2 if age >= 75 else (1 if age >= 65 else 0)
    score += 1 if diabetes else 0
    score += 2 if stroke_history else 0
    score += 1 if vascular_disease else 0
    score += 1 if sex_female else 0
    return score


def cha2ds2_vasc_risk_tier(score: int) -> str:
    """Broad qualitative tier only -- deliberately not quoting a specific annual stroke-risk percentage
    (published estimates vary meaningfully between derivation cohorts, and misquoting one from memory
    without a source to check would be worse than not giving a number at all)."""
    if score == 0:
        return "low"
    if score == 1:
        return "low-to-moderate"
    return "moderate-to-high"


# ---------------------------------------------------------------------------
# Wells' criteria (PE and DVT are different scores)
# ---------------------------------------------------------------------------

def wells_pe_score(
    dvt_signs: bool, pe_most_likely: bool, hr_over_100: bool,
    immobilization_or_surgery: bool, prior_dvt_pe: bool, hemoptysis: bool, malignancy: bool,
) -> float:
    score = 0.0
    score += 3.0 if dvt_signs else 0
    score += 3.0 if pe_most_likely else 0
    score += 1.5 if hr_over_100 else 0
    score += 1.5 if immobilization_or_surgery else 0
    score += 1.5 if prior_dvt_pe else 0
    score += 1.0 if hemoptysis else 0
    score += 1.0 if malignancy else 0
    return score


def wells_pe_interpretation(score: float) -> str:
    """
    2-tier: <=4 unlikely, >4 likely.
    3-tier: <2 low, 2-6 moderate, >6 high.
    (Wells JA et al., Ann Intern Med 2001/2001 -- original PE criteria.)
    """
    if score < 2:
        tier3 = "Low probability (3-tier)"
    elif score <= 6:
        tier3 = "Moderate probability (3-tier)"
    else:
        tier3 = "High probability (3-tier)"
    tier2 = "PE unlikely (2-tier)" if score <= 4 else "PE likely (2-tier)"
    return f"{tier2}; {tier3}"


def wells_dvt_score(
    active_cancer: bool, paralysis_or_immobilization: bool, bedridden_or_surgery: bool,
    tenderness_deep_veins: bool, entire_leg_swollen: bool, calf_swelling_3cm: bool,
    pitting_edema: bool, collateral_veins: bool, prior_dvt: bool, alternative_diagnosis_as_likely: bool,
) -> int:
    score = 0
    for flag in (
        active_cancer, paralysis_or_immobilization, bedridden_or_surgery, tenderness_deep_veins,
        entire_leg_swollen, calf_swelling_3cm, pitting_edema, collateral_veins, prior_dvt,
    ):
        score += 1 if flag else 0
    if alternative_diagnosis_as_likely:
        score -= 2
    return score


def wells_dvt_interpretation(score: int) -> str:
    """
    2-tier: <2 (i.e. <=1) unlikely, >=2 likely.
    3-tier: <=0 low, 1-2 moderate, >=3 high.
    (Wells PS et al., Lancet 1997/N Engl J Med 2003 -- original DVT criteria.)
    """
    if score <= 0:
        tier3 = "Low probability (3-tier)"
    elif score <= 2:
        tier3 = "Moderate probability (3-tier)"
    else:
        tier3 = "High probability (3-tier)"
    tier2 = "DVT unlikely (2-tier)" if score < 2 else "DVT likely (2-tier)"
    return f"{tier2}; {tier3}"

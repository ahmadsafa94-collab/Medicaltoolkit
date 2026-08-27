"""
Standard renal-function calculators: CKD-EPI 2021 (race-free) eGFR, and
Cockcroft-Gault creatinine clearance (CrCl).

These are exact, published, guideline-standard formulas -- there is no
ambiguity or interpretation involved, unlike matching a calculated value to
a specific drug's free-text renal-dosing table. That matching step is
deliberately NOT automated anywhere in this bot: drug_lookup.py already
avoids re-parsing FDA table text into structured cells (a misread column
could silently transpose a dose), and the same caution applies here. This
module only computes the lab value itself; renal_flow.py shows that value
next to the drug's own renal-dosing text/table and leaves matching them up
to the user.

eGFR (CKD-EPI) and CrCl (Cockcroft-Gault) are NOT the same measurement and
are not interchangeable -- some FDA labels key their dosing thresholds to
one, some to the other. Callers should always show which one was
calculated, clearly labeled with its unit.

References:
  - CKD-EPI 2021: Inker LA et al., "New Creatinine- and Cystatin C-Based
    Equations to Estimate GFR without Race", N Engl J Med 2021.
  - Cockcroft-Gault: Cockcroft DW, Gault MH, Nephron 1976.
  - KDIGO 2012 CKD staging (G1-G5).
"""


class RenalInputError(ValueError):
    """Raised when an input is missing, non-numeric, or physiologically implausible."""


MGDL_PER_UMOLL = 1 / 88.4  # serum creatinine: 1 mg/dL = 88.4 umol/L


def umol_to_mgdl(value_umol_l: float) -> float:
    """Convert serum creatinine from umol/L (common outside the US) to mg/dL (what both formulas expect)."""
    return value_umol_l * MGDL_PER_UMOLL


def _validate_number(value, name: str, low: float, high: float) -> float:
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RenalInputError(f"{name} is required.")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise RenalInputError(f"{name} must be a number.")
    if value != value or value in (float("inf"), float("-inf")):  # NaN/inf guard
        raise RenalInputError(f"{name} must be a real number.")
    if not (low <= value <= high):
        raise RenalInputError(
            f"{name} of {value:g} is outside the plausible range ({low:g}-{high:g}) -- please double-check it."
        )
    return value


def validate_age(age) -> float:
    return _validate_number(age, "Age", 0, 120)


def validate_weight_kg(weight) -> float:
    return _validate_number(weight, "Weight", 1, 400)


def validate_creatinine_mgdl(creatinine) -> float:
    return _validate_number(creatinine, "Serum creatinine", 0.1, 25)


def validate_direct_value(value, label: str) -> float:
    return _validate_number(value, label, 0, 200)


def normalize_sex(raw: str) -> str:
    """Normalize a sex input to 'F' or 'M'. Raises RenalInputError otherwise."""
    s = (raw or "").strip().upper()
    if s in ("F", "FEMALE"):
        return "F"
    if s in ("M", "MALE"):
        return "M"
    raise RenalInputError("Sex must be male or female (required by the CKD-EPI/Cockcroft-Gault formulas).")


def calculate_egfr_ckdepi_2021(age: float, sex: str, creatinine_mg_dl: float) -> float:
    """
    CKD-EPI 2021 race-free creatinine equation for eGFR (mL/min/1.73m^2).
    sex: "F" or "M" (see normalize_sex).
    """
    sex = normalize_sex(sex)
    kappa = 0.7 if sex == "F" else 0.9
    alpha = -0.241 if sex == "F" else -0.302

    scr_over_kappa = creatinine_mg_dl / kappa
    min_term = min(scr_over_kappa, 1.0) ** alpha
    max_term = max(scr_over_kappa, 1.0) ** -1.200

    egfr = 142 * min_term * max_term * (0.9938 ** age)
    if sex == "F":
        egfr *= 1.012
    return egfr


def calculate_crcl_cockcroft_gault(age: float, sex: str, weight_kg: float, creatinine_mg_dl: float) -> float:
    """
    Cockcroft-Gault creatinine clearance (mL/min), using actual body weight.
    sex: "F" or "M" (see normalize_sex).

    Note: some clinicians prefer ideal or adjusted body weight for patients
    who are significantly obese or underweight. This implementation uses
    actual (total) body weight, the same default most basic CrCl
    calculators use -- callers should mention this simplification.
    """
    sex = normalize_sex(sex)
    crcl = ((140 - age) * weight_kg) / (72 * creatinine_mg_dl)
    if sex == "F":
        crcl *= 0.85
    return crcl


def ckd_egfr_stage(egfr: float) -> str:
    """KDIGO CKD G-stage label for an eGFR value (mL/min/1.73m^2)."""
    if egfr >= 90:
        return "G1 (normal or high)"
    if egfr >= 60:
        return "G2 (mildly decreased)"
    if egfr >= 45:
        return "G3a (mildly to moderately decreased)"
    if egfr >= 30:
        return "G3b (moderately to severely decreased)"
    if egfr >= 15:
        return "G4 (severely decreased)"
    return "G5 (kidney failure)"


def crcl_category(crcl: float) -> str:
    """
    Rough descriptive category for a Cockcroft-Gault CrCl value, using the
    same numeric cutoffs commonly seen in FDA renal-dosing tables. This is
    NOT the formal KDIGO G-stage (that's defined for eGFR) -- it's just a
    plain-language label so the number means something at a glance.
    """
    if crcl >= 90:
        return "normal"
    if crcl >= 60:
        return "mildly decreased"
    if crcl >= 30:
        return "moderately decreased"
    if crcl >= 15:
        return "severely decreased"
    return "kidney failure range"

"""
Static medical abbreviation / lab reference-range glossary.

Deliberately a hand-curated, static dict rather than anything AI-generated --
these are exactly the kind of short, easily-misremembered facts where a
generated answer could be subtly wrong with no easy way for a student to
notice. Lab reference ranges in particular vary by assay/lab/units, so every
numeric range here is explicitly labeled as a typical adult range to check
against the reporting lab's own reference interval, not a universal constant.

Entries are grouped as (term, category, definition); category is just for
display (📖 abbreviation vs 🧪 lab value) -- lookup itself is case-insensitive
and ignores the category.
"""

# (term, category, definition)
_ENTRIES: list[tuple[str, str, str]] = [
    # --- Common clinical abbreviations ---
    ("BID", "abbr", "Twice daily (bis in die)."),
    ("TID", "abbr", "Three times daily (ter in die)."),
    ("QID", "abbr", "Four times daily (quater in die)."),
    ("QD", "abbr", "Once daily (largely deprecated -- easily confused with QID; 'daily' is preferred in prescribing)."),
    ("QHS", "abbr", "At bedtime (quaque hora somni)."),
    ("PRN", "abbr", "As needed (pro re nata)."),
    ("PO", "abbr", "By mouth (per os)."),
    ("IV", "abbr", "Intravenous."),
    ("IM", "abbr", "Intramuscular."),
    ("SC / SQ", "abbr", "Subcutaneous."),
    ("NPO", "abbr", "Nothing by mouth (nil per os)."),
    ("STAT", "abbr", "Immediately."),
    ("AC", "abbr", "Before meals (ante cibum)."),
    ("PC", "abbr", "After meals (post cibum)."),
    ("BM", "abbr", "Bowel movement (context-dependent -- also 'blood/breast milk' elsewhere)."),
    ("DVT", "abbr", "Deep vein thrombosis."),
    ("PE", "abbr", "Pulmonary embolism (also: physical exam, depending on context)."),
    ("MI", "abbr", "Myocardial infarction."),
    ("CHF", "abbr", "Congestive heart failure."),
    ("COPD", "abbr", "Chronic obstructive pulmonary disease."),
    ("CKD", "abbr", "Chronic kidney disease."),
    ("AKI", "abbr", "Acute kidney injury."),
    ("ESRD", "abbr", "End-stage renal disease."),
    ("UTI", "abbr", "Urinary tract infection."),
    ("URI", "abbr", "Upper respiratory infection."),
    ("DM", "abbr", "Diabetes mellitus."),
    ("HTN", "abbr", "Hypertension."),
    ("HLD", "abbr", "Hyperlipidemia."),
    ("CAD", "abbr", "Coronary artery disease."),
    ("CVA", "abbr", "Cerebrovascular accident (stroke)."),
    ("TIA", "abbr", "Transient ischemic attack."),
    ("GERD", "abbr", "Gastroesophageal reflux disease."),
    ("IBD", "abbr", "Inflammatory bowel disease."),
    ("IBS", "abbr", "Irritable bowel syndrome."),
    ("RA", "abbr", "Rheumatoid arthritis."),
    ("SLE", "abbr", "Systemic lupus erythematosus."),
    ("OSA", "abbr", "Obstructive sleep apnea."),
    ("ARDS", "abbr", "Acute respiratory distress syndrome."),
    ("DKA", "abbr", "Diabetic ketoacidosis."),
    ("HHS", "abbr", "Hyperosmolar hyperglycemic state."),
    ("SIRS", "abbr", "Systemic inflammatory response syndrome."),
    ("ICU", "abbr", "Intensive care unit."),
    ("ED / ER", "abbr", "Emergency department / emergency room."),
    ("LOC", "abbr", "Level/loss of consciousness (context-dependent)."),
    ("SOB", "abbr", "Shortness of breath."),
    ("N/V", "abbr", "Nausea/vomiting."),
    ("N/V/D", "abbr", "Nausea/vomiting/diarrhea."),
    ("HA", "abbr", "Headache."),
    ("CP", "abbr", "Chest pain."),
    ("Abd", "abbr", "Abdomen/abdominal."),
    ("Bx", "abbr", "Biopsy."),
    ("Dx", "abbr", "Diagnosis."),
    ("Hx", "abbr", "History."),
    ("Tx", "abbr", "Treatment."),
    ("Sx", "abbr", "Symptoms (also: surgery, context-dependent)."),
    ("Fx", "abbr", "Fracture."),
    ("Rx", "abbr", "Prescription/treatment."),
    ("W/U", "abbr", "Workup."),
    ("F/U", "abbr", "Follow-up."),
    ("BMI", "abbr", "Body mass index (weight in kg / height in m^2). See /calculators."),
    ("BSA", "abbr", "Body surface area. See /calculators."),
    ("eGFR", "abbr", "Estimated glomerular filtration rate -- an estimate of kidney function. See /calculators or 'Calculate dose by renal function' under /dose."),
    ("CrCl", "abbr", "Creatinine clearance -- a different renal-function estimate from eGFR, requires weight. See /calculators."),
    ("INR", "abbr", "International normalized ratio -- standardizes prothrombin time across labs, used to monitor warfarin."),
    ("PT/PTT", "abbr", "Prothrombin time / partial thromboplastin time -- coagulation studies."),
    ("CBC", "abbr", "Complete blood count."),
    ("BMP", "abbr", "Basic metabolic panel (Na, K, Cl, CO2, BUN, creatinine, glucose, +/- calcium)."),
    ("CMP", "abbr", "Comprehensive metabolic panel (BMP plus liver enzymes, total protein, albumin, bilirubin)."),
    ("LFTs", "abbr", "Liver function tests."),
    ("TSH", "abbr", "Thyroid-stimulating hormone."),
    ("HbA1c", "abbr", "Glycated hemoglobin -- reflects average blood glucose over ~2-3 months."),
    ("ABG", "abbr", "Arterial blood gas."),
    ("UA", "abbr", "Urinalysis."),
    ("CXR", "abbr", "Chest X-ray."),
    ("CT", "abbr", "Computed tomography."),
    ("MRI", "abbr", "Magnetic resonance imaging."),
    ("ECG / EKG", "abbr", "Electrocardiogram."),
    ("ECHO", "abbr", "Echocardiogram."),
    ("BP", "abbr", "Blood pressure."),
    ("HR", "abbr", "Heart rate."),
    ("RR", "abbr", "Respiratory rate."),
    ("O2 sat / SpO2", "abbr", "Oxygen saturation."),
    ("T", "abbr", "Temperature (in vitals contexts)."),
    ("BSA (chemo dosing)", "abbr", "See BSA above -- also the standard basis for chemotherapy dosing."),
    ("QTc", "abbr", "Corrected QT interval (heart-rate-corrected). See /calculators."),
    ("MELD", "abbr", "Model for End-Stage Liver Disease -- transplant-allocation score. See /calculators."),
    ("CHA2DS2-VASc", "abbr", "Stroke-risk score in atrial fibrillation. See /calculators."),

    # --- Common adult lab reference ranges (typical ranges -- always verify
    # against the reporting lab's own interval, since assay/units/population
    # all shift the exact cutoffs) ---
    ("Sodium (Na)", "lab", "~135-145 mEq/L (typical adult range; verify against your lab)."),
    ("Potassium (K)", "lab", "~3.5-5.0 mEq/L (typical adult range; verify against your lab)."),
    ("Chloride (Cl)", "lab", "~96-106 mEq/L (typical adult range; verify against your lab)."),
    ("Bicarbonate (HCO3)", "lab", "~22-28 mEq/L (typical adult range; verify against your lab)."),
    ("BUN", "lab", "~7-20 mg/dL (typical adult range; verify against your lab)."),
    ("Creatinine", "lab", "~0.6-1.3 mg/dL (typical adult range; varies notably by sex/muscle mass -- verify against your lab)."),
    ("Glucose (fasting)", "lab", "~70-99 mg/dL fasting (typical adult range; verify against your lab)."),
    ("Calcium (total)", "lab", "~8.5-10.5 mg/dL (typical adult range; verify against your lab -- correct for albumin if low, see /calculators)."),
    ("Albumin", "lab", "~3.5-5.0 g/dL (typical adult range; verify against your lab)."),
    ("Total bilirubin", "lab", "~0.1-1.2 mg/dL (typical adult range; verify against your lab)."),
    ("ALT", "lab", "~7-56 U/L (typical adult range; verify against your lab)."),
    ("AST", "lab", "~10-40 U/L (typical adult range; verify against your lab)."),
    ("Hemoglobin (Hgb)", "lab", "~13.5-17.5 g/dL (male), ~12.0-15.5 g/dL (female) (typical adult ranges; verify against your lab)."),
    ("Hematocrit (Hct)", "lab", "~38.8-50.0% (male), ~34.9-44.5% (female) (typical adult ranges; verify against your lab)."),
    ("WBC", "lab", "~4,500-11,000 /uL (typical adult range; verify against your lab)."),
    ("Platelets", "lab", "~150,000-450,000 /uL (typical adult range; verify against your lab)."),
    ("INR (no anticoagulation)", "lab", "~0.8-1.1 (typical; therapeutic target on warfarin is usually ~2.0-3.0, indication-dependent)."),
    ("TSH", "lab", "~0.4-4.0 mIU/L (typical adult range; verify against your lab -- assay-dependent)."),
    ("HbA1c", "lab", "<5.7% normal, 5.7-6.4% prediabetes, >=6.5% diabetes (ADA criteria; verify current guideline)."),
    ("Lactate", "lab", "~0.5-2.2 mmol/L (typical adult range; verify against your lab)."),
]

_INDEX = {term.upper(): (term, category, definition) for term, category, definition in _ENTRIES}


class GlossaryNotFoundError(Exception):
    pass


def lookup_term(term: str) -> tuple[str, str, str]:
    """Exact (case-insensitive) lookup. Returns (term, category, definition). Raises GlossaryNotFoundError."""
    key = term.strip().upper()
    if key in _INDEX:
        return _INDEX[key]
    # allow matching "eGFR" style terms even if punctuation/spacing differs slightly
    for stored_key, entry in _INDEX.items():
        if stored_key.replace(" ", "").replace("/", "") == key.replace(" ", "").replace("/", ""):
            return entry
    raise GlossaryNotFoundError(
        f"'{term}' isn't in the glossary yet. Try /glossary with no term to browse, "
        "or check the spelling/abbreviation."
    )


def search_glossary(prefix: str, limit: int = 10) -> list[str]:
    """Prefix search over glossary terms (case-insensitive), for inline-style autocomplete."""
    prefix = prefix.strip().upper()
    if not prefix:
        return []
    matches = [term for term, _cat, _def in _ENTRIES if term.upper().startswith(prefix)]
    if len(matches) < limit:
        # also allow matches anywhere in the term for short abbreviations buried mid-string
        for term, _cat, _def in _ENTRIES:
            if prefix in term.upper() and term not in matches:
                matches.append(term)
                if len(matches) >= limit:
                    break
    return matches[:limit]


def all_terms() -> list[str]:
    return [term for term, _cat, _def in _ENTRIES]


def format_entry(term: str, category: str, definition: str) -> str:
    emoji = "🧪" if category == "lab" else "📖"
    return f"{emoji} *{term}*\n{definition}"

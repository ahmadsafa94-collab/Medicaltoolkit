"""
Deterministic interaction facts the checker hands to the AI as findings,
from two public references:

1. The FDA's table of drugs that interact with CYP enzymes and transporters
   (interaction_data/fda_cyp_table.json, built by build_fda_cyp_table.py).
   Unlike a drug's class ("CYP3A4 inhibitor"), it says how STRONG an
   inhibitor or inducer is and how SENSITIVE a substrate is -- exactly what
   label warnings are written in terms of ("avoid strong CYP3A
   inhibitors"). Every inhibitor/inducer + substrate pairing on the list is
   reported as a match.

2. The expert-panel list of 15 high-priority drug-drug interactions, the
   ones considered contraindicated for concurrent use in electronic health
   records (Phansalkar et al., JAMIA 2012;19:735; the 15 class pairs as
   listed in Cornu, Phansalkar et al., Int J Med Inform 2018). Classes are
   spelled out as drug lists here, or resolved through the FDA table for
   the CYP-defined ones ("strong CYP3A4 inhibitors").

Nothing here is AI: same drugs in, same findings out. Matching works on
lowercase ingredient names, taken from the label's generic name (salts
stripped, combination products split) and its display name.
"""

import json
import logging
import os
import re

logger = logging.getLogger(__name__)

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "interaction_data", "fda_cyp_table.json")
try:
    with open(_DATA, encoding="utf-8") as f:
        FDA_TABLE = json.load(f)
except Exception:
    logger.exception("Couldn't load the FDA CYP/transporter table")
    FDA_TABLE = {"drugs": {}, "footnotes": {}}

FDA_AS_OF = FDA_TABLE.get("as_of") or "unknown date"
_DRUGS = FDA_TABLE.get("drugs", {})
_FOOTNOTES = FDA_TABLE.get("footnotes", {})

# Common spelling differences between label names and the FDA table.
_ALIASES = {"rifampicin": "rifampin", "acetaminophen": "paracetamol"}

_SALT_RE = re.compile(
    r"\b(hydrochloride|hcl|hydrobromide|sodium|potassium|calcium|magnesium|sulfate|maleate|mesylate|"
    r"besylate|tartrate|bitartrate|citrate|succinate|fumarate|acetate|phosphate|bromide|hyclate|"
    r"monohydrate|dihydrate|trihydrate|hemihydrate|anhydrous|extended-release|delayed-release|"
    r"er|xr|sr|cr|dr|la|xl|odt)\b",
    re.I,
)
_SPLIT_RE = re.compile(r"\s*(?:,|/|\band\b|\bwith\b|\+|;)\s*", re.I)
_LEVEL_RANK = {"strong": 3, "moderate": 2, "weak": 1, "sensitive": 2, "moderate sensitive": 1}


def ingredients(drug: dict) -> list[str]:
    """Lowercase single-ingredient names for a checker drug ({'name', 'sections'})."""
    sections = drug.get("sections") or {}
    names = []
    for raw in (sections.get("_generic"), drug.get("name"), sections.get("_name")):
        if not raw:
            continue
        for part in _SPLIT_RE.split(raw.lower()):
            part = re.sub(r"\s+", " ", _SALT_RE.sub(" ", part)).strip(" .-")
            if len(part) > 2:
                names.append(_ALIASES.get(part, part))
    return list(dict.fromkeys(names))


def _fda_entries(drug: dict) -> list[tuple[str, dict]]:
    """FDA-table rows for this drug: the whole combination first (e.g. 'lopinavir and ritonavir'), then each ingredient."""
    sections = drug.get("sections") or {}
    out, seen = [], set()
    generic = re.sub(r"\s+", " ", _SALT_RE.sub(" ", (sections.get("_generic") or "").lower())).strip()
    for name in [generic] + ingredients(drug):
        if name and name in _DRUGS and name not in seen:
            seen.add(name)
            out.append((name, _DRUGS[name]))
    return out


def fda_profile(drug: dict) -> list[str]:
    """Readable lines for the prompt, e.g. 'clarithromycin: strong CYP3A inhibitor'."""
    lines = []
    for name, e in _fda_entries(drug):
        parts = []
        for enzyme, level in (e.get("inhibits") or {}).items():
            parts.append(f"{level} CYP{enzyme} inhibitor")
        for enzyme, level in (e.get("induces") or {}).items():
            parts.append(f"{level} CYP{enzyme} inducer")
        for enzyme, level in (e.get("substrate_of") or {}).items():
            parts.append(f"{level} CYP{enzyme} substrate")
        if e.get("transporter_inhibits"):
            parts.append("inhibitor of " + ", ".join(e["transporter_inhibits"]))
        if e.get("transporter_substrate"):
            parts.append("substrate of " + ", ".join(e["transporter_substrate"]))
        if not parts:
            continue
        lines.append(f"{name}: " + "; ".join(parts))
        for n in e.get("notes") or []:
            if n in _FOOTNOTES:
                lines.append(f"  (FDA note: {_FOOTNOTES[n]})")
    return lines


def _roles(drug: dict) -> dict:
    """Merged FDA roles across a drug's rows: {'inhibits': {enzyme: level}, ...}."""
    merged = {"inhibits": {}, "induces": {}, "substrate_of": {}, "transporter_inhibits": set(), "transporter_substrate": set()}
    for _, e in _fda_entries(drug):
        for kind in ("inhibits", "induces", "substrate_of"):
            for enzyme, level in (e.get(kind) or {}).items():
                if _LEVEL_RANK.get(level, 0) > _LEVEL_RANK.get(merged[kind].get(enzyme), 0):
                    merged[kind][enzyme] = level
        for kind in ("transporter_inhibits", "transporter_substrate"):
            merged[kind].update(e.get(kind) or [])
    return merged


def _norm_transporter(t: str) -> str:
    return "OATP1B" if t.startswith("OATP1B") else ("MATE" if t.startswith("MATE") else t)


def fda_matches(a: dict, b: dict) -> list[str]:
    """Inhibitor/inducer of one drug acting on an enzyme or transporter the other is a substrate of, both ways."""
    found = []
    for perp, victim in ((a, b), (b, a)):
        rp, rv = _roles(perp), _roles(victim)
        for enzyme, sub_level in rv["substrate_of"].items():
            if enzyme in rp["inhibits"]:
                found.append(
                    f"{perp['name']} is a {rp['inhibits'][enzyme]} CYP{enzyme} inhibitor and {victim['name']} is a "
                    f"{sub_level} CYP{enzyme} substrate -> {victim['name']} exposure is expected to RISE"
                )
            if enzyme in rp["induces"]:
                found.append(
                    f"{perp['name']} is a {rp['induces'][enzyme]} CYP{enzyme} inducer and {victim['name']} is a "
                    f"{sub_level} CYP{enzyme} substrate -> {victim['name']} exposure is expected to FALL"
                )
        inhibited = {_norm_transporter(t) for t in rp["transporter_inhibits"]}
        shared = sorted({_norm_transporter(t) for t in rv["transporter_substrate"]} & inhibited)
        if shared:
            found.append(
                f"{perp['name']} inhibits {', '.join(shared)} and {victim['name']} is a substrate of it "
                f"-> {victim['name']} exposure may RISE"
            )
    return found


# ---------------------------------------------------------------------
# The 15 high-priority interactions
# ---------------------------------------------------------------------

_MAOI = {"phenelzine", "tranylcypromine", "isocarboxazid", "selegiline", "rasagiline", "safinamide",
         "linezolid", "methylene blue", "procarbazine"}
_AMPHETAMINES = {"amphetamine", "dextroamphetamine", "levoamphetamine", "lisdexamfetamine", "methamphetamine",
                 "benzphetamine", "phentermine", "phendimetrazine", "diethylpropion"}
_SSRI = {"fluoxetine", "sertraline", "paroxetine", "citalopram", "escitalopram", "fluvoxamine", "vilazodone"}
_OPIOIDS = {"meperidine", "pethidine", "tramadol", "tapentadol", "methadone", "fentanyl", "morphine", "hydromorphone",
            "oxycodone", "oxymorphone", "hydrocodone", "codeine", "levorphanol", "buprenorphine"}
_TCA = {"amitriptyline", "nortriptyline", "imipramine", "desipramine", "clomipramine", "doxepin", "trimipramine",
        "protriptyline", "amoxapine"}
_PPI = {"omeprazole", "esomeprazole", "lansoprazole", "dexlansoprazole", "pantoprazole", "rabeprazole"}
_PROTEASE_INHIBITORS = {"atazanavir", "darunavir", "fosamprenavir", "indinavir", "lopinavir", "nelfinavir",
                        "ritonavir", "saquinavir", "tipranavir", "nirmatrelvir"}
_ERGOTS = {"ergotamine", "dihydroergotamine", "methylergonovine", "methylergometrine", "ergonovine"}
_TRIPTANS = {"sumatriptan", "rizatriptan", "zolmitriptan", "almotriptan", "naratriptan", "frovatriptan", "eletriptan"}


def _named(names: set):
    return lambda d: any(i in names for i in ingredients(d))


def _fda_role(kind: str, enzyme: str, levels: tuple):
    return lambda d: _roles(d)[kind].get(enzyme) in levels


def _qt_in_label(drug: dict) -> bool:
    """The label itself warns about QT prolongation / torsades (contraindications, warnings or boxed warning)."""
    s = drug.get("sections") or {}
    text = " ".join(
        " ".join((s.get(label) or {}).get("bullets") or []) for label in ("Contraindications", "Warnings & Cautions")
    ) + " " + " ".join((s.get("_interaction_extra") or {}).values())
    return bool(re.search(r"(?i)QT[c]?\s*(interval\s*)?prolong|prolong\w*\s+(of\s+)?(the\s+)?QT|torsade", text))


# (object class, precipitant class, object test, precipitant test, at least Major?)
HIGH_PRIORITY = [
    ("Amphetamines", "MAO inhibitors", _named(_AMPHETAMINES), _named(_MAOI), True),
    ("Atazanavir", "Proton pump inhibitors", _named({"atazanavir"}), _named(_PPI), True),
    ("Febuxostat", "Azathioprine / mercaptopurine", _named({"febuxostat"}), _named({"azathioprine", "mercaptopurine"}), True),
    ("SSRIs", "MAO inhibitors", _named(_SSRI), _named(_MAOI), True),
    ("Irinotecan", "Strong CYP3A4 inhibitors", _named({"irinotecan"}), _fda_role("inhibits", "3A", ("strong",)), True),
    ("Narcotic analgesics", "MAO inhibitors", _named(_OPIOIDS), _named(_MAOI), True),
    ("Tricyclic antidepressants", "MAO inhibitors", _named(_TCA), _named(_MAOI), True),
    # Not forced to Major: "QT-prolonging" here means the label warns of it,
    # which covers drugs of very different risk; the labels' wording decides.
    ("QT-prolonging drugs", "QT-prolonging drugs", _qt_in_label, _qt_in_label, False),
    ("Ramelteon", "CYP1A2 inhibitors", _named({"ramelteon"}), _fda_role("inhibits", "1A2", ("strong", "moderate")), True),
    ("Protease inhibitors", "Strong CYP3A4 inducers", _named(_PROTEASE_INHIBITORS), _fda_role("induces", "3A", ("strong",)), True),
    ("Simvastatin / lovastatin", "CYP3A4 inhibitors", _named({"simvastatin", "lovastatin"}),
     _fda_role("inhibits", "3A", ("strong", "moderate")), True),
    ("Atorvastatin", "Strong CYP3A4 inhibitors", _named({"atorvastatin"}), _fda_role("inhibits", "3A", ("strong",)), True),
    ("Ergot alkaloids", "Strong CYP3A4 inhibitors", _named(_ERGOTS), _fda_role("inhibits", "3A", ("strong",)), True),
    ("Tizanidine", "CYP1A2 inhibitors", _named({"tizanidine"}), _fda_role("inhibits", "1A2", ("strong", "moderate")), True),
    ("Tranylcypromine", "Procarbazine", _named({"tranylcypromine"}), _named({"procarbazine"}), True),
    ("Triptans", "MAO inhibitors", _named(_TRIPTANS), _named(_MAOI), True),
]


def high_priority_matches(a: dict, b: dict) -> list[tuple[str, bool]]:
    """[(description, at_least_major)] for every high-priority entry this pair falls under, either way round."""
    found = []
    for obj_name, prec_name, is_obj, is_prec, major in HIGH_PRIORITY:
        for x, y in ((a, b), (b, a)):
            try:
                hit = is_obj(x) and is_prec(y)
            except Exception:
                logger.exception("High-priority rule %s + %s failed", obj_name, prec_name)
                hit = False
            if hit:
                found.append((f"{x['name']} ({obj_name}) + {y['name']} ({prec_name})", major))
                break
    return found


def findings(drugs: list[dict]) -> str:
    """The pre-computed findings block for the prompt ('' when there are none)."""
    lines = []
    for i, a in enumerate(drugs):
        for b in drugs[i + 1:]:
            for text, major in high_priority_matches(a, b):
                if major:
                    lines.append(
                        f"- HIGH-PRIORITY LIST: {text}. On the expert-panel list of 15 interactions considered "
                        "contraindicated for concurrent use (Phansalkar et al., JAMIA 2012). Rate it at least 🟠 Major."
                    )
                else:
                    lines.append(
                        f"- HIGH-PRIORITY LIST: {text} -- both labels warn of QT prolongation, a combination on "
                        "the expert-panel high-priority list (Phansalkar et al., JAMIA 2012). Risk differs widely "
                        "between drugs, so grade it from the labels' wording."
                    )
            for text in fda_matches(a, b):
                lines.append(f"- FDA CYP/TRANSPORTER TABLE: {text}.")
    return "\n".join(dict.fromkeys(lines))

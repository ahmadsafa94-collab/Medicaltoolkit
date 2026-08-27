"""
Drug dosing/reference lookup.

Deliberately NOT using an LLM to generate dosing numbers -- that's the one
place in this bot where a hallucinated number could cause real harm. Instead
this pulls structured data straight from the FDA's official drug label
database (openFDA), which is free and requires no API key.

Docs: https://open.fda.gov/apis/drug/label/
"""

import re
import urllib.parse

import httpx

OPENFDA_LABEL_URL = "https://api.fda.gov/drug/label.json"

# Concept -> (display label, emoji, [possible openFDA field keys, in priority order])
# Labels don't all use the same schema (older vs PLR-format), so we try
# multiple keys per concept before giving up on that section.
FIELDS = [
    ("dosage_and_administration", "Dosage & Administration", "💉", ["dosage_and_administration"]),
    ("indications_and_usage", "Indications & Usage", "🎯", ["indications_and_usage"]),
    ("contraindications", "Contraindications", "🚫", ["contraindications"]),
    ("warnings_and_cautions", "Warnings & Cautions", "⚠️", ["warnings_and_cautions", "warnings", "boxed_warning"]),
    ("drug_interactions", "Drug Interactions", "🔀", ["drug_interactions"]),
    ("pediatric_use", "Pediatric Use", "🧒", ["pediatric_use"]),
    ("pregnancy", "Pregnancy", "🤰", ["pregnancy", "pregnancy_or_breast_feeding", "teratogenic_effects"]),
    ("nursing_mothers", "Nursing / Lactation", "🍼", ["nursing_mothers"]),
    ("geriatric_use", "Geriatric Use", "🧓", ["geriatric_use"]),
    # Catch-all: covers renal/hepatic impairment, pregnancy, pediatric, etc.
    # when a drug's label bundles them together instead of using separate keys.
    ("use_in_specific_populations", "Other Specific Populations", "👥", ["use_in_specific_populations"]),
]

DISCLAIMER = (
    "⚠️ Source: FDA label database (openFDA), reference only. "
    "Not a substitute for clinical judgment or a current formulary — "
    "verify before dosing a patient."
)

MAX_BULLETS_PER_FIELD = 5
MAX_CHARS_PER_BULLET = 200


class DrugNotFoundError(Exception):
    pass


def _split_into_bullets(text: str, max_bullets: int = MAX_BULLETS_PER_FIELD) -> list[str]:
    """Break a dense label paragraph into short, scannable bullet points."""
    text = re.sub(r"\s+", " ", text).strip()

    # Split on sentence boundaries, but don't break on common medical
    # abbreviations/decimals (e.g. "5 mg." or "q.i.d.")
    text = re.sub(r"(?<!\d)\.(?!\d)\s+(?=[A-Z(])", ".|", text)
    raw_parts = [p.strip() for p in text.split("|") if p.strip()]

    bullets = []
    for part in raw_parts:
        if len(part) > MAX_CHARS_PER_BULLET:
            part = part[:MAX_CHARS_PER_BULLET].rsplit(" ", 1)[0] + "..."
        bullets.append(part)
        if len(bullets) >= max_bullets:
            break
    return bullets


async def lookup_drug(drug_name: str) -> dict:
    """
    Query openFDA for a drug by generic or brand name.
    Returns a dict of {field_label: [bullet, bullet, ...]} for whichever
    fields are present, plus "_name" for the display name.
    Raises DrugNotFoundError if nothing matches.
    """
    drug_name = drug_name.strip().lower()
    query = (
        f'openfda.generic_name:"{drug_name}" '
        f'OR openfda.brand_name:"{drug_name}" '
        f'OR openfda.substance_name:"{drug_name}"'
    )
    params = {
        "search": query,
        "limit": 1,
    }
    url = f"{OPENFDA_LABEL_URL}?{urllib.parse.urlencode(params)}"

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.get(url)

    if response.status_code == 404:
        raise DrugNotFoundError(f"No FDA label found for '{drug_name}'.")
    response.raise_for_status()

    data = response.json()
    results = data.get("results", [])
    if not results:
        raise DrugNotFoundError(f"No FDA label found for '{drug_name}'.")

    record = results[0]
    openfda = record.get("openfda", {})

    display_name = (
        (openfda.get("brand_name") or [None])[0]
        or (openfda.get("generic_name") or [None])[0]
        or drug_name.title()
    )
    generic = (openfda.get("generic_name") or [None])[0]

    sections = {"_name": display_name, "_generic": generic}
    filled_concepts = set()

    for concept, field_label, emoji, keys in FIELDS:
        for key in keys:
            values = record.get(key)
            if values:
                bullets = _split_into_bullets(values[0])
                if bullets:
                    sections[field_label] = bullets
                    filled_concepts.add(concept)
                break  # stop trying alternate keys for this concept once found

    # Skip the catch-all "Other Specific Populations" section if we already
    # surfaced pregnancy/pediatric/geriatric individually -- avoids repeating
    # the same info twice.
    if {"pregnancy", "pediatric_use", "geriatric_use"} & filled_concepts:
        sections.pop("Other Specific Populations", None)

    if len(sections) <= 2:
        raise DrugNotFoundError(
            f"Found a label for '{display_name}' but it had no dosing/usage sections."
        )

    return sections


def format_drug_info(sections: dict) -> str:
    """Format a lookup_drug() result as a clean, bulleted Telegram message."""
    name = sections.get("_name", "Unknown drug")
    generic = sections.get("_generic")

    lines = [f"💊 *{name}*"]
    if generic and generic.lower() != name.lower():
        lines.append(f"_({generic})_")
    lines.append("")

    for _, field_label, emoji, _keys in FIELDS:
        bullets = sections.get(field_label)
        if not bullets:
            continue
        lines.append(f"{emoji} *{field_label}*")
        for bullet in bullets:
            lines.append(f"  • {bullet}")
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)


async def search_drug_names(prefix: str, limit: int = 8) -> list[str]:
    """
    Autocomplete helper: return a short list of drug names (generic + brand)
    starting with `prefix`, for use in Telegram inline-query suggestions.
    """
    prefix = prefix.strip().lower()
    if len(prefix) < 2:
        return []

    # openFDA wildcard prefix search (no quotes -- quotes force exact phrase match)
    query = f"openfda.generic_name:{prefix}* OR openfda.brand_name:{prefix}*"
    params = {
        "search": query,
        "limit": 25,  # over-fetch, then dedupe client-side
    }
    url = f"{OPENFDA_LABEL_URL}?{urllib.parse.urlencode(params)}"

    async with httpx.AsyncClient(timeout=8) as client:
        try:
            response = await client.get(url)
        except httpx.RequestError:
            return []

    if response.status_code != 200:
        return []

    data = response.json()
    names = []
    seen = set()
    for record in data.get("results", []):
        openfda = record.get("openfda", {})
        for candidate in (openfda.get("generic_name") or []) + (openfda.get("brand_name") or []):
            key = candidate.lower()
            if key.startswith(prefix) and key not in seen:
                seen.add(key)
                names.append(candidate.title())
            if len(names) >= limit:
                return names
    return names

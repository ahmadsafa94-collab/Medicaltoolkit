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

# Fields we care about, in display order, with a human-friendly label and emoji
FIELDS = [
    ("dosage_and_administration", "Dosage & Administration", "💉"),
    ("indications_and_usage", "Indications & Usage", "🎯"),
    ("contraindications", "Contraindications", "🚫"),
    ("warnings_and_cautions", "Warnings & Cautions", "⚠️"),
    ("drug_interactions", "Drug Interactions", "🔀"),
    ("pediatric_use", "Pediatric Use", "🧒"),
    ("pregnancy", "Pregnancy", "🤰"),
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
    for field_key, field_label, emoji in FIELDS:
        values = record.get(field_key)
        if values:
            bullets = _split_into_bullets(values[0])
            if bullets:
                sections[field_label] = bullets

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

    for _, field_label, emoji in FIELDS:
        bullets = sections.get(field_label)
        if not bullets:
            continue
        lines.append(f"{emoji} *{field_label}*")
        for bullet in bullets:
            lines.append(f"  • {bullet}")
        lines.append("")

    lines.append(DISCLAIMER)
    return "\n".join(lines)

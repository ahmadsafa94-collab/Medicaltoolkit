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

# Fields we care about, in display order, with a human-friendly label
FIELDS = [
    ("dosage_and_administration", "Dosage & Administration"),
    ("indications_and_usage", "Indications & Usage"),
    ("contraindications", "Contraindications"),
    ("warnings_and_cautions", "Warnings & Cautions"),
    ("drug_interactions", "Drug Interactions"),
    ("pediatric_use", "Pediatric Use"),
    ("pregnancy", "Pregnancy"),
]

DISCLAIMER = (
    "\n\n⚠️ Sourced from the FDA label database (openFDA), for reference "
    "only. Not a substitute for clinical judgment, current prescribing "
    "info, or institutional protocols. Always verify against an "
    "up-to-date formulary before dosing a patient."
)


class DrugNotFoundError(Exception):
    pass


def _clean(text: str, max_chars: int = 600) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "..."
    return text


async def lookup_drug(drug_name: str) -> dict:
    """
    Query openFDA for a drug by generic or brand name.
    Returns a dict of {field_label: text} for whichever fields are present.
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

    sections = {"_name": display_name}
    for field_key, field_label in FIELDS:
        values = record.get(field_key)
        if values:
            sections[field_label] = _clean(values[0])

    if len(sections) <= 1:
        raise DrugNotFoundError(
            f"Found a label for '{display_name}' but it had no dosing/usage sections."
        )

    return sections


def format_drug_info(sections: dict) -> str:
    """Format a lookup_drug() result as a Telegram-ready message."""
    name = sections.get("_name", "Unknown drug")
    lines = [f"💊 *{name}*\n"]
    for _, field_label in FIELDS:
        if field_label in sections:
            lines.append(f"*{field_label}:*\n{sections[field_label]}\n")
    lines.append(DISCLAIMER)
    return "\n".join(lines)

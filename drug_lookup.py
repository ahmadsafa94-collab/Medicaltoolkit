"""
Drug dosing/reference lookup.

Deliberately NOT using an LLM to generate dosing numbers -- that's the one
place in this bot where a hallucinated number could cause real harm. Instead
this pulls structured data straight from the FDA's official drug label
database (openFDA), which is free and requires no API key.

Docs: https://open.fda.gov/apis/drug/label/
"""

import asyncio
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

MAX_BULLETS_PER_FIELD = 12
MAX_CHARS_PER_BULLET = 600  # safety net only, for pathologically long single sentences


class DrugNotFoundError(Exception):
    pass


class DrugLookupRateLimitedError(Exception):
    pass


def _split_into_bullets(text: str, max_bullets: int = MAX_BULLETS_PER_FIELD) -> list[str]:
    """Break a dense label paragraph into short, scannable bullet points."""
    text = re.sub(r"\s+", " ", text).strip()

    # Split on sentence boundaries, but don't break on common medical
    # abbreviations/decimals (e.g. "5 mg." or "q.i.d.")
    text = re.sub(r"(?<!\d)\.(?!\d)\s+(?=[A-Z(])", ".|", text)
    raw_parts = [p.strip() for p in text.split("|") if p.strip()]

    bullets = []
    for part in raw_parts[:max_bullets]:
        # Only truncate a single sentence if it's absurdly long -- this is a
        # safety net, not a way to shorten normal-length sentences (that was
        # cutting real dosing info off mid-word, which is bad).
        if len(part) > MAX_CHARS_PER_BULLET:
            part = part[:MAX_CHARS_PER_BULLET].rsplit(" ", 1)[0] + " [...]"
        bullets.append(part)

    if len(raw_parts) > max_bullets:
        bullets.append(f"({len(raw_parts) - max_bullets} more sentence(s) in the full label — not shown here)")

    return bullets


async def lookup_drug(drug_name: str) -> dict:
    """
    Query openFDA for a drug by generic or brand name.
    Returns a dict of {field_label: [bullet, bullet, ...]} for whichever
    fields are present, plus "_name" for the display name.
    Raises DrugNotFoundError if nothing matches.
    """
    drug_name = drug_name.strip().lower()
    record = await _fetch_label_record(drug_name)

    if record is None:
        raise DrugNotFoundError(
            f"No FDA label found for '{drug_name}'. Try the plain generic "
            f"name (e.g. 'amoxicillin' rather than 'Amoxil 500mg')."
        )

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


async def _fetch_label_record(drug_name: str, retries: int = 1) -> dict | None:
    """
    Try a sequence of increasingly loose queries against openFDA until one
    returns a record. Returns None if nothing matched after all attempts.
    Handles timeouts and rate limits explicitly so a failed lookup always
    surfaces a clear result rather than hanging.
    """
    # Try exact quoted match across all three name fields first (fast, precise),
    # then fall back to substance_name only, then a loose unquoted match --
    # some antibiotics (e.g. salts like "cephalexin monohydrate") are stored
    # under substance_name but not generic_name, or vice versa.
    queries = [
        (
            f'openfda.generic_name:"{drug_name}" '
            f'OR openfda.brand_name:"{drug_name}" '
            f'OR openfda.substance_name:"{drug_name}"'
        ),
        f'openfda.substance_name:"{drug_name}"',
        f"openfda.generic_name:{drug_name} OR openfda.brand_name:{drug_name}",
    ]

    async with httpx.AsyncClient(timeout=10) as client:
        for query in queries:
            params = {"search": query, "limit": 1}
            url = f"{OPENFDA_LABEL_URL}?{urllib.parse.urlencode(params)}"

            for attempt in range(retries + 1):
                try:
                    response = await client.get(url)
                except (httpx.TimeoutException, httpx.ConnectError):
                    if attempt < retries:
                        await asyncio.sleep(1)
                        continue
                    return None  # give up on this query variant, try next

                if response.status_code == 404:
                    break  # no match for this query variant, try next
                if response.status_code == 429:
                    if attempt < retries:
                        await asyncio.sleep(2)
                        continue
                    raise DrugLookupRateLimitedError(
                        "openFDA rate limit reached. Wait a few seconds and try again."
                    )
                if response.status_code != 200:
                    break  # unexpected error, try next query variant

                results = response.json().get("results", [])
                if results:
                    return results[0]
                break  # 200 but empty results, try next query variant

    return None


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
    Autocomplete helper: return a short list of drug names starting with
    `prefix`, for use in Telegram inline-query suggestions.

    Uses a local curated list (drug_names.py) rather than a live openFDA
    wildcard query -- testing showed openFDA's openfda.* fields don't
    reliably filter on prefix wildcards (a query for "ser*" returned the
    entire unfiltered database). The actual /dose lookup below still hits
    openFDA live, so a drug missing from the local list can still be looked
    up directly by name -- it just won't appear in the typing suggestions.
    """
    from drug_names import search_common_drugs
    return search_common_drugs(prefix, limit=limit)

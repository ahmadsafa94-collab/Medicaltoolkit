"""
Drug dosing/reference lookup.

Deliberately NOT using an LLM to generate dosing numbers -- that's the one
place in this bot where a hallucinated number could cause real harm. Instead
this pulls structured data straight from the FDA's official drug label
database (openFDA), which is free and requires no API key.

Docs: https://open.fda.gov/apis/drug/label/
"""

import asyncio
import logging
import re
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

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


# openFDA's `search` parameter uses Lucene query syntax. drug_name comes
# straight from user input (a Telegram message), so without escaping,
# characters like `"` or `\` could break out of the quoted phrase we build
# below, and Lucene's other special characters (+-&&||!(){}[]^~*?:) could
# turn a plain drug-name lookup into an unintended boolean/wildcard query.
# Worst case without this is just a malformed query that fails to match
# (handled gracefully already), but escaping is the correct, defensive fix
# rather than relying on that fallback behavior.
_LUCENE_SPECIAL_CHARS = r'+-&|!(){}[]^"~*?:\\'


def _escape_lucene(term: str) -> str:
    return "".join(f"\\{ch}" if ch in _LUCENE_SPECIAL_CHARS else ch for ch in term)


_TABLE_HEADING_RE = re.compile(r"(?im)^[ \t]*table\s+\d+\b.*$")
_MULTISPACE_RE = re.compile(r" {3,}")
_MAX_TABLE_HEADING_LINES = 30  # cap runaway capture if no blank line ever ends it
_MIN_COLUMNAR_RUN = 3  # require 3+ consecutive aligned-looking lines, not 1-2 (avoids false positives on normal prose)

# Bullets that start with a short "lead" like "Adults:" or "Renal impairment:"
# get that lead bolded, so scanning a long section for the population/context
# that applies to you is faster than reading every bullet start-to-finish.
_LEAD_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 /()\-]{1,40}:)\s*(.*)$")


def _is_columnar_line(line: str) -> bool:
    """
    Loose proxy for 'this line looks like it has whitespace-aligned
    columns'. A single run of 3+ spaces is enough -- the common case is a
    2-column table (e.g. "dose range  ->  recommended dose"), which only
    produces one gap per row. Requiring this to hold for _MIN_COLUMNAR_RUN
    consecutive lines (checked by the caller) is what keeps this from
    false-positiving on an incidental extra space in ordinary prose.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 200:
        return False
    return bool(_MULTISPACE_RE.search(line))


def _extract_structured_blocks(raw_text: str) -> tuple[str, list[str]]:
    """
    Pull table-like regions out of raw (NOT yet whitespace-collapsed) label
    text, so they can be rendered as an image later instead of being
    flattened into an unreadable run-on paragraph by the bullet splitter.

    Two independent signals trigger capture:
      1. An explicit "Table N" caption -- FDA labels commonly caption real
         tables this way, and it's a near-zero-false-positive signal.
      2. A run of 3+ consecutive lines that each look column-aligned
         (2+ runs of 2+ spaces) -- a loose proxy for "this was a table
         before HTML-to-text flattening".

    Deliberately does NOT try to figure out where the actual column
    boundaries are within a line -- guessing wrong on dosing numbers would
    be worse than not detecting the table at all. The extracted block is
    returned completely verbatim so it can later be rendered in a
    monospace font, which reproduces the original space-alignment on its
    own without us needing to parse it into cells.

    Returns (remaining_prose_text, [table_block, ...]).
    """
    lines = raw_text.replace("\r\n", "\n").split("\n")
    n = len(lines)
    blocks = []
    remaining = []
    i = 0

    while i < n:
        line = lines[i]

        if _TABLE_HEADING_RE.match(line):
            j = i + 1
            captured = [line]
            while (
                j < n
                and lines[j].strip()
                and not _TABLE_HEADING_RE.match(lines[j])
                and (j - i) < _MAX_TABLE_HEADING_LINES
            ):
                captured.append(lines[j])
                j += 1
            blocks.append("\n".join(captured).strip())
            i = j
            continue

        if _is_columnar_line(line):
            j = i
            captured = []
            while j < n and _is_columnar_line(lines[j]):
                captured.append(lines[j])
                j += 1
            if len(captured) >= _MIN_COLUMNAR_RUN:
                blocks.append("\n".join(captured).strip())
                i = j
                continue

        remaining.append(line)
        i += 1

    return "\n".join(remaining), blocks


def _split_into_bullets(text: str, max_bullets: int = MAX_BULLETS_PER_FIELD) -> list[str]:
    """Break a dense label paragraph into short, scannable bullet points."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []

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


def _process_field_text(raw_text: str) -> tuple[list[str], list[str]]:
    """
    Full per-field pipeline: extract any table-like blocks first (from the
    UNTOUCHED raw text, before whitespace collapsing destroys their
    alignment), then bullet-split whatever prose is left.
    Returns (bullets, table_blocks).
    """
    prose, table_blocks = _extract_structured_blocks(raw_text)
    bullets = _split_into_bullets(prose) if prose.strip() else []
    return bullets, table_blocks


def _format_bullet_line(bullet: str) -> str:
    """Bold a short leading label like 'Adults:' so bullets scan faster."""
    match = _LEAD_RE.match(bullet)
    if match:
        lead, rest = match.groups()
        return f"• *{lead}* {rest}".rstrip()
    return f"• {bullet}"


async def lookup_drug(drug_name: str) -> dict:
    """
    Query openFDA for a drug by generic or brand name.
    Returns a dict of {field_label: {"bullets": [...], "tables": [...]}}
    for whichever fields are present, plus "_name" for the display name.
    "tables" holds raw (verbatim) text blocks that looked like tables in
    the original label -- see _extract_structured_blocks for why these are
    kept separate instead of being flattened into bullets.
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
                bullets, tables = _process_field_text(values[0])
                if bullets or tables:
                    sections[field_label] = {"bullets": bullets, "tables": tables}
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
    escaped = _escape_lucene(drug_name)
    queries = [
        (
            f'openfda.generic_name:"{escaped}" '
            f'OR openfda.brand_name:"{escaped}" '
            f'OR openfda.substance_name:"{escaped}"'
        ),
        f'openfda.substance_name:"{escaped}"',
        f"openfda.generic_name:{escaped} OR openfda.brand_name:{escaped}",
    ]

    async with httpx.AsyncClient(timeout=10) as client:
        for query_index, query in enumerate(queries):
            params = {"search": query, "limit": 1}
            url = f"{OPENFDA_LABEL_URL}?{urllib.parse.urlencode(params)}"
            logger.info("openFDA lookup attempt %d for '%s': %s", query_index, drug_name, url)

            for attempt in range(retries + 1):
                try:
                    response = await client.get(url)
                except (httpx.TimeoutException, httpx.ConnectError) as e:
                    logger.warning("openFDA request error (attempt %d): %s", attempt, e)
                    if attempt < retries:
                        await asyncio.sleep(1)
                        continue
                    break  # BUGFIX: was `return None` here, which aborted the
                            # ENTIRE lookup on a transient network error instead
                            # of trying the next (looser) query variant below.

                logger.info("openFDA response for query %d: status=%s", query_index, response.status_code)

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
                    logger.warning("openFDA unexpected status %s: %s", response.status_code, response.text[:300])
                    break  # unexpected error, try next query variant

                results = response.json().get("results", [])
                if results:
                    logger.info("openFDA match found on query %d for '%s'", query_index, drug_name)
                    return results[0]
                break  # 200 but empty results, try next query variant

    logger.info("openFDA: no match for '%s' after trying all query variants", drug_name)
    return None


def available_sections(sections: dict) -> list[tuple[str, str, str]]:
    """Return (concept_key, field_label, emoji) for each section actually present."""
    present = []
    for concept, field_label, emoji, _keys in FIELDS:
        if field_label in sections:
            present.append((concept, field_label, emoji))
    return present


def format_section(sections: dict, concept: str) -> tuple[str, list[dict]]:
    """
    Format just ONE section of a lookup_drug() result (for button taps).
    Returns (text, table_entries) -- table_entries is a list of
    {"title": str, "text": str} for any table-like blocks found in this
    section, meant to be rendered as images (see table_image.py) and sent
    alongside the text rather than folded into it.
    """
    name = sections.get("_name", "Unknown drug")

    field_label, emoji, data = None, None, None
    for c, label, e, _keys in FIELDS:
        if c == concept:
            field_label, emoji = label, e
            data = sections.get(label)
            break

    if data is None:
        return f"No data available for that section of {name}.", []

    bullets = data.get("bullets", [])
    tables = data.get("tables", [])

    lines = [f"💊 *{name}* — {emoji} *{field_label}*", ""]
    for bullet in bullets:
        lines.append(_format_bullet_line(bullet))
        lines.append("")  # blank line between points so each one stands on its own
    if tables:
        plural = "s" if len(tables) != 1 else ""
        lines.append(f"📊 {len(tables)} table{plural} in this section — sent as image{plural} below.")
        lines.append("")
    if not bullets and not tables:
        lines.append("_(No additional details in this section.)_")
        lines.append("")
    lines.append(DISCLAIMER)

    table_entries = [{"title": f"{emoji} {field_label}", "text": block} for block in tables]
    return "\n".join(lines), table_entries


def format_drug_info(sections: dict) -> tuple[str, list[dict]]:
    """
    Format a lookup_drug() result as a clean, bulleted Telegram message
    (ALL sections). Returns (text, table_entries) -- see format_section.
    """
    name = sections.get("_name", "Unknown drug")
    generic = sections.get("_generic")

    lines = [f"💊 *{name}*"]
    if generic and generic.lower() != name.lower():
        lines.append(f"_({generic})_")
    lines.append("")

    table_entries = []
    for _, field_label, emoji, _keys in FIELDS:
        data = sections.get(field_label)
        if not data:
            continue
        bullets = data.get("bullets", [])
        tables = data.get("tables", [])
        if not bullets and not tables:
            continue

        lines.append(f"{emoji} *{field_label}*")
        for bullet in bullets:
            lines.append(_format_bullet_line(bullet))
            lines.append("")
        if tables:
            plural = "s" if len(tables) != 1 else ""
            lines.append(f"📊 {len(tables)} table{plural} — sent as image{plural} below.")
            lines.append("")
        table_entries.extend({"title": f"{emoji} {field_label}", "text": block} for block in tables)

    lines.append(DISCLAIMER)
    return "\n".join(lines), table_entries


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

"""
AI layer for the 🔀 Drug Interactions checker (interaction_flow.py).

Two jobs, both grounded in real FDA data (openFDA via drug_lookup.py) rather
than Claude's own training knowledge of any specific drug:

  1. resolve_drug_name() -- when a typed name doesn't resolve directly via
     drug_lookup.lookup_drug() (a typo, a brand/trade name openFDA's
     brand_name field doesn't carry, or a name written in the user's own
     language), name_resolver names the real medication meant, so the flow
     can re-try the lookup and confirm with the user ("Did you mean X?")
     instead of failing outright. Claude is only ever asked WHICH drug was
     meant, never to describe or dose it -- the actual FDA label is still
     fetched fresh from openFDA once a name is confirmed.

  2. analyze_interactions() -- given each drug's own FDA-label Drug
     Interactions/Contraindications excerpts (the same drug_lookup.py data
     /dose shows verbatim), ask Claude to read them and describe, in plain
     language, what each label says (or doesn't say) about the others.
     This replaces interaction_flow.py's earlier raw-regex text search with
     a synthesized read of the SAME underlying FDA text, using the same
     "quote/summarize what's there, say plainly when it isn't" discipline
     drug_qa.py uses for single-drug Q&A.
"""

import logging

import cost_ledger
import name_resolver
from config import CLAUDE_MODEL
from pdf_processor import client  # reuse the one Anthropic client instance

logger = logging.getLogger(__name__)

_RELEVANT_FIELD_LABELS = ["Drug Interactions", "Contraindications"]


class InteractionAIError(Exception):
    pass


def resolve_drug_name(raw_input: str) -> str | None:
    """
    Fallback ONLY -- call this after a direct drug_lookup.lookup_drug(raw_input)
    has already raised DrugNotFoundError. Returns the single most likely real
    medication name to re-try the lookup with, or None.

    Delegates to name_resolver, so a name typed in Persian/Arabic/Urdu
    resolves here exactly as it does everywhere else a drug name is typed.
    This flow adds drugs one at a time and confirms each with a yes/no, so
    it takes only the top candidate rather than offering the whole list.
    """
    try:
        names = name_resolver.resolve_drug_names(raw_input, limit=1)
    except name_resolver.NameResolverError as e:
        raise InteractionAIError(str(e))
    return names[0] if names else None


def _label_context(name: str, sections: dict) -> str:
    lines = [f"== {name} =="]
    found = False
    for label in _RELEVANT_FIELD_LABELS:
        data = sections.get(label)
        bullets = (data or {}).get("bullets") or []
        if not bullets:
            continue
        found = True
        lines.append(f"-- {label} --")
        lines.extend(bullets)
    if not found:
        lines.append("(No Drug Interactions or Contraindications section in this label.)")
    return "\n".join(lines)


def analyze_interactions(drugs: list[dict], language: str = "English") -> str:
    """
    drugs: [{"name": str, "sections": <drug_lookup.lookup_drug() result>}, ...],
    at least 2. Asks Claude to read each drug's own Drug Interactions/
    Contraindications excerpts and describe what they say (or don't say)
    about the others -- grounded ONLY in that fetched FDA text.
    """
    names = ", ".join(d["name"] for d in drugs)
    context = "\n\n".join(_label_context(d["name"], d["sections"]) for d in drugs)

    system_prompt = (
        f"You are analyzing possible interactions between these medications: {names}. Below are the "
        "Drug Interactions and Contraindications excerpts from EACH drug's own official FDA label "
        "(openFDA) -- the same source this bot's /dose command shows verbatim. Follow these rules "
        "strictly:\n"
        "1. Base your analysis ONLY on the excerpts given below -- never your own memorized/training "
        "knowledge of these drugs, even if you're confident about it, since the excerpts are the "
        "authoritative, current source here and outside knowledge can be outdated or wrong for this "
        "specific product.\n"
        "2. For each pair of drugs, state plainly whether either one's label mentions the other -- by "
        "name, or by drug class (e.g. 'other CNS depressants', 'strong CYP3A4 inhibitors') -- and if "
        "so, summarize what it says and which drug's label it came from.\n"
        "3. If neither label mentions a given pair at all, say so plainly -- do not invent a mechanism, "
        "severity rating, or clinical recommendation that isn't in the text.\n"
        "4. Be concise and direct -- this will be read on a phone screen.\n"
        f"4b. Write your entire answer in {language}. Drug names, and abbreviations normally left in "
        "English (CYP3A4, QT), stay as they are.\n"
        "5. End with one line noting this reads each drug's own label text only: it is not a substitute "
        "for a dedicated interaction-checker database or pharmacist consult, and a label not mentioning "
        "something does not rule out a real interaction.\n"
        f"\nFDA LABEL EXCERPTS:\n{context}"
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": f"Analyze possible interactions among: {names}"}],
        )
    except Exception as e:
        raise InteractionAIError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response("interaction_analysis", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    return "".join(block.text for block in response.content if block.type == "text").strip()

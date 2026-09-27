"""
Understand what the user typed, whatever language they typed it in.

The bot answers in the user's chosen language (see language.py), but several
things it looks text up IN are English-only and always will be: openFDA's
drug label database, and glossary.py's term list. So a Persian, Arabic or
Urdu speaker could set their language, get Persian answers, and still hit a
flat "not found" the moment they typed a drug name in their own script --
the bot spoke their language but couldn't listen in it.

This module is the listening half. Claude is asked ONLY to identify what
real thing the typed text refers to -- never to describe, dose, or define
it. The answer still comes from the real English source (the FDA label,
the curated glossary entry) once a name is resolved, so nothing here puts
medical content in the model's mouth.

Used as a FALLBACK everywhere, never a first step: a direct lookup is always
tried first, so English input costs nothing extra and behaves exactly as
before. Only a miss reaches Claude.

Callers must confirm a resolved name with the user before acting on it
(see drug_qa_flow.py / bot.py's "Did you mean...?" keyboards). A silent
guess is how someone ends up reading the wrong drug's label.
"""

import logging

import cost_ledger
from config import CLAUDE_MODEL
from pdf_processor import client  # reuse the one Anthropic client instance

logger = logging.getLogger(__name__)

MAX_DRUG_SUGGESTIONS = 4  # more than this is a menu, not a "did you mean"


class NameResolverError(Exception):
    pass


def _ask(system_prompt: str, user_text: str, feature: str, max_tokens: int) -> str:
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_text.strip()}],
        )
    except Exception as e:
        raise NameResolverError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response(feature, response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    return "".join(block.text for block in response.content if block.type == "text").strip()


def _clean_line(line: str) -> str:
    """Strip list formatting Claude may add despite being told not to."""
    return line.strip().lstrip("-•*0123456789.) ").strip(" \"'.")


def resolve_drug_names(raw_input: str, limit: int = MAX_DRUG_SUGGESTIONS) -> list[str]:
    """
    Which real medication(s) does `raw_input` mean? Returns up to `limit`
    English names, most likely first, or [] if it isn't a medication at all.

    Handles three things at once, because from the user's side they're the
    same problem -- "the name I typed didn't work": a misspelling, a brand
    name the FDA record doesn't carry, and a name written in another
    language or script.

    Several names are returned rather than one because a mangled or
    transliterated name is often genuinely ambiguous ("clonaz" ->
    clonazepam or clonidine), and the caller offers the choice.
    """
    system_prompt = (
        "The user typed a medication name into a drug-information tool, but it didn't match anything "
        "in the FDA drug label database. Your ONLY job is to identify which real medication(s) they "
        "meant.\n\n"
        "The input may be any of:\n"
        "- a misspelling or a partial name\n"
        "- a brand/trade name the FDA record doesn't carry\n"
        "- the drug's name written in ANOTHER LANGUAGE OR SCRIPT (Persian/Farsi, Arabic, Urdu, Hindi, "
        "Turkish, Russian, etc.), or transliterated into Latin letters\n\n"
        "Rules:\n"
        f"- Reply with 1 to {limit} medication names, most likely first, one per line.\n"
        "- Always reply in ENGLISH using the GENERIC name, whatever language the input was in -- that is "
        "what a drug label database indexes. Resolve brand names to their generic.\n"
        "- Name the plain single-ingredient drug, not a combination product or a brand that happens to "
        "contain it: 'metofm' means metformin, never a metformin-containing combination.\n"
        "- Give more than one name only when the input is genuinely ambiguous between different "
        "medications. If one reading is clearly right, reply with just that one.\n"
        "- Output ONLY the names -- no numbering, bullets, punctuation, explanation or commentary.\n"
        "- If you cannot place the input as any real medication at all, reply with exactly: NONE"
    )
    text = _ask(system_prompt, raw_input, "drug_name_resolve", max_tokens=100)
    if not text or text.strip().upper() == "NONE":
        return []

    names: list[str] = []
    for line in text.splitlines():
        name = _clean_line(line)
        if name and name.upper() != "NONE" and name.lower() not in [n.lower() for n in names]:
            names.append(name)
    return names[:limit]


def resolve_glossary_term(raw_input: str, known_terms: list[str]) -> str | None:
    """
    Which entry of `known_terms` does `raw_input` refer to? Returns one term
    EXACTLY as it appears in that list, or None.

    Claude picks from the caller's list rather than naming a term freely, so
    the result is always something glossary.py can actually look up -- the
    answer shown to the user is still the curated glossary definition, never
    anything Claude wrote.
    """
    if not known_terms:
        return None

    system_prompt = (
        "The user searched a medical glossary for a term, and it didn't match any entry directly. "
        "Their input may be a misspelling, an alternative name or abbreviation, or the term written "
        "in ANOTHER LANGUAGE OR SCRIPT (Persian/Farsi, Arabic, Urdu, etc.).\n\n"
        "Below is the complete list of terms this glossary contains. Decide which ONE of them the "
        "user was looking for.\n\n"
        "Rules:\n"
        "- Reply with the matching term copied EXACTLY as it appears in the list below, and nothing "
        "else -- no explanation, no punctuation, no translation.\n"
        "- The term must be one from the list. Never invent or reword one.\n"
        "- If none of them is what the user meant, reply with exactly: NONE\n\n"
        "GLOSSARY TERMS:\n" + "\n".join(known_terms)
    )
    text = _clean_line(_ask(system_prompt, raw_input, "glossary_term_resolve", max_tokens=30))
    if not text or text.upper() == "NONE":
        return None

    # Trust the list, not the reply: match case-insensitively back onto the
    # caller's own terms so a reply that differs in case or spacing still
    # resolves, and anything not in the list is discarded rather than
    # handed on to a lookup that would only fail.
    for term in known_terms:
        if term.lower() == text.lower():
            return term
    return None

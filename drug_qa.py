"""
"Ask AI about a drug" -- free-form Q&A grounded in a single drug's real FDA
label (drug_lookup.py's openFDA data), not a general knowledge answer from
Claude's own training. Complements /dose's fixed section-by-section menu
with "just ask" for anything the label covers -- dose, side effects,
contraindications, interactions, use in pregnancy, etc. -- in one turn,
without having to know which section it lives under.

Unlike pdf_qa.py's "Ask my book" (which needs Voyage embeddings + a
retrieval step because a whole textbook doesn't fit in one prompt), a
single drug's label is small enough that no retrieval is needed: the whole
formatted label is handed to Claude directly as context every time. This
keeps the answer grounded in the SAME data /dose already shows verbatim
from openFDA -- Claude is never asked to recall or invent a dose from its
own memory, only to find, quote, and summarize what's actually in the
fetched label, and to say plainly when the label doesn't address the
question rather than filling the gap with outside knowledge.
"""

import logging

import cost_ledger
from config import CLAUDE_MODEL
from drug_lookup import DISCLAIMER, FIELDS
from pdf_processor import client  # reuse the one Anthropic client instance

logger = logging.getLogger(__name__)

MAX_HISTORY_TURNS = 6  # older turns are dropped rather than growing the prompt without bound


class DrugQAError(Exception):
    pass


def _label_context(sections: dict) -> str:
    """
    Flatten a drug_lookup.lookup_drug() result into one plain-text block,
    grouped by section heading -- bullets only. Table blocks are skipped
    here: they're raw, page-layout-alignment-dependent text pulled from the
    original label (see drug_lookup._extract_structured_blocks), meant to be
    rendered as a monospace image, not read as prose context by Claude --
    /dose's own section view already surfaces them verbatim as images when
    a user taps into a section that has one.
    """
    lines = []
    for _, field_label, _emoji, _keys in FIELDS:
        data = sections.get(field_label)
        if not data:
            continue
        bullets = data.get("bullets") or []
        if not bullets:
            continue
        lines.append(f"== {field_label} ==")
        lines.extend(bullets)
        lines.append("")
    return "\n".join(lines).strip()


def answer_question(
    drug_name: str, sections: dict, question: str, history: list[dict] | None = None, language: str = "English"
) -> str:
    """
    Synchronous -- run via asyncio.to_thread from an async handler.

    sections: an already-fetched drug_lookup.lookup_drug() result -- the
    caller fetches it ONCE (either fresh, or reused from a just-completed
    /dose lookup's session_cache entry) and passes it in again for every
    follow-up question in the same session, the same "fetch once, ask
    repeatedly" pattern pdf_qa.py uses for an indexed book.

    history, if given, is prior turns for THIS drug/session as
    [{"question": str, "answer": str}, ...], oldest first, replayed as real
    multi-turn messages so a follow-up ("and in renal impairment?") reads
    naturally instead of needing to be a fully self-contained question.
    """
    context = _label_context(sections)
    if not context:
        raise DrugQAError(f"No usable label sections for {drug_name} to answer questions from.")

    history = (history or [])[-MAX_HISTORY_TURNS:]

    system_prompt = (
        f"You are answering questions about the drug '{drug_name}' using ONLY the FDA label excerpts "
        "below (from openFDA, the same source this bot's /dose command shows verbatim). Follow these "
        "rules strictly:\n"
        "1. Answer using only information in the excerpts -- never your own memorized/training "
        "knowledge of this drug, even if you're confident about it, since the excerpts are the "
        "authoritative, current source here and outside knowledge can be outdated or wrong for this "
        "specific product/formulation.\n"
        "2. Note which section(s) your answer is drawn from, e.g. '(see Dosage & Administration)', so "
        "the user can jump to it via /dose for the full wording.\n"
        "3. If the excerpts don't address the question, say so plainly instead of guessing or falling "
        "back on general knowledge.\n"
        "4. Never invent or calculate a final numeric dose yourself. If the label states a rule that "
        "needs a calculation (e.g. weight-based or renal-adjusted dosing), quote the rule/formula as "
        "written and let the user apply it -- do not compute and state a specific number as if it were "
        "the label's own text.\n"
        "5. Be concise and direct -- this will be read on a phone screen.\n"
        + (
            "6. This is one turn in an ongoing conversation about this drug -- let the answer flow "
            "naturally from the earlier turns shown, without re-explaining what's already covered.\n"
            if history
            else ""
        )
        + f"7. Respond in {language}.\n"
        + f"\nFDA LABEL EXCERPTS FOR {drug_name.upper()}:\n{context}"
    )

    messages = []
    for turn in history:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})
    messages.append({"role": "user", "content": question})

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            system=system_prompt,
            messages=messages,
        )
    except Exception as e:
        raise DrugQAError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response("drug_qa", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text + "\n\n" + DISCLAIMER

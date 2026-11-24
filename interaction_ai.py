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

import asyncio
import logging
import re

import cost_ledger
import interaction_rules
import name_resolver
import rxclass
from config import CLAUDE_MODEL
from pdf_processor import client  # reuse the one Anthropic client instance

logger = logging.getLogger(__name__)

_RELEVANT_FIELD_LABELS = ["Drug Interactions", "Contraindications"]

_HEADING_RE = re.compile(r"(?m)^\s*#{1,4}\s+(.+?)\s*$")
_BOLD2_RE = re.compile(r"\*\*(.+?)\*\*")


def to_telegram_markdown(text: str) -> str:
    """The answer's '### A + B' headings and **bold** as Telegram's legacy Markdown (*bold*)."""
    text = _HEADING_RE.sub(lambda m: "*" + m.group(1).replace("*", "") + "*", text)
    return _BOLD2_RE.sub(r"*\1*", text)


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


def _label_context(drug: dict, scale: float = 1.0) -> str:
    name, sections = drug["name"], drug["sections"]
    lines = [f"== {name} =="]
    generic = sections.get("_generic")
    if generic and generic.lower() != name.lower():
        lines.append(f"(generic name: {generic})")

    classes = drug.get("classes") or []
    if classes:
        lines.append("-- Drug classes (FDA label classes and NIH RxClass; for matching class-level statements) --")
        lines.extend(f"- {c}" for c in classes)

    profile = interaction_rules.fda_profile(drug)
    if profile:
        lines.append(f"-- FDA table of CYP/transporter interacting drugs (as of {interaction_rules.FDA_AS_OF}) --")
        lines.extend(profile)

    found = False
    for label in _RELEVANT_FIELD_LABELS:
        data = sections.get(label) or {}
        bullets = data.get("bullets") or []
        # Tables matter most here: many current labels put the actual
        # interaction list ("Table 4: Clinically Significant Drug
        # Interactions") in a table, and leaving them out made the model
        # report "neither label mentions X" about drugs the table names.
        tables = data.get("tables") or []
        if not bullets and not tables:
            continue
        found = True
        lines.append(f"-- {label} --")
        lines.extend(bullets)
        for i, block in enumerate(tables, 1):
            lines.append(f"[Table {i} from this section, verbatim; columns may be misaligned]")
            lines.append(block)
    if not found:
        lines.append("(No Drug Interactions or Contraindications section in this label.)")

    # Boxed warning, interaction-relevant warnings, metabolism/transporters
    # (drug_lookup.INTERACTION_EXTRA) -- shortened when many drugs are checked.
    for label, text in (sections.get("_interaction_extra") or {}).items():
        limit = max(400, int(len(text) * scale))
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0] + " [...]"
        lines.append(f"-- {label} --")
        lines.append(text)
    return "\n".join(lines)


async def add_drug_classes(drugs: list[dict]) -> None:
    """
    Attach each drug's classes (d["classes"]): the label's own FDA classes
    plus NIH RxClass (rxclass.py), looked up in parallel. Never raises -- a
    failed lookup only means less to match class-level warnings against.
    """
    async def one(d):
        sections = d.get("sections") or {}
        own = list(sections.get("_pharm_classes") or [])
        try:
            extra = await rxclass.drug_classes(sections.get("_generic") or d["name"])
        except Exception:
            logger.exception("RxClass lookup failed for %r", d.get("name"))
            extra = []
        seen, merged = set(), []
        for c in own + extra:
            key = re.sub(r"\s*[\[(].*$", "", c.split(": ", 1)[-1]).strip().lower()
            if key and key not in seen:
                seen.add(key)
                merged.append(c)
        d["classes"] = merged[:25]

    await asyncio.gather(*(one(d) for d in drugs))



def analyze_interactions(drugs: list[dict], language: str = "English") -> str:
    """
    drugs: [{"name": str, "sections": <drug_lookup.lookup_drug() result>,
    "classes": [...] (optional, from add_drug_classes)}, ...], at least 2.
    Asks Claude to read each drug's own FDA label text (interactions,
    contraindications, boxed warning, relevant warnings, metabolism) and
    class data, and report every pair in a fixed, scannable format --
    grounded ONLY in that fetched text.
    """
    names = ", ".join(d["name"] for d in drugs)
    # Up to 5 drugs get the full extra sections; beyond that they shrink so
    # a 15-drug check stays a sensible size.
    scale = min(1.0, 5 / len(drugs))
    context = "\n\n".join(_label_context(d, scale) for d in drugs)
    findings = interaction_rules.findings(drugs)

    system_prompt = (
        f"You are checking for interactions between these medications: {names}. Below, for EACH drug, "
        "is text from its own official FDA label (openFDA): Drug Interactions, Contraindications, Boxed "
        "Warning, the interaction-relevant sentences of its Warnings and its Metabolism/Pharmacokinetics "
        "sections, its drug classes (the FDA's label classes and the NIH's RxClass), and its entry in the "
        "FDA's table of drugs that interact with CYP enzymes and transporters (which gives potency: "
        "strong/moderate/weak inhibitor or inducer, sensitive substrate). Above the label material are "
        "PRE-COMPUTED FINDINGS, matched exactly from that FDA table and from the expert-panel list of 15 "
        "high-priority interactions. Rules:\n"
        "1. Use ONLY the material below -- never your own memorized knowledge of these drugs, even if "
        "confident. It is the authoritative, current source here.\n"
        "2. Check EVERY pair. A pair interacts when either label names the other drug, or names a class "
        "or property the other drug has according to ITS material (classes list, metabolism, warnings) -- "
        "e.g. A's label says 'avoid strong CYP3A4 inhibitors' and B is listed as a 'Cytochrome P450 3A4 "
        "Inhibitor'; or A is 'metabolized by CYP2D6' and B's label calls it a strong CYP2D6 inhibitor; or "
        "both labels warn of QT prolongation, serotonin syndrome, bleeding, CNS/respiratory depression, "
        "hyperkalemia etc. (an additive effect). Say which way you matched it.\n"
        "2b. Every PRE-COMPUTED FINDING is established: each one MUST appear in a pair block (several "
        "findings for one pair go in the same block). A high-priority finding sets the minimum severity "
        "it states; the labels can only raise it (e.g. to 🔴 when a label says contraindicated). For an "
        "FDA-table finding, grade by the labels' wording when they address it (a label's 'avoid strong "
        "CYP3A inhibitors' + the table saying the other drug IS a strong CYP3A inhibitor = that label's "
        "severity); otherwise use 'Possible — not graded by the labels' and state the expected size of the "
        "effect from the table's definitions (strong inhibitor + sensitive substrate: exposure up 5-fold "
        "or more).\n"
        "3. Severity -- take it from the label's own wording, never invent it:\n"
        "   🔴 Contraindicated: 'contraindicated', 'do not use/coadminister'.\n"
        "   🟠 Major: 'avoid', a boxed warning about the combination, or wording like serious, "
        "life-threatening, fatal.\n"
        "   🟡 Moderate: 'monitor', 'adjust/reduce the dose', 'use with caution', 'consider'.\n"
        "   🟢 Minor: the label says the effect is not clinically significant or needs no action.\n"
        "   When the match is only an inference from classes/metabolism (no label states this "
        "combination or its class), write 'Possible — not graded by the labels' instead of a colour, "
        "and say what would make it clinically relevant. RxClass/label classes do not tell potency: "
        "call an inhibitor or inducer 'strong'/'moderate'/'weak' only when a label or the FDA CYP table "
        "says so.\n"
        "4. Exact format (Markdown, for a phone screen):\n"
        "**Bottom line:** one or two sentences: the most important finding(s) and what to do.\n"
        "Then one block per interacting pair, most severe first:\n"
        "### <Drug A> + <Drug B>\n"
        "- **Severity:** <emoji + level, or 'Possible — not graded by the labels'>\n"
        "- **Mechanism:** <how, e.g. B inhibits CYP3A4, which clears A -- only as the material states>\n"
        "- **What can happen:** <the clinical effect the label describes>\n"
        "- **What to do:** <the label's management advice: avoid, dose change, monitoring; "
        "'Not stated in the labels' if absent>\n"
        "- **Source:** <which drug's label and section; 'FDA CYP table'; 'High-priority DDI list'; or "
        "'class data (RxClass)'>\n"
        "Finally, one line: **No interaction found in the labels:** <the remaining pairs, "
        "comma-separated> (omit if none). Do not write a block for those pairs.\n"
        "5. Be concise: short phrases, no repetition, no preamble.\n"
        f"6. Write the whole answer in {language}, including the field names (Severity, Mechanism...). "
        "Drug names and abbreviations normally left in English (CYP3A4, QT, INR) stay as they are; keep "
        "the colour emoji.\n"
        "7. End with one short line: based on FDA labels, the FDA CYP table and class data only -- not a "
        "substitute for a dedicated interaction database or a pharmacist; a label not mentioning "
        "something does not rule out an interaction.\n"
        + (f"\nPRE-COMPUTED FINDINGS:\n{findings}\n" if findings else "\nPRE-COMPUTED FINDINGS: none.\n")
        + f"\nFDA LABEL MATERIAL:\n{context}"
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            # Sonnet 5 thinks by default when this is omitted, and thinking
            # tokens count against max_tokens -- which is sized for the answer.
            thinking={"type": "disabled"},
            max_tokens=3000,
            system=system_prompt,
            messages=[{"role": "user", "content": f"Check interactions among: {names}"}],
        )
    except Exception:
        logger.exception("AI request failed")
        raise InteractionAIError("The AI service didn't respond properly. Please try again in a moment.")

    try:
        cost_ledger.record_claude_response("interaction_analysis", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    return "".join(block.text for block in response.content if block.type == "text").strip()

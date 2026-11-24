"""
Drug classes from the NIH's RxClass API (free, no key):
https://lhncbc.nlm.nih.gov/RxNav/APIs/RxClassAPIs.html

Used by the interaction checker to connect a label's class-level warning
("avoid with strong CYP3A4 inhibitors", "other serotonergic drugs") to the
other drug actually on the list: RxClass knows clarithromycin is a
"Cytochrome P450 3A4 Inhibitor" and sertraline a "Serotonin Reuptake
Inhibitor", which neither drug's interaction section necessarily says.

Two sources per ingredient:
  DAILYMED  the FDA's own pharmacologic classes from the label (established
            class, mechanism of action -- where the CYP / P-gp inhibitor and
            inducer classes live -- and physiologic effect)
  ATC       the WHO therapeutic classification (e.g. "Selective serotonin
            reuptake inhibitors"), which also covers drugs whose label
            carries no FDA class at all

Best effort throughout: a slow or failed lookup returns what it has (maybe
nothing) and the check carries on from the label text alone.
"""

import asyncio
import logging
import re

import httpx

logger = logging.getLogger(__name__)

_URL = "https://rxnav.nlm.nih.gov/REST/rxclass/class/byDrugName.json"
_TIMEOUT = 6.0
_CACHE_MAX = 1000
_cache: dict[str, list[str]] = {}

_TYPE_LABELS = {
    "EPC": "pharmacologic class",
    "MOA": "mechanism",
    "PE": "physiologic effect",
}
_SPLIT_RE = re.compile(r"\s*(?:,|/|\band\b|\bwith\b|\+)\s*", re.I)
# Salt and hydrate words a label's generic name carries ("clavulanate
# potassium", "sertraline hydrochloride") that RxClass's name match may not.
_SALT_RE = re.compile(
    r"\b(hydrochloride|hcl|hydrobromide|sodium|potassium|calcium|magnesium|sulfate|maleate|mesylate|"
    r"besylate|tartrate|bitartrate|citrate|succinate|fumarate|acetate|phosphate|bromide|hyclate|"
    r"monohydrate|dihydrate|trihydrate|hemihydrate|anhydrous|dipropionate|propionate|valerate)\b",
    re.I,
)


def _base_name(ingredient: str) -> str:
    stripped = re.sub(r"\s+", " ", _SALT_RE.sub(" ", ingredient)).strip()
    return stripped if len(stripped) > 2 else ingredient


async def _query(http: httpx.AsyncClient, name: str, source: str) -> list[str] | None:
    """The classes, [] when RxClass has none, or None when the lookup itself failed."""
    try:
        resp = await http.get(_URL, params={"drugName": name, "relaSource": source})
        resp.raise_for_status()
        items = ((resp.json().get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo")) or []
    except Exception as e:
        logger.info("RxClass %s lookup failed for %r: %s", source, name, type(e).__name__)
        return None
    out = []
    for item in items:
        # Only the single ingredient itself: by name, RxClass also returns
        # the classes of every combination product containing it -- which
        # made simvastatin a "DPP-4 inhibitor" via simvastatin/sitagliptin.
        if (item.get("minConcept") or {}).get("tty") == "MIN":
            continue
        concept = item.get("rxclassMinConceptItem") or {}
        cname, ctype = concept.get("className"), concept.get("classType") or ""
        if not cname:
            continue
        if ctype.startswith("ATC"):
            out.append(f"{cname} (ATC)")
        elif ctype in _TYPE_LABELS:
            out.append(f"{cname} ({_TYPE_LABELS[ctype]})")
    return out


async def _ingredient_classes(http: httpx.AsyncClient, ingredient: str) -> list[str]:
    key = ingredient.lower()
    if key in _cache:
        return _cache[key]
    daily, atc = await asyncio.gather(_query(http, ingredient, "DAILYMED"), _query(http, ingredient, "ATC"))
    classes = list(dict.fromkeys((daily or []) + (atc or [])))
    # Only a complete answer is cached: one timeout used to leave a drug
    # without its classes, for every user, until the next restart.
    if daily is not None and atc is not None:
        if len(_cache) >= _CACHE_MAX:
            _cache.pop(next(iter(_cache)))
        _cache[key] = classes
    return classes


async def drug_classes(name: str) -> list[str]:
    """
    Classes for every ingredient of `name` (a combination product's generic
    name, "amlodipine and benazepril", is split), each tagged with the
    ingredient when there is more than one.
    """
    ingredients = [_base_name(p) for p in _SPLIT_RE.split((name or "").strip()) if len(p) > 2][:4]
    if not ingredients:
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            results = await asyncio.gather(*(_ingredient_classes(http, i) for i in ingredients))
    except Exception:
        logger.exception("RxClass lookup failed for %r", name)
        return []
    if len(ingredients) == 1:
        return results[0]
    return [f"{ing.lower()}: {c}" for ing, classes in zip(ingredients, results) for c in classes]

"""
PubMed through NCBI's official E-utilities API (free; no login, no browser):
https://www.ncbi.nlm.nih.gov/books/NBK25501/

Drug LookUp can add PubMed to an answer: the question becomes a few search
terms, the most relevant articles that have an abstract are fetched, and each
abstract is one cited passage linking to its PubMed page.

Without a key NCBI allows 3 requests a second; NCBI_API_KEY (free, from an
NCBI account) raises that to 10. Best effort: a slow or failed search returns
nothing and the answer carries on from the label.
"""

import logging
import os
import xml.etree.ElementTree as ET

import httpx

logger = logging.getLogger(__name__)

SOURCE_ID = "pubmed"
SOURCE_TITLE = "PubMed (NCBI)"

_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
_TIMEOUT = 12.0
_MAX_ARTICLES = 4
_MAX_CHARS = 3000


def _params(extra: dict) -> dict:
    params = {"db": "pubmed", "tool": "medic-toolkit", **extra}
    key = os.environ.get("NCBI_API_KEY")
    if key:
        params["api_key"] = key
    return params


def _text(el) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _parse(xml_text: str) -> list[dict]:
    out = []
    root = ET.fromstring(xml_text)
    for art in root.iter("PubmedArticle"):
        pmid = _text(art.find(".//MedlineCitation/PMID"))
        title = _text(art.find(".//Article/ArticleTitle"))
        parts = []
        for ab in art.findall(".//Article/Abstract/AbstractText"):
            label = ab.get("Label")
            body = _text(ab)
            if body:
                parts.append(f"{label}: {body}" if label else body)
        if not pmid or not parts:
            continue
        journal = _text(art.find(".//Article/Journal/ISOAbbreviation")) or _text(art.find(".//Article/Journal/Title"))
        year = _text(art.find(".//Article/Journal/JournalIssue/PubDate/Year")) or _text(
            art.find(".//Article/Journal/JournalIssue/PubDate/MedlineDate")
        )[:4]
        types = [_text(t) for t in art.findall(".//PublicationTypeList/PublicationType")]
        kind = next((t for t in types if t in ("Meta-Analysis", "Systematic Review", "Randomized Controlled Trial", "Review", "Practice Guideline", "Guideline")), "")
        cite = ", ".join(x for x in (journal, year, kind) if x)
        text = f"{title} ({cite}). Abstract: " + " ".join(parts)
        out.append({
            "text": text[:_MAX_CHARS],
            "title": f"PubMed: {title[:150]}" + (f" ({cite})" if cite else ""),
            "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            "book_id": SOURCE_ID,
            "page": None,
        })
    return out


async def passages(terms: str) -> list[dict]:
    """The most relevant PubMed abstracts for these search terms, as cited passages."""
    terms = (terms or "").strip()
    if not terms:
        return []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        r = await http.get(
            f"{_BASE}/esearch.fcgi",
            params=_params({"term": f"({terms}) AND hasabstract", "retmode": "json", "retmax": _MAX_ARTICLES, "sort": "relevance"}),
        )
        r.raise_for_status()
        ids = ((r.json().get("esearchresult") or {}).get("idlist")) or []
        ids = [i for i in ids if str(i).isdigit()][:_MAX_ARTICLES]
        if not ids:
            return []
        r = await http.get(f"{_BASE}/efetch.fcgi", params=_params({"id": ",".join(ids), "retmode": "xml"}))
        r.raise_for_status()
    found = {p["url"]: p for p in _parse(r.text)}
    # In the relevance order esearch gave.
    return [found[u] for u in (f"https://pubmed.ncbi.nlm.nih.gov/{i}/" for i in ids) if u in found]

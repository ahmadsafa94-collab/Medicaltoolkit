"""
"Are the keys working?" -- one cheap live call per external service.

Runs once at every startup (results go to the log, one line per service)
and on demand from the admin panel's 🧪 Test functionality -> 🔑 Check API
keys. Each check costs a fraction of a cent: a 5-token Claude reply, a
one-word embedding, a two-passage rerank, one openFDA query.

The point is to turn "every AI feature is broken" into one readable line
naming the service and the reason (bad key, no credit, workspace needed,
rate limited) instead of a traceback buried in a user's failed request.
"""

import asyncio
import logging

import drug_lookup
import pdf_qa
from config import CLAUDE_MODEL, TRANSLATION_MODEL, VOYAGE_MODEL, VOYAGE_RERANK_MODEL
from pdf_processor import client as claude_client

logger = logging.getLogger(__name__)

_TIMEOUT = 30


def _reason(e: Exception) -> str:
    """The provider's own message, without the Python wrapping around it."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        msg = err.get("message") or err.get("detail")
        if msg:
            return str(msg)[:300]
    return (str(e) or type(e).__name__)[:300]


def _claude(model: str) -> None:
    claude_client.messages.create(
        model=model, max_tokens=5, messages=[{"role": "user", "content": "Reply with: ok"}]
    )


async def _check(name: str, fn) -> tuple[str, bool, str]:
    try:
        await asyncio.wait_for(fn(), timeout=_TIMEOUT)
        return name, True, "OK"
    except asyncio.TimeoutError:
        return name, False, f"no answer within {_TIMEOUT}s"
    except Exception as e:
        return name, False, _reason(e)


async def run_checks() -> list[tuple[str, bool, str]]:
    async def anthropic_main():
        await asyncio.to_thread(_claude, CLAUDE_MODEL)

    async def anthropic_translation():
        await asyncio.to_thread(_claude, TRANSLATION_MODEL)

    async def voyage_embed():
        await pdf_qa.embed_texts(["lithium dosing"], input_type="query")

    async def voyage_rerank():
        await asyncio.to_thread(
            pdf_qa.voyage_client.rerank, "lithium", ["lithium dose", "insulin dose"], model=VOYAGE_RERANK_MODEL, top_k=1
        )

    async def openfda():
        await drug_lookup.lookup_drug("ibuprofen")

    async def web_browser():
        import web_sources
        await web_sources.self_test()

    return list(
        await asyncio.gather(
            _check(f"Anthropic ({CLAUDE_MODEL})", anthropic_main),
            _check(f"Anthropic translations ({TRANSLATION_MODEL})", anthropic_translation),
            _check(f"Voyage search ({VOYAGE_MODEL})", voyage_embed),
            _check(f"Voyage rerank ({VOYAGE_RERANK_MODEL})", voyage_rerank),
            _check("openFDA drug labels", openfda),
            _check("Web browser (web sources)", web_browser),
        )
    )


def format_results(results) -> str:
    return "\n".join(f"{'✅' if ok else '❌'} {name}: {detail}" for name, ok, detail in results)


async def log_startup_checks() -> None:
    try:
        results = await run_checks()
    except Exception:
        logger.exception("Startup key check crashed (non-fatal)")
        return
    for name, ok, detail in results:
        (logger.info if ok else logger.error)("KEY CHECK %s %s: %s", "OK " if ok else "FAIL", name, detail)

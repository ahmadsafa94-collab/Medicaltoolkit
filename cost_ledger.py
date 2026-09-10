"""
Lightweight running log of estimated Claude/Voyage API spend, feeding the
admin panel's cost dashboard (see admin_flow.py). Every logging call is
best-effort and wrapped by its caller in a broad try/except -- a bug here
must never take down the actual feature it's instrumenting, the same
principle telegram_helpers.py and chapter_ai.py already apply to progress
callbacks.

Deliberately a flat append-only JSONL file rather than a database: this bot
already persists everything else (library.py, subscriptions.py) as small
per-purpose JSON/JSONL files, and cost entries are write-once, read-rarely
(only when the admin opens the cost dashboard).

Token counts are read from each Claude response's own `.usage` field (real
usage, not an estimate of input size) where the SDK provides it -- getattr()
with defaults throughout so an older/stubbed response object without a
`.usage` attribute just logs nothing instead of raising.
"""

import json
import logging
import os
import time

from config import (
    STORAGE_DIR,
    CLAUDE_INPUT_PRICE_PER_MILLION_USD,
    CLAUDE_OUTPUT_PRICE_PER_MILLION_USD,
    VOYAGE_PRICE_PER_MILLION_USD,
)

logger = logging.getLogger(__name__)

_ADMIN_DIR = os.path.join(STORAGE_DIR, "_admin")
_LOG_PATH = os.path.join(_ADMIN_DIR, "cost_log.jsonl")


def _append(entry: dict) -> None:
    try:
        os.makedirs(_ADMIN_DIR, exist_ok=True)
        with open(_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        logger.exception("Failed to append to cost ledger (non-fatal)")


def record_claude_response(feature: str, response, user_id: int | None = None) -> float:
    """
    Log the real cost of one Claude API call, read from response.usage.
    Returns the estimated USD cost (0.0 if the response has no usable usage
    info, e.g. a test stub's fake response).
    """
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None) if usage else None
    output_tokens = getattr(usage, "output_tokens", None) if usage else None
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return 0.0

    usd = (
        input_tokens / 1_000_000 * CLAUDE_INPUT_PRICE_PER_MILLION_USD
        + output_tokens / 1_000_000 * CLAUDE_OUTPUT_PRICE_PER_MILLION_USD
    )
    _append(
        {
            "ts": time.time(),
            "kind": "claude",
            "feature": feature,
            "user_id": user_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usd": usd,
        }
    )
    return usd


def record_voyage_tokens(feature: str, total_tokens: int, user_id: int | None = None) -> float:
    """Log an estimated Voyage embedding cost -- Voyage's Python SDK doesn't return per-call token usage, so this is called with a caller-computed character/4 estimate rather than a real count."""
    if not isinstance(total_tokens, int) or total_tokens <= 0:
        return 0.0
    usd = total_tokens / 1_000_000 * VOYAGE_PRICE_PER_MILLION_USD
    _append(
        {
            "ts": time.time(),
            "kind": "voyage",
            "feature": feature,
            "user_id": user_id,
            "tokens": total_tokens,
            "usd": usd,
        }
    )
    return usd


def summarize_costs(days: int = 30) -> dict:
    """
    Returns {"total_usd": float, "calls": int, "by_feature": {feature: usd}}
    for entries logged in the last `days` days. Used by the admin panel's
    cost dashboard -- reads the whole log file on each call, which is fine
    at this bot's scale (a few thousand lines is a trivial read) and keeps
    the log itself dead simple (no rotation/compaction logic needed yet).
    """
    cutoff = time.time() - days * 86400
    total = 0.0
    calls = 0
    by_feature: dict[str, float] = {}

    if not os.path.exists(_LOG_PATH):
        return {"total_usd": 0.0, "calls": 0, "by_feature": {}}

    try:
        with open(_LOG_PATH) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("ts", 0) < cutoff:
                    continue
                usd = entry.get("usd", 0.0)
                feature = entry.get("feature", "other")
                total += usd
                calls += 1
                by_feature[feature] = by_feature.get(feature, 0.0) + usd
    except OSError:
        logger.exception("Failed to read cost ledger")

    return {"total_usd": total, "calls": calls, "by_feature": by_feature}

"""
Lexicomp / Lexidrug answers read off the admin's VMOS Cloud phone.

The Lexidrug app on the cloud phone is the source here, not openFDA. The
server drives that phone through VMOS Cloud's OpenAPI (vmos_client.py),
using plain ADB shell commands:

  1. launch the Lexidrug app,
  2. tap its search field, type the drug name, open the first result,
  3. dump the screen's text with `uiautomator dump`, swipe up, dump again,
     until the monograph stops changing (or a page cap is hit),
  4. stitch the text together and cache it on disk.

Questions are then answered by Claude using ONLY that monograph text, the
same "grounded, never from memory" contract drug_qa.py keeps for FDA
labels. The phone is the slow part (tens of seconds per drug), so a
monograph is fetched once and reused for every question about that drug
until the cache entry expires.

There is exactly one phone, and a phone can only show one screen, so every
fetch goes through a single lock: two users opening two drugs at once would
otherwise type into each other's search box.

Every UI detail that depends on the app's layout (package name, the id of
its search box, the label of a result row) is an environment setting, not
code, because the app's layout changes between versions and only the admin
can see the phone. /lexitest (admin_flow) runs one fetch and reports each
step, which is how those settings get tuned.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET

import cost_ledger
import vmos_client
from config import (
    CLAUDE_MODEL,
    LEXIDRUG_CACHE_TTL_SECONDS,
    LEXIDRUG_LAUNCH_WAIT_SECONDS,
    LEXIDRUG_MAX_SCROLLS,
    LEXIDRUG_PACKAGE,
    LEXIDRUG_RESULT_WAIT_SECONDS,
    LEXIDRUG_SEARCH_ID,
    STORAGE_DIR,
)
from pdf_processor import client  # the one shared Anthropic client

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(STORAGE_DIR, "_lexidrug_cache")
MAX_HISTORY_TURNS = 6
MAX_CONTEXT_CHARS = 120_000  # a long monograph is ~40-80k chars; this is a backstop

DISCLAIMER = (
    "⚠️ Read from the Lexidrug app for reference only. Verify against the full monograph and "
    "clinical judgment before acting on it."
)

_DUMP_PATH = "/sdcard/_lexi_dump.xml"
_device_lock = asyncio.Lock()


class LexidrugError(Exception):
    """Anything that should reach the user as a plain sentence."""


def is_enabled() -> bool:
    return vmos_client.is_configured()


# ---------------------------------------------------------------------
# Screen reading
# ---------------------------------------------------------------------

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def _parse_nodes(xml_text: str) -> list[dict]:
    """uiautomator XML -> flat list of {text, desc, id, cls, center}."""
    start = xml_text.find("<?xml")
    if start == -1:
        start = xml_text.find("<hierarchy")
    if start == -1:
        raise LexidrugError("Couldn't read the phone's screen (no UI dump came back).")
    end = xml_text.rfind("</hierarchy>")
    xml_text = xml_text[start : end + len("</hierarchy>")] if end != -1 else xml_text[start:]
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise LexidrugError(f"Couldn't read the phone's screen (bad UI dump: {e}).")

    nodes = []
    for el in root.iter("node"):
        m = _BOUNDS_RE.match(el.get("bounds", ""))
        center = None
        if m:
            x1, y1, x2, y2 = map(int, m.groups())
            center = ((x1 + x2) // 2, (y1 + y2) // 2)
        nodes.append(
            {
                "text": (el.get("text") or "").strip(),
                "desc": (el.get("content-desc") or "").strip(),
                "id": el.get("resource-id") or "",
                "cls": el.get("class") or "",
                "center": center,
                "y": center[1] if center else 0,
            }
        )
    return nodes


async def _dump_screen() -> list[dict]:
    # Attributes we never read are stripped on the phone, which roughly
    # halves what has to come back through the API.
    out = await vmos_client.shell(
        f"uiautomator dump {_DUMP_PATH} >/dev/null 2>&1; "
        f"sed -E 's/ (index|package|checkable|checked|clickable|enabled|focusable|focused|"
        f"""scrollable|long-clickable|password|selected)="[^"]*"//g' {_DUMP_PATH}"""
    )
    return _parse_nodes(out)


def _screen_lines(nodes: list[dict]) -> list[str]:
    """Visible text, top to bottom, de-duplicated within the screen."""
    lines, seen = [], set()
    for n in sorted(nodes, key=lambda n: n["y"]):
        for t in (n["text"], n["desc"]):
            if t and t not in seen:
                seen.add(t)
                lines.append(t)
    return lines


async def _tap(node: dict) -> None:
    x, y = node["center"]
    await vmos_client.shell(f"input tap {x} {y}")


def _shell_quote_text(text: str) -> str:
    # `input text` treats a space as an argument break; %s is its own escape
    # for a space. Everything that isn't a plain name character is dropped,
    # since this string goes straight onto a shell command line.
    cleaned = " ".join(re.sub(r"[^A-Za-z0-9 \-]", " ", text).split())
    return cleaned.replace(" ", "%s")


def _find_search_box(nodes: list[dict]) -> dict | None:
    if LEXIDRUG_SEARCH_ID:
        for n in nodes:
            if n["id"].endswith(LEXIDRUG_SEARCH_ID) and n["center"]:
                return n
    for n in nodes:
        if n["cls"].endswith("EditText") and n["center"]:
            return n
    for n in nodes:
        label = (n["desc"] or n["text"]).lower()
        if "search" in label and n["center"]:
            return n
    return None


def _find_result(nodes: list[dict], drug: str, typed_y: int) -> dict | None:
    """The result row best matching the drug: exact title first, then prefix."""
    want = drug.lower()
    below = [n for n in nodes if n["center"] and n["y"] > typed_y and n["text"]]
    for n in below:
        if n["text"].lower() == want:
            return n
    for n in below:
        if n["text"].lower().startswith(want):
            return n
    for n in below:
        if want in n["text"].lower():
            return n
    return None


async def _open_monograph(drug: str, log: list[str]) -> tuple[str, str]:
    """Drive the phone to the drug's monograph and read it. Returns (title, text)."""
    pkg = LEXIDRUG_PACKAGE
    await vmos_client.shell(f"am force-stop {pkg}; monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
    log.append(f"launched {pkg}")
    await asyncio.sleep(LEXIDRUG_LAUNCH_WAIT_SECONDS)

    nodes = await _dump_screen()
    box = _find_search_box(nodes)
    if not box:
        log.append("screen after launch: " + " | ".join(_screen_lines(nodes)[:25]))
        raise LexidrugError(
            "Lexidrug opened, but its search box wasn't found on screen. "
            "The app may be logged out or showing a pop-up."
        )
    await _tap(box)
    await asyncio.sleep(1)

    # Tapping a search icon often opens a new screen with the real field.
    nodes = await _dump_screen()
    field = next((n for n in nodes if n["cls"].endswith("EditText") and n["center"]), None) or box
    if field["center"] != box["center"]:
        await _tap(field)
    log.append(f"search field at {field['center']}")

    await vmos_client.shell(f"input text '{_shell_quote_text(drug)}'")
    log.append(f"typed {drug!r}")
    await asyncio.sleep(LEXIDRUG_RESULT_WAIT_SECONDS)

    nodes = await _dump_screen()
    result = _find_result(nodes, drug, field["y"])
    if not result:
        log.append("results screen: " + " | ".join(_screen_lines(nodes)[:25]))
        raise LexidrugError(f'Lexidrug has no result matching "{drug}". Check the spelling or try the generic name.')
    title = result["text"]
    await vmos_client.shell("input keyevent 111")  # ESC: hide the keyboard so it doesn't cover the tap
    await asyncio.sleep(0.5)
    nodes = await _dump_screen()
    result = _find_result(nodes, drug, 0) or result
    await _tap(result)
    log.append(f"opened {title!r}")
    await asyncio.sleep(LEXIDRUG_RESULT_WAIT_SECONDS)

    # Read: dump, swipe, repeat, keeping each new line once and in order.
    size = await vmos_client.shell("wm size")
    m = re.search(r"(\d+)x(\d+)", size or "")
    w, h = (int(m.group(1)), int(m.group(2))) if m else (720, 1280)
    x = w // 2
    y_from, y_to = int(h * 0.80), int(h * 0.25)

    collected, seen = [], set()
    stale = 0
    for page in range(LEXIDRUG_MAX_SCROLLS + 1):
        new = [ln for ln in _screen_lines(await _dump_screen()) if ln not in seen]
        for ln in new:
            seen.add(ln)
            collected.append(ln)
        stale = stale + 1 if not new else 0
        if stale >= 2:  # two swipes with nothing new: the end of the page
            break
        await vmos_client.shell(f"input swipe {x} {y_from} {x} {y_to} 350")
        await asyncio.sleep(0.6)
    log.append(f"read {len(collected)} lines over {page + 1} screens")

    text = "\n".join(collected)
    if len(text) < 200:
        raise LexidrugError(f"The {title} monograph opened, but almost no text could be read from it.")
    return title, text


# ---------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------

def _cache_path(drug: str) -> str:
    key = hashlib.sha1(drug.strip().lower().encode()).hexdigest()[:16]
    return os.path.join(CACHE_DIR, f"{key}.json")


def _cache_get(drug: str) -> dict | None:
    try:
        with open(_cache_path(drug), encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, ValueError):
        return None
    if time.time() - entry.get("fetched_at", 0) > LEXIDRUG_CACHE_TTL_SECONDS:
        return None
    return entry


def _cache_put(drug: str, title: str, text: str) -> dict:
    entry = {"query": drug, "title": title, "text": text, "fetched_at": time.time()}
    os.makedirs(CACHE_DIR, exist_ok=True)
    # Stored under both what was typed and the title Lexidrug opened, since
    # the mini app asks follow-ups using the title it was shown.
    for key in {drug.strip().lower(), title.strip().lower()}:
        path = _cache_path(key)
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(path + ".tmp", path)
    return entry


async def get_monograph(drug: str, log: list[str] | None = None, refresh: bool = False) -> dict:
    """{"title", "text", "fetched_at"} for a drug, from cache or the phone."""
    drug = drug.strip()
    if not drug:
        raise LexidrugError("Type a drug name first.")
    if not is_enabled():
        raise LexidrugError("Lexicomp isn't connected right now. Please use Drug LookUp instead.")
    log = log if log is not None else []
    if not refresh:
        cached = _cache_get(drug)
        if cached:
            log.append("served from cache")
            return cached
    async with _device_lock:
        # Someone else may have fetched it while we waited for the phone.
        if not refresh:
            cached = _cache_get(drug)
            if cached:
                log.append("served from cache (fetched while waiting)")
                return cached
        try:
            title, text = await _open_monograph(drug, log)
        except vmos_client.VmosError as e:
            logger.warning("VMOS call failed during Lexidrug fetch: %s", e)
            raise LexidrugError(f"The cloud phone didn't respond properly ({e}). Please try again shortly.")
    return _cache_put(drug, title, text)


# ---------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------

def answer_question(
    title: str, monograph: str, question: str, history: list[dict] | None = None, language: str = "English"
) -> str:
    """Synchronous -- run via asyncio.to_thread."""
    history = (history or [])[-MAX_HISTORY_TURNS:]
    system_prompt = (
        f"You are answering questions about '{title}' using ONLY the Lexidrug (Lexicomp) monograph text "
        "below, which was read off the Lexidrug app's screen. Rules:\n"
        "1. Use only this text -- never your own memorized knowledge of the drug, even if confident.\n"
        "2. Name the monograph section your answer comes from, e.g. '(Lexidrug: Dosing: Renal "
        "Impairment)'.\n"
        "3. If the text doesn't cover the question, say so plainly. The text was captured by scrolling "
        "the screen, so a section may be collapsed or missing -- say that rather than guessing.\n"
        "4. Quote doses exactly as written. Never compute a patient-specific dose yourself.\n"
        "5. Be concise -- this is read on a phone.\n"
        f"6. Respond in {language}.\n"
        f"\nLEXIDRUG MONOGRAPH: {title.upper()}\n{monograph[:MAX_CONTEXT_CHARS]}"
    )
    messages = []
    for turn in history:
        messages.append({"role": "user", "content": turn["question"]})
        messages.append({"role": "assistant", "content": turn["answer"]})
    messages.append({"role": "user", "content": question})

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=1200, thinking={"type": "disabled"}, system=system_prompt, messages=messages
        )
    except Exception:
        logger.exception("AI request failed")
        raise LexidrugError("The AI service didn't respond properly. Please try again in a moment.")
    try:
        cost_ledger.record_claude_response("lexidrug_qa", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    return text + "\n\n" + DISCLAIMER

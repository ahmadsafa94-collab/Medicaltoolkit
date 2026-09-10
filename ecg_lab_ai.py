"""
AI-based ECG and lab-value interpretation -- INTERPRETIVE/EDUCATIONAL only,
never diagnostic. See the delivered roadmap doc for the full design
rationale; this module is the implementation of that design.

Both functions here are gated behind subscriptions.check_and_consume_trial()
by their callers (bot.py's handlers) BEFORE this module is ever invoked --
this module itself has no concept of free/premium, it just does the AI
interpretation once a caller has already confirmed the user is allowed to.

Safety framing, enforced structurally rather than just requested nicely:
  - Every system prompt explicitly forbids a diagnosis or a management
    recommendation -- only a structured, descriptive read of what's shown.
  - _DISCLAIMER is appended to EVERY response, every time, not just once at
    onboarding -- it needs to be visible at the moment of use.
  - Lab interpretation is anchored to this bot's own glossary.py reference
    ranges (handed to Claude as authoritative context, with instructions to
    use ONLY those, not its own memorized ranges) so normal/abnormal
    framing is consistent with the rest of the bot, not a second,
    potentially-divergent source of "normal" numbers.

Two-pass "draft then verify" pipeline: every interpretation below is
actually TWO Claude calls, not one. The first produces a draft; the second
is a fresh, independent look at the SAME input (image or values) that
checks the draft for errors and returns a corrected final version -- this
is the "checked with AI to ensure correct answers" pass. It's also where
a specific, common failure mode gets fixed: a single-pass vision call tends
to hedge defensively and claim an image is "unclear/unreadable" even when
it's perfectly legible, because the ORIGINAL prompt below explicitly tells
it to flag unclear images and an LLM asked to watch for a failure mode will
over-report it. The verify pass is explicitly instructed to re-examine that
specific claim and drop it unless the image is genuinely illegible, rather
than rubber-stamping the draft's hedge. This doubles the Claude cost of
every ECG/lab interpretation call -- an accepted tradeoff for the accuracy
and false-"unclear" fixes this was asked for.
"""

import base64
import logging

import cost_ledger
import glossary
from config import CLAUDE_MODEL
from pdf_processor import client  # reuse the one Anthropic client instance

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_DISCLAIMER = (
    "\n\n⚠️ Interpretive/educational only -- NOT a diagnosis. A real tracing or lab result must be read by "
    "the treating clinician using the full clinical picture, which this bot never has. If this is a real "
    "patient, use it only alongside -- never instead of -- clinical judgment and a qualified reviewer."
)

# Shared across both ECG and lab-image verify prompts: the specific
# instruction that reins in over-cautious "image unclear" hedging.
_IMAGE_CLARITY_REVIEW_INSTRUCTION = (
    "If the draft claims the image is unclear, low-quality, cropped, or otherwise unreadable, look again and "
    "judge that claim specifically on its own merits: only agree it's unreadable if you genuinely cannot make "
    "out the content at all (e.g. severe blur across the whole image, missing/illegible key values, most of "
    "it cropped out of frame). A phone photo taken at a slight angle, mild glare, a photo of a printed page or "
    "a screen, or a plain/busy background is NOT by itself a reason to call it unclear -- if you can actually "
    "read the values from it, do so and drop the unclear caveat entirely instead of repeating it defensively."
)


class InterpretationError(Exception):
    pass


def _image_block(image_bytes: bytes, media_type: str) -> dict:
    if media_type not in ALLOWED_IMAGE_MEDIA_TYPES:
        raise InterpretationError(f"Unsupported image type '{media_type}'. Please send a JPEG, PNG, or WEBP photo.")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(image_bytes).decode("ascii")},
    }


def _call_claude(feature: str, system_prompt: str, content, max_tokens: int = 1200) -> str:
    """Shared single-Claude-call plumbing (client call + cost logging + text extraction) for every pass below."""
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        raise InterpretationError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response(feature, response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    return "".join(block.text for block in response.content if block.type == "text").strip()


def interpret_ecg(image_bytes: bytes, media_type: str, language: str = "English") -> str:
    """
    Synchronous -- run via asyncio.to_thread from an async handler.
    image_bytes: raw bytes of a photographed/scanned ECG tracing.
    Two Claude calls: a draft read, then an independent verify pass over the
    same image (see module docstring) before the disclaimer is appended.
    """
    draft_system_prompt = (
        "You are helping a medical student practice ECG interpretation as a STUDY EXERCISE, not a clinical "
        "read for patient care. Look at the ECG image and describe what it shows using ALWAYS this exact "
        "structure, one line per item:\n"
        "Rate: <value or estimate, beats/min>\n"
        "Rhythm: <regular/irregular; P-wave presence and morphology>\n"
        "Axis: <normal / left deviation / right deviation, estimated>\n"
        "Intervals: <PR, QRS, QT -- and QTc if a rate is determinable>\n"
        "Notable morphology: <ST segment, T waves, Q waves, voltage -- each phrased as 'within normal limits' "
        "or 'notable, commonly seen in ___', citing the general category of condition, never a specific "
        "patient diagnosis>\n"
        "Overall impression: <a plain-language summary of the PATTERN only, e.g. 'sinus rhythm with normal "
        "intervals' or 'ST elevation pattern in the anterior leads, commonly associated with anterior wall "
        "ischemia/infarction as a category' -- NEVER state or imply this specific image IS a diagnosis like "
        "'this is a STEMI' or 'this patient has X'.>\n\n"
        "Only say the image is too low-quality, cropped, or unclear to read reliably if you genuinely cannot "
        "make out the waveform at all -- a phone photo at an angle, mild glare, or an ordinary background is "
        "still readable and does NOT warrant that caveat. If it's truly unreadable, say plainly which parts "
        f"are unreadable instead of guessing at values you can't see. Respond in {language}."
    )
    draft = _call_claude(
        "ecg_interpretation",
        draft_system_prompt,
        [
            _image_block(image_bytes, media_type),
            {"type": "text", "text": "Interpret this ECG tracing for study purposes."},
        ],
    )

    verify_system_prompt = (
        "You are the SECOND, independent reviewer checking a draft ECG interpretation against the actual "
        "image, as a quality check before it's shown to a medical student. You will see the same ECG image "
        "plus the draft interpretation below. Your job:\n"
        "1. Re-examine the image yourself and check the draft's Rate/Rhythm/Axis/Intervals/Notable morphology/"
        "Overall impression against what the image actually shows. Correct anything wrong; keep anything "
        "already correct.\n"
        f"2. {_IMAGE_CLARITY_REVIEW_INSTRUCTION}\n"
        "3. Output ONLY the corrected final interpretation, in the exact same six-line structure as the draft "
        "(Rate/Rhythm/Axis/Intervals/Notable morphology/Overall impression) -- do not mention that you are "
        "reviewing or show your reasoning, just the corrected final text a student should read.\n"
        "4. Same safety rules as the draft: never state or imply a specific diagnosis for this image, only "
        f"describe the pattern. Respond in {language}.\n\n"
        f"DRAFT INTERPRETATION TO CHECK:\n{draft}"
    )
    verified = _call_claude(
        "ecg_interpretation_verify",
        verify_system_prompt,
        [
            _image_block(image_bytes, media_type),
            {"type": "text", "text": "Here is the same ECG image again. Verify/correct the draft per your instructions."},
        ],
    )

    return (verified or draft) + _DISCLAIMER


def _lab_reference_context() -> str:
    """
    The bot's own curated lab reference ranges (glossary.py's "lab" category)
    handed to Claude as the ONLY source of truth for normal/abnormal cutoffs
    -- keeps lab interpretation consistent with /glossary instead of a second,
    independently-recalled set of "normal" numbers.
    """
    lines = [f"{term}: {definition}" for term, category, definition in glossary._ENTRIES if category == "lab"]
    return "\n".join(lines)


def _lab_system_prompt(language: str) -> str:
    return (
        "You are helping a medical student practice interpreting lab results as a STUDY EXERCISE, not a "
        "clinical read for patient care. Below is this bot's own reference-range list -- use ONLY these "
        "ranges to judge whether a value is high/low/normal, never your own memorized reference ranges (labs "
        "vary by assay/lab/population, and consistency with this bot's own /glossary matters more than a "
        "marginally different textbook number).\n\n"
        f"REFERENCE RANGES:\n{_lab_reference_context()}\n\n"
        "For each value the user gives you: state whether it's high/low/normal against the reference ranges "
        "above (if a value isn't in the list, say so and skip judging it rather than guessing a range), then "
        "1-2 sentences on general categories of causes commonly associated with that abnormality (e.g. "
        "'a low potassium like this is commonly seen with diuretic use, GI losses, or...') -- general "
        "educational categories only, NEVER a diagnosis for this specific patient/case. If several values "
        "together suggest a well-known pattern worth knowing (e.g. a classic electrolyte pattern), you may "
        "name that pattern as a teaching point, but always frame it as 'a pattern commonly discussed as ___', "
        f"never as this case's actual diagnosis. Respond in {language}."
    )


def interpret_lab_text(values_text: str, language: str = "English") -> str:
    """
    Synchronous -- run via asyncio.to_thread from an async handler.
    values_text: free-typed lab values, e.g. 'Na 148, K 2.9, Cr 1.8'.
    Two Claude calls: a draft read, then an independent verify pass over the
    same values (see module docstring) before the disclaimer is appended.
    """
    if not values_text.strip():
        raise InterpretationError("Please send the lab values as text, e.g. 'Na 148, K 2.9, Cr 1.8'.")

    values_text = values_text.strip()[:2000]
    draft = _call_claude("lab_interpretation", _lab_system_prompt(language), values_text)

    verify_system_prompt = (
        "You are the SECOND, independent reviewer checking a draft lab-value interpretation, as a quality "
        "check before it's shown to a medical student. Below is this bot's own reference-range list (the "
        "same one the draft was told to use), the original values the user gave, and the draft "
        "interpretation. Your job:\n"
        "1. Re-check the draft's high/low/normal calls against the reference ranges yourself, and re-check "
        "its stated causes/patterns for accuracy. Correct anything wrong; keep anything already correct.\n"
        "2. Output ONLY the corrected final interpretation, in the same style/structure as the draft -- do "
        "not mention that you are reviewing or show your reasoning, just the corrected final text.\n"
        "3. Same safety rules as the draft: general educational categories only, never a diagnosis for this "
        f"specific case. Respond in {language}.\n\n"
        f"REFERENCE RANGES:\n{_lab_reference_context()}\n\n"
        f"ORIGINAL VALUES FROM USER:\n{values_text}\n\n"
        f"DRAFT INTERPRETATION TO CHECK:\n{draft}"
    )
    verified = _call_claude("lab_interpretation_verify", verify_system_prompt, "Verify/correct the draft per your instructions.")

    return (verified or draft) + _DISCLAIMER


def interpret_lab_image(image_bytes: bytes, media_type: str, language: str = "English") -> str:
    """
    Synchronous -- run via asyncio.to_thread from an async handler.
    image_bytes: a photo of a lab report.
    Two Claude calls: a draft read, then an independent verify pass over the
    same image (see module docstring) before the disclaimer is appended.
    """
    draft = _call_claude(
        "lab_interpretation",
        _lab_system_prompt(language),
        [
            _image_block(image_bytes, media_type),
            {"type": "text", "text": "Read the lab values in this image and interpret them for study purposes."},
        ],
    )

    verify_system_prompt = (
        "You are the SECOND, independent reviewer checking a draft lab-report interpretation against the "
        "actual image, as a quality check before it's shown to a medical student. You will see the same "
        "image plus the draft interpretation below. Your job:\n"
        "1. Re-read the values in the image yourself and check the draft's transcription and its high/low/"
        "normal calls against the reference ranges below. Correct anything wrong; keep anything already "
        "correct.\n"
        f"2. {_IMAGE_CLARITY_REVIEW_INSTRUCTION}\n"
        "3. Output ONLY the corrected final interpretation, in the same style/structure as the draft -- do "
        "not mention that you are reviewing or show your reasoning, just the corrected final text.\n"
        "4. Same safety rules as the draft: general educational categories only, never a diagnosis for this "
        f"specific case. Respond in {language}.\n\n"
        f"REFERENCE RANGES:\n{_lab_reference_context()}\n\n"
        f"DRAFT INTERPRETATION TO CHECK:\n{draft}"
    )
    verified = _call_claude(
        "lab_interpretation_verify",
        verify_system_prompt,
        [
            _image_block(image_bytes, media_type),
            {"type": "text", "text": "Here is the same lab report image again. Verify/correct the draft per your instructions."},
        ],
    )

    return (verified or draft) + _DISCLAIMER

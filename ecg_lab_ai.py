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


class InterpretationError(Exception):
    pass


def _image_block(image_bytes: bytes, media_type: str) -> dict:
    if media_type not in ALLOWED_IMAGE_MEDIA_TYPES:
        raise InterpretationError(f"Unsupported image type '{media_type}'. Please send a JPEG, PNG, or WEBP photo.")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(image_bytes).decode("ascii")},
    }


def interpret_ecg(image_bytes: bytes, media_type: str, language: str = "English") -> str:
    """
    Synchronous -- run via asyncio.to_thread from an async handler.
    image_bytes: raw bytes of a photographed/scanned ECG tracing.
    """
    system_prompt = (
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
        "If the image is too low-quality, cropped, or unclear to read reliably, say so plainly (which parts "
        "are unreadable) and ask for a clearer photo instead of guessing at values you can't actually see. "
        f"Respond in {language}."
    )
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        _image_block(image_bytes, media_type),
                        {"type": "text", "text": "Interpret this ECG tracing for study purposes."},
                    ],
                }
            ],
        )
    except InterpretationError:
        raise
    except Exception as e:
        raise InterpretationError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response("ecg_interpretation", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text + _DISCLAIMER


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
    """Synchronous -- run via asyncio.to_thread from an async handler. values_text: free-typed lab values, e.g. 'Na 148, K 2.9, Cr 1.8'."""
    if not values_text.strip():
        raise InterpretationError("Please send the lab values as text, e.g. 'Na 148, K 2.9, Cr 1.8'.")

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            system=_lab_system_prompt(language),
            messages=[{"role": "user", "content": values_text.strip()[:2000]}],
        )
    except Exception as e:
        raise InterpretationError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response("lab_interpretation", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text + _DISCLAIMER


def interpret_lab_image(image_bytes: bytes, media_type: str, language: str = "English") -> str:
    """Synchronous -- run via asyncio.to_thread from an async handler. image_bytes: a photo of a lab report."""
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1200,
            system=_lab_system_prompt(language),
            messages=[
                {
                    "role": "user",
                    "content": [
                        _image_block(image_bytes, media_type),
                        {"type": "text", "text": "Read the lab values in this image and interpret them for study purposes."},
                    ],
                }
            ],
        )
    except InterpretationError:
        raise
    except Exception as e:
        raise InterpretationError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response("lab_interpretation", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text + _DISCLAIMER

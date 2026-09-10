"""
AI logic for the radiology/histology image quiz (Study Tools -> 🩻 Image Quiz).
Reuses the same Claude vision call shape as ecg_lab_ai.py, but produces a
self-test quiz about an uploaded image rather than a direct interpretation --
useful for practicing image recognition against textbook/practice figures.

Unlike chapter_ai.quiz_chapter (which is grounded in real textbook text the
student already has), the "correct" answers here come from Claude's own
reading of the image, not a verified source -- flagged clearly in the output
so it's used the way an AI-generated study aid should be: a prompt for
further study, not an authoritative answer key.
"""

import logging

import cost_ledger
from config import CLAUDE_MODEL
from ecg_lab_ai import ALLOWED_IMAGE_MEDIA_TYPES, InterpretationError, _image_block
from pdf_processor import client

logger = logging.getLogger(__name__)

_DISCLAIMER = (
    "\n\n⚠️ AI-generated from a single image, not a verified answer key -- cross-check against a real "
    "annotated source (textbook, atlas, instructor) before treating any answer here as authoritative."
)


class ImageQuizError(Exception):
    pass


def generate_image_quiz(image_bytes: bytes, media_type: str, num_questions: int = 3, language: str = "English") -> str:
    """Synchronous -- run via asyncio.to_thread from an async handler. Returns plain formatted quiz text (same style as chapter_ai.quiz_chapter)."""
    system_prompt = (
        "You are writing a self-test quiz for a medical student practicing radiology/histology image "
        f"recognition. Look at the image and write exactly {num_questions} multiple-choice questions "
        "(4 options: A-D) testing recognition of what's shown -- the modality/stain, the structures visible, "
        "and the most likely finding(s). After all the questions, include an 'Answers' section listing the "
        "correct letter and a one-sentence explanation referencing specific features visible in the image. "
        "If the image is too unclear to confidently base questions on, say so instead of guessing. "
        f"Use plain text only (no markdown headers, '-' for any bullets). Respond in {language}."
    )
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=2000,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        _image_block(image_bytes, media_type),
                        {"type": "text", "text": "Generate the quiz for this image."},
                    ],
                }
            ],
        )
    except InterpretationError as e:
        raise ImageQuizError(str(e))
    except Exception as e:
        raise ImageQuizError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response("image_quiz", response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    return text + _DISCLAIMER

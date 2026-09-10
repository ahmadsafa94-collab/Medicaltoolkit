"""
AI logic for OSCE-style case practice: Claude plays a fictional patient, the
student asks history/exam questions one at a time, and a debrief at the end
reveals the intended diagnosis with brief feedback. Clearly framed throughout
as educational role-play -- NEVER a real-patient tool, and the system prompt
explicitly forbids Claude from breaking character to give real medical advice.

Kept separate from osce_flow.py (the Telegram plumbing) the same way
chapter_ai.py/quiz_ai.py/pdf_qa.py/ecg_lab_ai.py are all separated from
their *_flow.py callers in this codebase.
"""

import logging

import cost_ledger
from config import CLAUDE_MODEL
from pdf_processor import client

logger = logging.getLogger(__name__)

MAX_TURNS = 20  # a case that runs this long is capped rather than growing the prompt unboundedly

_SYSTEM_PROMPT_TEMPLATE = (
    "You are role-playing a FICTIONAL patient for a medical student's OSCE (clinical skills) practice "
    "session. This is educational role-play only -- NOT a real patient, NOT a diagnostic tool, and NOT "
    "medical advice for anyone's real health. Stay in character as a patient presenting with: {topic}. "
    "Answer the student's questions in first person, plain language (not medical jargon), and only reveal "
    "information they actually ask about -- do not volunteer your full history unprompted, the way a real "
    "patient wouldn't either. Invent plausible, internally consistent details (age, history, symptoms, vitals "
    "if asked) once, then stay consistent with them for the rest of the conversation. Never reveal the "
    "intended diagnosis, never break character, and never refer to yourself as an AI during the case -- if "
    "asked something a patient wouldn't know ('what's my diagnosis?'), answer as a patient would ('I don't "
    "know, that's why I'm here'). If the student's message looks like a real emergency framed as real (not "
    "part of the fictional case), gently break character just long enough to say this is a practice exercise "
    "and they should seek real emergency care if this is genuinely happening to them, then stop. "
    "Respond in {language}."
)

_DEBRIEF_SYSTEM_PROMPT_TEMPLATE = (
    "The fictional OSCE practice case (patient presenting with: {topic}) is now over. Step OUT of character. "
    "State the intended diagnosis for the case you were just role-playing, then give the medical student "
    "brief, constructive feedback based on the conversation: what history/exam questions they covered well, "
    "and 2-3 important things they should have asked but didn't (if any). This is educational feedback on a "
    "FICTIONAL practice case only. Respond in {language}."
)


class OsceError(Exception):
    pass


def _call(system_prompt: str, messages: list[dict], feature: str, max_tokens: int = 500) -> str:
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=max_tokens, system=system_prompt, messages=messages
        )
    except Exception as e:
        raise OsceError(f"Claude request failed: {e}")

    try:
        cost_ledger.record_claude_response(feature, response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    return "".join(block.text for block in response.content if block.type == "text").strip()


def start_case(topic: str, language: str = "English") -> str:
    """Returns the patient's opening statement -- the first thing shown to the student."""
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(topic=topic, language=language)
    messages = [{"role": "user", "content": "(Begin the encounter now with your opening statement to the doctor.)"}]
    return _call(system_prompt, messages, feature="osce")


def continue_case(topic: str, history: list[dict], student_message: str, language: str = "English") -> str:
    """history: prior [{"role": "user"|"assistant", "content": str}, ...] turns, oldest first (NOT including student_message)."""
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(topic=topic, language=language)
    messages = list(history[-MAX_TURNS:]) + [{"role": "user", "content": student_message}]
    return _call(system_prompt, messages, feature="osce")


def debrief_case(topic: str, history: list[dict], language: str = "English") -> str:
    system_prompt = _DEBRIEF_SYSTEM_PROMPT_TEMPLATE.format(topic=topic, language=language)
    messages = list(history[-MAX_TURNS:]) + [{"role": "user", "content": "(End the case now and give the debrief.)"}]
    return _call(system_prompt, messages, feature="osce", max_tokens=700)

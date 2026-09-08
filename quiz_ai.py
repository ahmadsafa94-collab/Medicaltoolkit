"""
AI-generated multiple-choice quiz for the Book Shelf mini app: the user
picks which chapters to draw from, a difficulty level, and how many
questions (up to 30), and Claude returns STRUCTURED JSON so the mini app
can render an interactive quiz (pick an answer, see immediately whether
it's right, read the explanation) rather than a wall of free text like
chapter_ai.quiz_chapter's chat-based version produces.

Validation follows the same strict pattern as pdf_processor.detect_chapters:
Claude's JSON is checked field-by-field before use, since a malformed or
out-of-range entry here would otherwise surface as a confusing crash deep
in the frontend instead of a clear "couldn't generate the quiz" message.
"""

import json
import logging
import re

from config import CLAUDE_MODEL
from pdf_processor import client  # reuse the shared Anthropic client instance
from chapter_ai import extract_text_for_page_range, ChapterAIError, MAX_CHARS_PER_CHAPTER

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 30
VALID_DIFFICULTIES = {"easy", "medium", "difficult"}

# Total text budget across ALL selected chapters combined, regardless of how
# many are picked -- keeps "select every chapter of a 1500-page book" from
# turning into one enormous prompt. Split evenly across the selected
# chapters below.
MAX_TOTAL_QUIZ_CONTEXT_CHARS = 100_000

_DIFFICULTY_GUIDANCE = {
    "easy": "Focus on definitions, basic facts, and recall -- straightforward, unambiguous questions with "
            "clearly wrong distractors.",
    "medium": "Mix recall with applied understanding -- some questions should require connecting two related "
              "facts, not just recalling one in isolation.",
    "difficult": "Favor clinical application and distinguishing between similar/confusable concepts -- wrong "
                 "options should be genuinely plausible distractors, not obviously wrong.",
}


class QuizError(Exception):
    pass


def generate_quiz(
    book_title: str, pdf_path: str, chapters: list[dict], difficulty: str, num_questions: int
) -> list[dict]:
    """
    Synchronous -- run via asyncio.to_thread from an async handler.

    chapters: the SELECTED subset of the book's {"title","start_page","end_page"}
    entries (see library.set_chapters) to draw questions from.

    Returns a list of:
        {"question": str, "options": [str, str, str, str], "correct_index": int, "explanation": str}
    """
    if difficulty not in VALID_DIFFICULTIES:
        raise QuizError(f"Invalid difficulty '{difficulty}' -- must be one of {sorted(VALID_DIFFICULTIES)}.")
    if not isinstance(num_questions, int) or isinstance(num_questions, bool) or not (1 <= num_questions <= MAX_QUESTIONS):
        raise QuizError(f"Number of questions must be a whole number between 1 and {MAX_QUESTIONS}.")
    if not chapters:
        raise QuizError("Select at least one chapter to quiz from.")

    parts = []
    remaining = MAX_TOTAL_QUIZ_CONTEXT_CHARS
    per_chapter_budget = max(2000, MAX_TOTAL_QUIZ_CONTEXT_CHARS // len(chapters))
    for chapter in chapters:
        if remaining <= 0:
            break
        budget = min(per_chapter_budget, remaining, MAX_CHARS_PER_CHAPTER)
        try:
            text, _truncated = extract_text_for_page_range(
                pdf_path, chapter["start_page"], chapter["end_page"], max_chars=budget
            )
        except ChapterAIError as e:
            logger.warning("Skipping chapter '%s' in quiz generation: %s", chapter["title"], e)
            continue
        parts.append(f"[Chapter: {chapter['title']}]\n{text}")
        remaining -= len(text)

    if not parts:
        raise QuizError("Couldn't extract readable text from the selected chapters.")

    context = "\n\n".join(parts)
    difficulty_guidance = _DIFFICULTY_GUIDANCE[difficulty]

    system_prompt = (
        f"You are writing a {difficulty}-difficulty multiple-choice quiz for a medical student, covering the "
        f"book '{book_title}', based ONLY on the excerpts provided below -- do not introduce outside facts. "
        f"{difficulty_guidance} "
        f"Write EXACTLY {num_questions} questions. "
        "Respond with ONLY a JSON array, no markdown fences, no preamble. Format: "
        '[{"question": "...", "options": ["...", "...", "...", "..."], "correct_index": 0, '
        '"explanation": "..."}, ...] '
        "options must have exactly 4 entries, correct_index is a 0-based index into options, and explanation "
        "is a one-sentence justification referencing the source material."
    )

    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=min(300 * num_questions + 500, 8000),
            system=system_prompt,
            messages=[{"role": "user", "content": context}],
        )
    except Exception as e:
        raise QuizError(f"Claude request failed: {e}")

    raw = "".join(block.text for block in response.content if block.type == "text").strip()
    raw = re.sub(r"^```json|```$", "", raw.strip(), flags=re.MULTILINE).strip()

    try:
        questions = json.loads(raw)
    except json.JSONDecodeError as e:
        raise QuizError(f"Could not parse Claude's response as JSON: {e}\nRaw: {raw[:500]}")

    if not isinstance(questions, list) or not questions:
        raise QuizError("Claude returned no questions.")

    validated = []
    for q in questions:
        if not isinstance(q, dict) or "question" not in q or "options" not in q or "correct_index" not in q:
            raise QuizError(f"Malformed question entry: {q}")
        options = q["options"]
        if not isinstance(options, list) or len(options) != 4:
            raise QuizError(f"Question does not have exactly 4 options: {q}")
        ci = q["correct_index"]
        if isinstance(ci, bool) or not isinstance(ci, int) or not (0 <= ci < 4):
            raise QuizError(f"Invalid correct_index in question: {q}")
        validated.append(
            {
                "question": str(q["question"]),
                "options": [str(o) for o in options],
                "correct_index": ci,
                "explanation": str(q.get("explanation", "")),
            }
        )

    return validated

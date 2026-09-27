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
  - ECG interpretation is anchored the same way where the admin has
    uploaded ECG textbooks: the verify pass below is given the passages
    from those books that match this specific tracing, and told to prefer
    their criteria and terminology over its own recollection. See
    ecg_reference.py. With no books loaded, the read falls back to exactly
    the unreferenced behavior it had before.

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
import ecg_qtc
import ecg_reference
import glossary
import ui_text
from config import CLAUDE_MODEL
from pdf_processor import client  # reuse the one Anthropic client instance

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_DISCLAIMER = (
    "\n\n⚠️ Interpretive/educational only -- NOT a diagnosis. A real tracing or lab result must be read by "
    "the treating clinician using the full clinical picture, which this bot never has. If this is a real "
    "patient, use it only alongside -- never instead of -- clinical judgment and a qualified reviewer."
)

# Every value is measured from the WAVEFORM, never read off the machine's
# printout. A study tool that transcribes the header isn't interpreting the
# ECG -- it's copying someone else's answer, teaches the student nothing,
# and inherits the machine's own errors (automated readings are routinely
# wrong on axis, on QT, and on any rhythm that isn't plain sinus). So the
# printed measurements and the machine's interpretation line are explicitly
# off limits, and the methods below are what produce the numbers. The
# calibration markers are the one exception and are not a result: they are
# the scale of the graph, and without them no measurement means anything.
# The sanity check is here because the classic failure is being out by a
# clean factor (counting every other R wave, or misreading paper speed),
# which a glance at R-R spacing catches.
_MEASUREMENT_METHOD_INSTRUCTION = (
    "MEASURED VALUES -- derive every one of these from the waveform yourself.\n\n"
    "IGNORE ANYTHING THE MACHINE PRINTED. Most ECGs print computed values along the top or side "
    "(rate/HR/'Vent. rate', PR, QRS/QRSD, QT/QTc, the P-QRS-T axes) and often a machine "
    "interpretation line as well. Do NOT read, use, quote or be influenced by any of them, and do "
    "not mention that they exist. They are frequently wrong -- especially for axis, QT, and any "
    "rhythm other than plain sinus -- and the point of this exercise is to measure the tracing, not "
    "to copy a printout. Every number you give must come from your own measurement of the waveform.\n"
    "The ONE thing you may read from the page is the CALIBRATION: paper speed (standard 25 mm/s) and "
    "gain (standard 10 mm/mV), plus the calibration pulse if shown. Those are the scale of the grid, "
    "not a result. If the speed or gain is non-standard, scale your measurements accordingly and say "
    "so; if calibration isn't visible, assume 25 mm/s and 10 mm/mV and state that assumption.\n\n"
    "RATE: at 25 mm/s one large (5 mm) box is 0.20 s, so rate = 300 / (large boxes between two "
    "consecutive R waves), or 1500 / (small 1 mm boxes). Use several consecutive R-R intervals, not "
    "one. If the rhythm is IRREGULAR, box-counting between one pair of beats is wrong: count the QRS "
    "complexes across the full 10-second recording, multiply by 6, and report it as an average.\n\n"
    "INTERVALS: at 25 mm/s one small box = 0.04 s (40 ms), one large box = 0.20 s (200 ms). Measure "
    "in the lead where the onset and offset are clearest, then confirm in a second lead.\n"
    "- PR: start of P to start of QRS. Normal 120-200 ms (3-5 small boxes).\n"
    "- QRS: onset to offset of QRS. Normal under 120 ms (under 3 small boxes).\n"
    "- QT: measure it from the start of QRS to the end of T, in the lead where the T-wave end is "
    "clearest (often II or V5), and report it in ms on the Intervals line.\n"
    "  Do NOT compute QTc yourself and do NOT write a QTc value you worked out -- square roots and "
    "cube roots done in your head are exactly what goes wrong, and this app calculates QTc for you "
    "from the two numbers you measured. Instead, end your whole reply with this marker on its own "
    "final line, using your measured QT in milliseconds and your measured rate in beats per minute:\n"
    "    <<QTDATA qt_ms=NNN rate_bpm=NNN>>\n"
    "  Plain ASCII, exactly that format, digits only, whatever language the rest of your answer is "
    "in. It is stripped out before the reader sees anything, and the QTc is inserted in its place. "
    "If the T-wave end or the rate genuinely cannot be read, omit the marker entirely and say so on "
    "the Intervals line -- never guess numbers just to fill it in.\n\n"
    "AXIS: use the quadrant method on the NET deflection (positive minus negative "
    "area) of leads I and aVF, judging the whole complex rather than the tallest spike:\n"
    "- I positive, aVF positive -> normal axis\n"
    "- I positive, aVF negative -> left axis deviation (confirm with II: also negative supports it)\n"
    "- I negative, aVF positive -> right axis deviation\n"
    "- I negative, aVF negative -> extreme/northwest axis\n"
    "Normal is about -30 to +90 degrees.\n\n"
    "ST-SEGMENT DEVIATION: measure at the J point, relative to the TP (or PR) baseline, and state it "
    "in mm with the leads it appears in -- not as a general impression.\n\n"
    "SANITY-CHECK EVERY VALUE AGAINST THE TRACING BEFORE WRITING IT. R waves about 5 large boxes "
    "apart is roughly 60/min, 3 apart roughly 100/min. A QRS you call narrow must look narrow "
    "(under 3 small boxes); a PR you call normal must be 3-5 small boxes wide; an axis you call "
    "normal must have both I and aVF upright. If a value contradicts what the tracing looks like, "
    "you have likely misread the paper speed, the calibration or the lead -- redo it rather than "
    "reporting it. State each value as a number, or say plainly that it cannot be measured from this "
    "image; never guess.\n\n"
    "Report each value plainly, as your own measurement. Do not annotate it with where it came from "
    "-- no '(printed)', '(printed header)', '(from the machine)' or similar."
)

# Retrieved alongside the tracing's own findings so the books' METHOD for
# each measured value is in front of the verify pass, not just passages
# about whatever pattern this tracing happens to show. Separate queries
# rather than one combined one, and separate from the draft text, because a
# single query lets the strongest topic crowd the others out -- see
# ecg_reference.build_reference.
_METHOD_QUERIES = [
    "determining heart rate from an ECG, 300 rule, 1500 rule, large squares between R waves, "
    "paper speed 25 mm/s, 10 second rule for an irregular rhythm",
    "measuring ECG intervals, PR interval, QRS duration, QT interval and QTc correction, "
    "Bazett formula, normal interval values in milliseconds",
    "determining the QRS axis, quadrant method with leads I and aVF, left axis deviation, "
    "right axis deviation, normal axis range in degrees",
]

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


def _output_budget(language: str) -> int:
    """
    Token budget for one interpretation pass.

    Non-Latin scripts tokenize far less efficiently than English -- Persian
    and Arabic run roughly two to three times the tokens for the same
    content -- so a budget that comfortably fits a six-line English read
    truncates the same read in Persian. That is not a theoretical concern:
    it cut a Persian ECG off mid-sentence, losing the QTc, the morphology
    line and the overall impression entirely, while looking like a
    complete answer.
    """
    return 2000 if language.strip().lower() == "english" else 4000


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
        raise InterpretationError(f"The AI request failed: {e}")

    try:
        cost_ledger.record_claude_response(feature, response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if getattr(response, "stop_reason", None) == "max_tokens":
        # Truncation is silent from the reader's side: the answer just
        # stops, mid-sentence, with the last lines missing but everything
        # above them looking right. Log it loudly so a budget that has
        # become too small shows up here instead of in a bug report.
        logger.error(
            "%s hit its token limit (%s tokens) and was TRUNCATED -- raise _output_budget",
            feature,
            getattr(getattr(response, "usage", None), "output_tokens", "?"),
        )
    if not text:
        # A pass that comes back with no text at all used to vanish silently:
        # interpret_ecg fell through to `verified or draft`, and when BOTH
        # were empty the user got a header, the sources and the disclaimer
        # wrapped around nothing. Log what the API actually returned so the
        # cause is identifiable from the admin error log rather than having
        # to guess; the caller turns a wholly-empty result into a real error.
        logger.error(
            "%s returned no text (stop_reason=%s, block types=%s, output_tokens=%s)",
            feature,
            getattr(response, "stop_reason", "?"),
            [getattr(b, "type", "?") for b in response.content],
            getattr(getattr(response, "usage", None), "output_tokens", "?"),
        )
    return text


def _require_text(verified: str, draft: str, what: str) -> str:
    """
    The final interpretation, or a real error if both passes came back
    empty. Without this the caller would happily send a heading, the
    sources and the disclaimer wrapped around an empty interpretation --
    which is what a user actually got: a card with nothing in it and no
    indication anything had gone wrong. _call_claude has already logged
    the API's stop_reason and block types by this point.
    """
    text = verified or draft
    if not text.strip():
        raise InterpretationError(
            f"The AI returned an empty {what}. This is usually temporary -- please try again, "
            "and if it keeps happening send a clearer photo of the tracing."
        )
    return text


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
        "Rate: <value in beats/min>\n"
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
        f"{_MEASUREMENT_METHOD_INSTRUCTION}\n\n"
        "Do the measuring silently. Output ONLY the six lines above -- no working, no box counts, no "
        "commentary before or after them. Never reply with nothing: if something genuinely cannot be "
        "measured, still emit all six lines and say so on the line it belongs to.\n"
        "ALL SIX LINES ARE REQUIRED, including the last two -- Notable morphology and Overall "
        "impression. Never stop after the intervals. If you are running long, shorten the earlier "
        "lines rather than dropping the later ones: the overall impression is the part the student "
        "reads first.\n\n"
        + (
            ""
            if language.strip().lower() == "english"
            else (
                f"Write the six line LABELS in {language} too, not just the values -- same six lines "
                "in the same order, translated. A line that starts in English and continues in "
                "another script renders out of order for the reader. Measurement abbreviations "
                "(PR, QRS, QT, QTc, ST, lead names like V1-V3) stay in Latin letters as they are "
                "written in every language.\n\n"
            )
        )
        + "Only say the image is too low-quality, cropped, or unclear to read reliably if you genuinely cannot "
        "make out the waveform at all -- a phone photo at an angle, mild glare, or an ordinary background is "
        "still readable and does NOT warrant that caveat. If it's truly unreadable, say plainly which parts "
        f"are unreadable instead of guessing at values you can't see. Respond in {language}."
    )
    # Both passes get the same, language-aware budget (see _output_budget):
    # a read truncated mid-structure is worse than a slightly costlier one,
    # and the VERIFY pass is the one the user actually sees.
    draft = _call_claude(
        "ecg_interpretation",
        draft_system_prompt,
        [
            _image_block(image_bytes, media_type),
            {"type": "text", "text": "Interpret this ECG tracing for study purposes."},
        ],
        max_tokens=_output_budget(language),
    )

    # The draft's TEXT is the retrieval query for the admin's ECG textbooks:
    # the image itself can't be embedded against text chunks, but the draft's
    # structured read (rate/rhythm/axis/intervals/morphology) describes the
    # tracing precisely enough to pull the passages that actually teach this
    # pattern. Empty when no reference books are loaded, in which case the
    # verify prompt below is exactly what it was before -- see ecg_reference.py.
    reference_block, reference_hits = ecg_reference.build_reference([draft, *_METHOD_QUERIES])

    instructions = [
        "Re-examine the image yourself and check the draft's Rate/Rhythm/Axis/Intervals/Notable morphology/"
        "Overall impression against what the image actually shows. Correct anything wrong; keep anything "
        "already correct.",
        # Called out separately from the general re-check above because
        # these are the lines users report as wrong, and a verify pass reads
        # much more like a rubber stamp when it is only told to "check".
        "Measure the RATE, the INTERVALS and the AXIS off the waveform yourself FIRST, before "
        "reading what the draft said about them, then compare. Work through this:\n"
        f"{_MEASUREMENT_METHOD_INSTRUCTION}\n"
        "Where your value and the draft's disagree, do not split the difference and do not defer to "
        "the draft: re-measure that one value and take whichever matches the waveform. If the draft "
        "quoted a value the tracing does not support -- or one that looks copied from the machine's "
        "printout rather than measured -- replace it with your own measurement.",
    ]
    if reference_block:
        instructions.append(
            "Check the draft against the TEACHING REFERENCES below -- excerpts from the ECG textbooks this "
            "bot is taught from, retrieved for this specific tracing. Where a reference gives a concrete "
            "criterion (a measurement cutoff, the defining features of a named pattern, a lead-by-lead "
            "rule), apply it exactly as written in preference to your own recollection, and prefer the "
            "book's own terminology and level of detail so the read sounds like the textbook a student is "
            "learning from. This applies to the RATE as much as to the pattern: if a reference gives a "
            "method for determining or checking the rate, follow that method rather than your own. "
            "Where the references don't cover something -- part of the pattern, or the rate method -- "
            "fall back to standard ECG knowledge rather than forcing an irrelevant reference in. Do NOT "
            "add citations, page numbers "
            "or book names inside the six lines themselves -- the student is shown the exact books and "
            "pages separately, under your output, so an inline citation would only duplicate that and "
            "break the structure."
        )
    instructions += [
        _IMAGE_CLARITY_REVIEW_INSTRUCTION,
        "Output ONLY the corrected final interpretation, in the exact same six-line structure as the draft "
        "(Rate/Rhythm/Axis/Intervals/Notable morphology/Overall impression) -- do not mention that you are "
        "reviewing or show your reasoning, just the corrected final text a student should read. Never "
        "reply with nothing: if the draft needed no changes at all, output it back unchanged rather "
        "than returning an empty message. ALL SIX LINES ARE REQUIRED: emit Notable morphology and "
        "Overall impression even if the draft omitted them. If the draft stops part-way through, "
        "finish it. End your reply with the <<QTDATA ...>> marker carrying YOUR measured QT and "
        "rate, exactly as specified above -- the app computes and inserts the QTc from it, so a "
        "missing marker means the reader gets no QTc at all.",
        "Same safety rules as the draft: never state or imply a specific diagnosis for this image, only "
        f"describe the pattern. Respond in {language}.",
    ]

    verify_system_prompt = (
        "You are the SECOND, independent reviewer checking a draft ECG interpretation against the actual "
        "image, as a quality check before it's shown to a medical student. You will see the same ECG image "
        "plus the draft interpretation below. Your job:\n"
        + "\n".join(f"{i}. {text}" for i, text in enumerate(instructions, start=1))
        + (f"\n\nTEACHING REFERENCES:\n{reference_block}" if reference_block else "")
        + f"\n\nDRAFT INTERPRETATION TO CHECK:\n{draft}"
    )
    verified = _call_claude(
        "ecg_interpretation_verify",
        verify_system_prompt,
        [
            _image_block(image_bytes, media_type),
            {"type": "text", "text": "Here is the same ECG image again. Verify/correct the draft per your instructions."},
        ],
        max_tokens=_output_budget(language),
    )

    # Sources sit between the read and the disclaimer: the student can see
    # which book and page backs what they were just told (and go look it
    # up), while the safety note stays last where it's most visible. Absent
    # entirely when no teaching book informed this read, rather than a
    # "no sources" line -- there is nothing to cite, and saying so every
    # time would just be noise on an otherwise unchanged read.
    sources = ecg_reference.format_sources(reference_hits)
    interpretation = _require_text(verified, draft, "ECG interpretation")

    # QTc is computed here, from the QT and rate the model measured, rather
    # than by the model -- see ecg_qtc. The marker is taken from the verify
    # pass where present (its measurements are the final ones), falling
    # back to the draft's, and is always stripped so it never reaches the
    # reader whether or not it parsed.
    measurements = ecg_qtc.parse_marker(verified) or ecg_qtc.parse_marker(draft)
    interpretation = ecg_qtc.strip_marker(interpretation)
    if measurements:
        # Drop any QTc the model wrote anyway, so the computed one below is
        # the only figure on screen -- two disagreeing QTc values with no
        # way to tell which is right is worse than either alone.
        interpretation = ecg_qtc.strip_model_qtc(interpretation)
        interpretation += "\n\n" + ecg_qtc.format_line(
            *measurements, translate=lambda s: ui_text.translate_message(language, s) or s
        )
    else:
        logger.warning("No usable QTDATA marker in the ECG read -- QTc omitted rather than guessed")
    if sources:
        interpretation += f"\n\n{sources}"
    return interpretation + _DISCLAIMER


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

    return _require_text(verified, draft, "lab interpretation") + _DISCLAIMER


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

    return _require_text(verified, draft, "lab interpretation") + _DISCLAIMER

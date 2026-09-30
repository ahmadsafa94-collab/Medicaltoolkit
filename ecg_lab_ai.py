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
import ecg_tiles
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
    "- QRS: onset to offset of QRS. Normal under 120 ms (under 3 small boxes). Measure the GLOBAL "
    "duration -- earliest onset visible in any lead to the latest offset visible in any lead. A "
    "standard sheet records four leads at a time down each column, so leads in the same column are "
    "simultaneous and can be read directly against each other. Two things in particular make this "
    "measurement come out too SHORT, and that is the error that matters, because it is the one that "
    "turns a block into a normal variant:\n"
    "  (a) The offset is the J point: where the last deflection of the QRS, however low its "
    "amplitude, finally meets the ST segment. In a notched or bundle-branch complex the terminal "
    "portion is slurred and low-voltage and slides gradually into the ST, so the eye stops at the end "
    "of the last TALL deflection and loses the 20-40 ms that follow it. That terminal slur is part of "
    "the QRS -- follow it to where the trace actually flattens.\n"
    "  (b) Measuring within a single lead. The onset may be earlier in another lead than in the one "
    "being read, and the offset later.\n"
    "  Measure it in the WIDEST lead on the tracing, not merely the clearest one -- QRS duration is defined by the "
    "broadest complex, and a bundle branch block puts its extra width at the END of the complex in "
    "one region only: the terminal R' of right bundle branch block lives in V1-V2 and can be "
    "invisible in the limb leads, the slurred terminal S of left bundle branch block in I and "
    "V5-V6. So scan all twelve leads, find the widest complex, measure from its earliest onset to "
    "its latest offset, and report THAT number. A QRS measured only in lead II is the classic way a "
    "bundle branch block gets called narrow and missed.\n"
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

# The "Notable morphology" line used to be specified as a short enumerated
# list -- ST segment, T waves, Q waves, voltage -- and the model answered
# exactly the question it was asked. A real tracing came back read as a
# narrow-QRS sinus rhythm with "low limb-lead voltage, no pathologic Q
# waves, ST at baseline, upright T waves" when it in fact showed right
# bundle branch block and an S1Q3T3 pattern. Neither was an oversight of
# vision: RBBB is a QRS-SHAPE finding and S1Q3T3 is a CROSS-LEAD one, and
# neither category was on the list, so neither was ever looked for.
#
# Replacing the list with an open-ended "describe anything notable" does
# not fix it either -- what counts as notable is exactly what a reader
# working quickly does not yet know to look at. So the named patterns are
# enumerated WITH the criteria that define them, and every line is worked
# through on every read. The cross-lead group is separate because those
# patterns are invisible to a lead-by-lead sweep: they only exist in the
# comparison between leads.
_PATTERN_CHECKLIST = (
    "NAMED-PATTERN CHECKLIST -- work silently through every line below on this tracing BEFORE you "
    "write the Notable morphology and Overall impression lines. Most entries will be absent, and "
    "that is fine: state 'within normal limits' only for the standard categories (QRS shape and "
    "conduction, ST segment, T waves, Q waves, voltage). Every other pattern here is mentioned ONLY "
    "when the criteria given for it are actually met -- do not list the ones you ruled out.\n\n"
    "CONDUCTION AND QRS SHAPE (check this group FIRST: a conduction abnormality changes how the ST "
    "segments, T waves and axis must be read):\n"
    "- Right bundle branch block: QRS 120 ms or wider WITH an rSR' / rsR' shape in V1-V2 (a second, "
    "late positive deflection -- the 'M' or 'rabbit ears' -- where the terminal R' is as tall as or "
    "taller than the initial r), plus a broad slurred S wave in I, aVL, V5-V6. Incomplete RBBB is "
    "the same shape at 110-119 ms. Look at V1 deliberately on EVERY tracing: a second positive "
    "deflection late in the QRS there is right bundle branch block and essentially nothing else.\n"
    "  If you DO see that rSR' / rsr' shape in V1-V2, the QRS width stops being one measurement "
    "among several and becomes the finding that decides the read, so treat it that way: go back and "
    "re-measure the width specifically, by the method given above, before concluding anything from "
    "it. And keep in mind what is being separated. The difference between 120 ms and 100 ms is half "
    "a small box on the paper -- a handful of pixels in this image, which is the same size as your "
    "own measurement error. So a width landing anywhere in 100-119 ms is NOT grounds for dismissing "
    "a shape you actually saw as a normal variant. Report both: say the shape is present, say the "
    "width is at or near the borderline, and name incomplete RBBB as the live consideration. \"An "
    "rsr' is present in V1-V2 and the QRS measures about 110 ms, so incomplete right bundle branch "
    "block cannot be excluded\" is a correct and useful read. \"An rsr' is present but the QRS is "
    "under 110 ms, so this is a normal variant\" discards a positive observation on the strength of "
    "a measurement that cannot resolve the distinction it is being used to make.\n"
    "  Two traps that go with this. First, the broad slurred terminal S in I, aVL, V5-V6 is obvious "
    "in COMPLETE RBBB but is often subtle or genuinely absent in the incomplete form, so its absence "
    "does not exclude RBBB when V1 shows the rSR'. Second, a notched V1 complex measured at under "
    "100 ms is internally inconsistent: the notch IS delayed right-ventricular activation and that "
    "delay takes time. Read that combination as a sign the width has been measured too short, not as "
    "evidence the notch is innocent.\n"
    "- Left bundle branch block: QRS 120 ms or wider with a broad, often notched monophasic R in I, "
    "aVL, V5-V6, a deep wide S or QS in V1-V2, and loss of the septal q in I/V5-V6. Incomplete LBBB "
    "at 110-119 ms.\n"
    "- Left anterior fascicular block: left axis deviation past -45 degrees with qR in I/aVL and rS "
    "in II, III, aVF, QRS under 120 ms. Left posterior fascicular block: right axis deviation with "
    "rS in I/aVL and qR in II, III, aVF.\n"
    "- Nonspecific intraventricular conduction delay: QRS 110 ms or wider without either bundle "
    "branch pattern.\n"
    "- Pre-excitation (WPW pattern): PR under 120 ms with a slurred delta-wave upstroke and a wide "
    "QRS.\n"
    "- AV block: first degree (PR over 200 ms), second degree Mobitz I (progressive PR lengthening "
    "then a dropped QRS) or Mobitz II (constant PR with a dropped QRS), third degree (P waves and "
    "QRS complexes entirely independent).\n"
    "- Paced beats; electrical alternans (QRS amplitude alternating beat to beat).\n\n"
    "CROSS-LEAD PATTERNS -- these exist only in the comparison BETWEEN leads, so a lead-by-lead "
    "sweep will not find them. Check each one across the leads named:\n"
    "- S1Q3T3: a prominent S wave in lead I, a Q wave in lead III, and T-wave inversion in lead III. "
    "When all three are present, name it. It is a pattern and nothing more -- neither sensitive nor "
    "specific -- classically discussed in the context of acute right-heart strain.\n"
    "  When only two of the three are there, do not report the pattern as simply absent. Say which "
    "components are present and which is not, in those terms: 'a Q wave and T-wave inversion in III "
    "are present; a prominent S in I is not'. Two of three is a real observation about the tracing "
    "and a flat 'no S1Q3T3' hides it -- and the missing component is frequently the one hardest to "
    "be sure of at this resolution. This applies to every multi-component pattern on this list.\n"
    "- Right-heart strain pattern: right axis deviation, new complete or incomplete RBBB, T-wave "
    "inversion across V1-V4 and sinus tachycardia appearing together.\n"
    "- Right ventricular hypertrophy: dominant R in V1 with right axis deviation. Left ventricular "
    "hypertrophy: S in V1 plus R in V5 or V6 over 35 mm, or R in aVL over 11 mm.\n"
    "- Atrial enlargement from the P wave: over 2.5 mm tall in II (right atrial), or notched and "
    "over 120 ms in II with a deep terminal negative component in V1 (left atrial).\n"
    "- Ischemia or infarction BY TERRITORY, named by the leads involved: V1-V4 anterior, I/aVL/"
    "V5-V6 lateral, II/III/aVF inferior, a tall R with ST depression in V1-V2 posterior. Check the "
    "opposite territory for reciprocal change.\n"
    "- Pericarditis pattern (widespread concave ST elevation with PR-segment depression), benign "
    "early repolarization, Brugada pattern (coved ST elevation with an RBBB-like V1-V2 morphology), "
    "Wellens pattern (biphasic or deeply inverted T in V2-V3), de Winter pattern, hyperkalemia "
    "(peaked T waves with a flattened or absent P), hypokalemia (flat T with a U wave), digoxin "
    "effect, low voltage, poor R-wave progression across V1-V3.\n\n"
    "Anything on this list that IS present gets named in Notable morphology together with the leads "
    "that show it, and carried into Overall impression. A pattern found and then left out of the "
    "impression is the same as never having found it."
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
    # Without this query the books' conduction chapters never reach the
    # verify pass unless the DRAFT already mentioned a bundle branch block
    # -- which is precisely the case where the draft is wrong, so the
    # criteria that would have caught it are the criteria that go missing.
    "bundle branch block criteria, right bundle branch block rSR prime in V1, left bundle branch "
    "block, incomplete block, fascicular block, QRS morphology and width, named ECG patterns "
    "including S1Q3T3 and right heart strain",
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


# Sent with the magnified views so the model knows what it is looking at.
# Without it the obvious failure mode is treating five pictures of one
# tracing as five tracings -- reporting the same finding four times, or
# "comparing" quadrants as if they were serial ECGs.
#
# The last paragraph is the other half of the fix. With the pattern
# checklist in place the model stopped skipping RBBB and started asserting
# its absence instead ("no rSR' pattern in V1-V2") from a view where the
# deflection is a handful of pixels wide. A confident wrong negative is
# worse than an admitted limit, so an unresolvable feature has to be
# reported as unresolvable.
_MAGNIFIED_VIEWS_INSTRUCTION = (
    "YOU ARE BEING SHOWN THE SAME TRACING SEVERAL TIMES. The first image is the complete sheet. "
    "The images after it are overlapping MAGNIFIED QUADRANTS of that same sheet, each labelled "
    "immediately above it. They are not different ECGs, not different patients and not serial "
    "tracings: do not compare them against each other, and do not report a finding twice because it "
    "appears in two overlapping views.\n"
    "Use them for different jobs. The complete sheet is for the layout -- which lead sits where, the "
    "rhythm strip, the calibration pulse, and the R-R spacing you count the rate from. The magnified "
    "quadrants are where you actually READ the waveform: QRS width, the shape of the terminal part of "
    "the QRS, notches, small Q waves, P-wave morphology, ST J-point deviation, T-wave polarity. Fine "
    "detail is several times larger there, and a feature one small box wide (40 ms) is close to "
    "invisible on the full sheet while being plainly legible in a quadrant.\n"
    "So before you state that any fine-grained feature is ABSENT, go to the quadrant that magnifies "
    "the lead in question and look there. This matters most for: the terminal portion of the QRS in "
    "V1 and V2 (a second, late positive deflection there is right bundle branch block), an S wave in "
    "I and aVL, a Q wave in III, and the true width of the widest QRS on the sheet. 'No rSR\' in V1' "
    "asserted from the full-sheet view alone is not a finding -- at that scale the deflection is a "
    "few pixels wide and absence cannot be established from it.\n"
    "If, having looked at the magnified view, you still genuinely cannot resolve a feature, say that "
    "it cannot be resolved on this image rather than reporting it as normal or absent. An honest "
    "'cannot be assessed on this tracing' is useful to a student; a confident 'normal' that is wrong "
    "teaches them the wrong thing.\n"
    "One consequence of the magnification: a 12-lead sheet prints a header, and at this scale any "
    "name, date of birth, record number or hospital detail in it may now be legible where it was not "
    "on the full sheet. Ignore all of it. Do not read it, do not transcribe it, do not refer to it "
    "and do not let it influence the read -- you are describing a waveform, and the six lines contain "
    "no patient details of any kind. This is a study exercise on a tracing the user was asked to "
    "de-identify; treat the header as if it were blank."
)


def _ecg_image_content(image_bytes: bytes, media_type: str) -> tuple[list[dict], bool]:
    """
    (content blocks for one pass, whether magnified views were built).

    Each quadrant is preceded by its own text label so the model can tell
    the views apart; on the fallback path this is byte-for-byte the single
    image block it always was.
    """
    views = ecg_tiles.build_views(image_bytes)
    if not views:
        return [_image_block(image_bytes, media_type)], False
    blocks: list[dict] = []
    for label, data, view_media_type in views:
        blocks.append({"type": "text", "text": f"[{label}]"})
        blocks.append(_image_block(data, view_media_type))
    return blocks, True


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

    Raised again when the read moved to five magnified views and a much
    longer prompt (the named-pattern checklist and the views instruction):
    more to work through before the first line is written makes a tight
    ceiling likelier to bite, and the six lines this produces are short, so
    the headroom costs little and only gets billed when it is used --
    output tokens are charged on what is generated, not on the budget.

    Raised once more, and by more than looks necessary, after a verify pass
    spent an entire 3000-token ceiling and emitted exactly one line of
    text. Six short lines do not cost 3000 tokens, so those tokens went
    into blocks that are not text and never reach the reader; whatever the
    ceiling is, it has to leave room for that on top of the visible answer.
    _pick_read is the backstop for when it still is not enough, and
    _call_claude_meta logs the block types so the next occurrence says
    outright where the budget went.
    """
    return 6000 if language.strip().lower() == "english" else 8000


def _call_claude(feature: str, system_prompt: str, content, max_tokens: int = 1200) -> str:
    """Just the text -- for callers that have no second pass to fall back on."""
    return _call_claude_meta(feature, system_prompt, content, max_tokens)[0]


def _call_claude_meta(feature: str, system_prompt: str, content, max_tokens: int = 1200) -> tuple[str, bool]:
    """
    (text, was_truncated) -- shared single-Claude-call plumbing (client call
    + cost logging + text extraction) for every pass below.

    Truncation is returned, not just logged, because a caller with two
    passes has to be able to act on it. A verify pass that stopped after
    "Rate: approximately 95" is not a better read than a complete draft,
    but it is longer than nothing, so a plain `verified or draft` picks it
    and shows the user a one-line interpretation -- which is exactly what
    happened, with the QTc line underneath quoting a different rate because
    the marker had fallen back to the draft.
    """
    try:
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as e:
        # str() on an exception is not guaranteed to say anything -- some
        # carry no message at all -- and "The AI request failed: " ending at
        # the colon is worse than useless, so the type is the floor.
        raise InterpretationError(f"The AI request failed: {e or type(e).__name__}")

    try:
        cost_ledger.record_claude_response(feature, response)
    except Exception:
        logger.exception("Cost ledger logging failed (non-fatal)")

    text = "".join(block.text for block in response.content if block.type == "text").strip()
    truncated = getattr(response, "stop_reason", None) == "max_tokens"
    if truncated:
        # Truncation is silent from the reader's side: the answer just
        # stops, mid-sentence, with the last lines missing but everything
        # above them looking right. Log it loudly so a budget that has
        # become too small shows up here instead of in a bug report.
        #
        # The block types are logged too, because they answer the question
        # this raises: a pass that burns thousands of tokens and emits one
        # line of text spent them somewhere, and a non-text block in this
        # list is where.
        logger.error(
            "%s hit its token limit and was TRUNCATED at %s output tokens (max_tokens=%s, "
            "block types=%s, text length=%d) -- raise _output_budget",
            feature,
            getattr(getattr(response, "usage", None), "output_tokens", "?"),
            max_tokens,
            [getattr(b, "type", "?") for b in response.content],
            len(text),
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
    return text, truncated


# Below this fraction of the draft's length, a verify pass is treated as
# stunted rather than concise. A real verify pass rewrites the same six
# lines, so it lands near the draft's length; a third of it means it
# stopped early. Deliberately not a check for the six line labels, which
# are translated into the user's language and so cannot be matched on.
_STUNTED_RATIO = 0.5


def _pick_read(verified: str, verified_truncated: bool, draft: str, draft_truncated: bool) -> tuple[str, bool]:
    """
    (final text, came_from_verify) -- which of the two passes to actually show.

    The verify pass wins by default: that is the whole point of running it.
    It loses only when it is visibly incomplete next to the draft, because
    a truncated verify is not a corrected read, it is the first line of
    one. `verified or draft` used to make this choice on emptiness alone,
    so a verify pass that died after its first line beat a complete draft
    and the user was shown a one-line ECG interpretation.
    """
    if not verified.strip():
        return draft, False
    if not draft.strip():
        return verified, True

    # Both truncated: nothing is complete, so take whichever got further.
    if verified_truncated and draft_truncated:
        return (verified, True) if len(verified) >= len(draft) else (draft, False)

    stunted = len(verified) < _STUNTED_RATIO * len(draft)
    if verified_truncated and stunted:
        logger.error(
            "Verify pass was truncated to %d chars against a %d-char draft -- showing the draft instead",
            len(verified), len(draft),
        )
        return draft, False
    if stunted:
        # No truncation flag, but the length says otherwise. Worth honouring:
        # the flag depends on the API reporting a stop_reason we recognise,
        # and being wrong here costs the user most of their read.
        logger.error(
            "Verify pass returned %d chars against a %d-char draft without a truncation flag "
            "-- showing the draft instead",
            len(verified), len(draft),
        )
        return draft, False
    return verified, True


def _require_text(verified: str, draft: str, what: str, hint: str = "") -> str:
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
            f"The AI returned an empty {what}. This is usually temporary -- please try again."
            + (f" {hint}" if hint else "")
        )
    return text


# The two user-turn texts, named because the retry paths below have to send
# exactly the same request the first attempt did.
DRAFT_REQUEST_TEXT = "Interpret this ECG tracing for study purposes."
VERIFY_REQUEST_TEXT = (
    "Here is the same ECG again, in the same views the draft was written from. "
    "Verify/correct the draft per your instructions."
)


def _single_view_fallback(image_bytes: bytes, media_type: str):
    """
    (image_content, magnified, views_note) for one un-magnified image.

    Used when a pass comes back with no text at all. Sending five views of
    a tracing is a bigger, stranger request than sending one, and a model
    that returns nothing is most likely objecting to something about the
    request rather than about the ECG -- so the retry drops back to exactly
    the request shape that worked before magnification existed, which is
    both the most likely thing to succeed and a direct test of whether the
    magnification is what broke it. The log line is the diagnosis: if these
    retries start appearing, the multi-view request is the cause, and if
    they do not, it is not.
    """
    logger.error(
        "ECG pass returned no text with magnified views -- retrying with a single full-sheet image. "
        "If this line is frequent, the multi-view request is the problem, not the tracing."
    )
    return [_image_block(image_bytes, media_type)], False, ""


def _ecg_draft_prompt(language: str, views_note: str) -> str:
    """
    The first-pass prompt. A function rather than an inline string because
    it has to be buildable twice: if a pass comes back with no text at all,
    interpret_ecg retries it with a single un-magnified image, and the
    views_note that describes the magnified quadrants has to come back out
    of the prompt when the quadrants themselves do -- a prompt that talks
    about five views while one image is attached is its own bug.
    """
    return (
        "You are helping a medical student practice ECG interpretation as a STUDY EXERCISE, not a clinical "
        "read for patient care. Look at the ECG image and describe what it shows using ALWAYS this exact "
        "structure, one line per item:\n"
        "Rate: <value in beats/min>\n"
        "Rhythm: <regular/irregular; P-wave presence and morphology>\n"
        "Axis: <normal / left deviation / right deviation, estimated>\n"
        "Intervals: <PR, QRS, QT -- and QTc if a rate is determinable>\n"
        "Notable morphology: <QRS shape and conduction, ST segment, T waves, Q waves, voltage, and any "
        "named pattern from the checklist below -- each phrased as 'within normal limits' or 'notable, "
        "commonly seen in ___', citing the general category of condition, never a specific patient "
        "diagnosis>\n"
        "Overall impression: <a plain-language summary of the PATTERN only, e.g. 'sinus rhythm with normal "
        "intervals' or 'ST elevation pattern in the anterior leads, commonly associated with anterior wall "
        "ischemia/infarction as a category' -- NEVER state or imply this specific image IS a diagnosis like "
        "'this is a STEMI' or 'this patient has X'.>\n\n"
        f"{views_note}"
        f"{_MEASUREMENT_METHOD_INSTRUCTION}\n\n"
        f"{_PATTERN_CHECKLIST}\n\n"
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


def _ecg_verify_prompt(language: str, draft: str, reference_block: str, magnified: bool) -> str:
    """The second-pass prompt. A function for the same reason as
    _ecg_draft_prompt above -- the retry has to rebuild it without the
    magnified-views instruction."""
    instructions = []
    if magnified:
        instructions.append(_MAGNIFIED_VIEWS_INSTRUCTION)
    instructions += [
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
        # The draft not mentioning a pattern is not evidence the pattern is
        # absent -- it is the single most likely thing for a first read to
        # have skipped. So the checklist is worked through again here from
        # the image, deliberately without taking the draft's silence as an
        # answer, which is the only way a missed RBBB or S1Q3T3 gets caught.
        "Work through the NAMED-PATTERN CHECKLIST yourself, on the image, from the beginning. Do "
        "NOT treat the draft's silence about a pattern as evidence that the pattern is absent -- an "
        "omitted finding is the commonest error in a first read, and it looks identical to a "
        "correctly negative one. Check lead V1 for a late second positive deflection (right bundle "
        "branch block) and leads I and III together for S1Q3T3 explicitly, every time, whatever the "
        "draft says. Add to Notable morphology and Overall impression any pattern whose criteria are "
        "met, and remove any the draft claimed whose criteria are not:\n"
        f"{_PATTERN_CHECKLIST}",
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

    return (
        "You are the SECOND, independent reviewer checking a draft ECG interpretation against the actual "
        "image, as a quality check before it's shown to a medical student. You will see the same ECG image "
        "plus the draft interpretation below. Your job:\n"
        + "\n".join(f"{i}. {text}" for i, text in enumerate(instructions, start=1))
        + (f"\n\nTEACHING REFERENCES:\n{reference_block}" if reference_block else "")
        + f"\n\nDRAFT INTERPRETATION TO CHECK:\n{draft}"
    )


def interpret_ecg(image_bytes: bytes, media_type: str, language: str = "English") -> str:
    """
    Synchronous -- run via asyncio.to_thread from an async handler.
    image_bytes: raw bytes of a photographed/scanned ECG tracing.
    Two Claude calls: a draft read, then an independent verify pass over the
    same image (see module docstring) before the disclaimer is appended.
    """
    # Built once and reused by both passes: cropping is cheap but not free,
    # and the verify pass must see exactly the same views as the draft or it
    # cannot check the draft's claims about them.
    image_content, magnified = _ecg_image_content(image_bytes, media_type)
    views_note = f"{_MAGNIFIED_VIEWS_INSTRUCTION}\n\n" if magnified else ""

    # Both passes get the same, language-aware budget (see _output_budget):
    # a read truncated mid-structure is worse than a slightly costlier one,
    # and the VERIFY pass is the one the user actually sees.
    draft, draft_truncated = _call_claude_meta(
        "ecg_interpretation",
        _ecg_draft_prompt(language, views_note),
        [*image_content, {"type": "text", "text": DRAFT_REQUEST_TEXT}],
        max_tokens=_output_budget(language),
    )
    if not draft and magnified:
        image_content, magnified, views_note = _single_view_fallback(image_bytes, media_type)
        draft, draft_truncated = _call_claude_meta(
            "ecg_interpretation",
            _ecg_draft_prompt(language, views_note),
            [*image_content, {"type": "text", "text": DRAFT_REQUEST_TEXT}],
            max_tokens=_output_budget(language),
        )

    # The draft's TEXT is the retrieval query for the admin's ECG textbooks:
    # the image itself can't be embedded against text chunks, but the draft's
    # structured read (rate/rhythm/axis/intervals/morphology) describes the
    # tracing precisely enough to pull the passages that actually teach this
    # pattern. Empty when no reference books are loaded, in which case the
    # verify prompt below is exactly what it was before -- see ecg_reference.py.
    reference_block, reference_hits = ecg_reference.build_reference([draft, *_METHOD_QUERIES])

    verified, verified_truncated = _call_claude_meta(
        "ecg_interpretation_verify",
        _ecg_verify_prompt(language, draft, reference_block, magnified),
        [*image_content, {"type": "text", "text": VERIFY_REQUEST_TEXT}],
        max_tokens=_output_budget(language),
    )
    if not verified and magnified:
        # Only reached when the DRAFT succeeded on five views and the
        # verify pass alone came back empty -- if the draft had already
        # fallen back, `magnified` is False by now and the verify call
        # above used the single view too, rather than re-sending a request
        # shape that has just been shown not to work.
        #
        # The draft survives an empty verify pass (`verified or draft`
        # below), so this retry is not load-bearing the way the draft's is.
        # It is still worth one attempt: the verify pass is the one whose
        # text the reader actually gets, and losing it silently means
        # losing the second look at the checklist.
        image_content, magnified, views_note = _single_view_fallback(image_bytes, media_type)
        verified, verified_truncated = _call_claude_meta(
            "ecg_interpretation_verify",
            _ecg_verify_prompt(language, draft, reference_block, magnified),
            [*image_content, {"type": "text", "text": VERIFY_REQUEST_TEXT}],
            max_tokens=_output_budget(language),
        )

    # Sources sit between the read and the disclaimer: the student can see
    # which book and page backs what they were just told (and go look it
    # up), while the safety note stays last where it's most visible. Absent
    # entirely when no teaching book informed this read, rather than a
    # "no sources" line -- there is nothing to cite, and saying so every
    # time would just be noise on an otherwise unchanged read.
    sources = ecg_reference.format_sources(reference_hits)

    # Which pass to show, and therefore which pass's measurements to trust
    # -- the two have to be the same one. They were not: the text came from
    # `verified or draft` while the QTc marker came from
    # parse_marker(verified) or parse_marker(draft), so a verify pass that
    # truncated before writing its marker put the draft's rate under the
    # verify pass's text and printed two different rates in one read.
    final, from_verify = _pick_read(verified, verified_truncated, draft, draft_truncated)
    primary, secondary = (verified, draft) if from_verify else (draft, verified)

    interpretation = _require_text(
        final,
        draft,
        "ECG interpretation",
        # Both passes came back empty, including the single-image retry, so
        # the usual advice ("send a clearer photo") is not the issue and
        # saying it just sends the user in circles. Point at the one thing
        # that is actually likely to differ on a second attempt.
        hint="If it happens again, try cropping the image to just the tracing, or send it as an "
        "ordinary photo rather than a file.",
    )

    # QTc is computed here, from the QT and rate the model measured, rather
    # than by the model -- see ecg_qtc. The marker comes from whichever pass
    # supplied the text above, so the QT and rate under the read are the
    # ones that read was written from; the other pass is only a fallback for
    # when the chosen one never emitted a marker at all.
    measurements = ecg_qtc.parse_marker(primary) or ecg_qtc.parse_marker(secondary)
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

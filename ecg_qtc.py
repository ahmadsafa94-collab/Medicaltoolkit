"""
QTc, computed here rather than asked of the model.

The model was being told the Bazett formula and asked to apply it, and it
kept getting the number wrong. That is not a prompt problem and no amount
of rewording fixes it: a square root and a division are arithmetic, and a
language model produces them by generating plausible digits, not by
calculating. Everything upstream of the arithmetic -- reading the QT
duration and the rate off the tracing -- is genuine vision work the model
does well, so the split is: it measures, this module computes.

The model emits its two measurements in a marker line that never reaches
the reader (see MARKER_RE); this module parses them, computes both
corrections exactly, and the caller replaces the marker with a QTc line
built from real numbers.

Two corrections because they disagree in exactly the cases that matter.
Bazett over-corrects at fast rates and under-corrects at slow ones -- at
100/min it can read ~35 ms longer than Fridericia -- so a tachycardic
patient can look prolonged by Bazett alone when Fridericia says
otherwise. Showing both is how the discrepancy becomes visible instead of
being hidden behind whichever one happened to be used.
"""

import logging
import re

logger = logging.getLogger(__name__)

# What the model is asked to append. ASCII and fixed, so it survives
# translation and right-to-left rendering intact.
MARKER_RE = re.compile(r"<<\s*QTDATA\s+qt_ms\s*=\s*(\d{2,4})\s+rate_bpm\s*=\s*(\d{1,3})\s*>>", re.IGNORECASE)

# Physiologically possible bounds. A value outside these means the marker
# was misread or invented, and a wrong QTc is worse than none -- better to
# print nothing than a confidently wrong number on a medical read.
QT_MIN_MS, QT_MAX_MS = 200, 800
RATE_MIN_BPM, RATE_MAX_BPM = 20, 300


def parse_marker(text: str) -> tuple[int, int] | None:
    """(qt_ms, rate_bpm) from the model's marker line, or None if absent/implausible."""
    match = MARKER_RE.search(text or "")
    if not match:
        return None
    qt_ms, rate_bpm = int(match.group(1)), int(match.group(2))
    if not (QT_MIN_MS <= qt_ms <= QT_MAX_MS and RATE_MIN_BPM <= rate_bpm <= RATE_MAX_BPM):
        logger.warning("QTc marker out of physiological range (QT=%s ms, rate=%s/min) -- ignoring", qt_ms, rate_bpm)
        return None
    return qt_ms, rate_bpm


def strip_marker(text: str) -> str:
    """Remove the marker line from text shown to the user."""
    return re.sub(r"[ \t]*" + MARKER_RE.pattern + r"[ \t]*", "", text or "", flags=re.IGNORECASE).strip()


def compute(qt_ms: int, rate_bpm: int) -> dict:
    """
    Both rate corrections, in ms, rounded to whole milliseconds.

        RR (s)      = 60 / rate
        QTc Bazett  = QT / RR**(1/2)
        QTc Frider. = QT / RR**(1/3)
    """
    rr_seconds = 60.0 / rate_bpm
    return {
        "qt_ms": qt_ms,
        "rate_bpm": rate_bpm,
        "rr_seconds": round(rr_seconds, 3),
        "bazett_ms": round(qt_ms / (rr_seconds ** 0.5)),
        "fridericia_ms": round(qt_ms / (rr_seconds ** (1.0 / 3.0))),
    }


# Thresholds are sex-dependent and this bot never knows the patient's sex,
# so the bands are stated the way a reader can apply them, rather than a
# single verdict that would be wrong half the time.
def category(qtc_ms: int) -> str:
    if qtc_ms > 500:
        return "markedly prolonged (>500 ms)"
    if qtc_ms > 460:
        return "prolonged"
    if qtc_ms >= 440:
        return "borderline (normal in women, borderline in men)"
    return "normal"


# The only prose in the QTc line, kept as a handful of fixed strings so
# they can be pre-translated like any other UI text (ui_text.COMMON_MESSAGES)
# instead of the whole line -- which carries numbers, and so would miss the
# translation cache on every single read.
DISAGREEMENT_NOTE = (
    "The two corrections disagree at this rate. Bazett over-corrects when the rate is fast and "
    "under-corrects when it is slow, so Fridericia is the more reliable of the two here."
)
TRANSLATABLE_FRAGMENTS = [
    "normal",
    "borderline (normal in women, borderline in men)",
    "prolonged",
    "markedly prolonged (>500 ms)",
    DISAGREEMENT_NOTE,
]


def format_line(qt_ms: int, rate_bpm: int, translate=None) -> str:
    """
    The QTc line inserted into an interpretation, built from computed
    values. Bazett is categorised because it is the threshold clinicians
    quote; Fridericia sits alongside it so a disagreement is visible.

    `translate` is an optional str -> str used for the few fixed phrases,
    so the line reads in the user's language while the numbers stay
    numbers. Everything else here (ms, Bazett, Fridericia, QT, RR) is
    written the same way in every language the bot offers.
    """
    say = translate or (lambda s: s)
    r = compute(qt_ms, rate_bpm)
    line = (
        f"QTc: {r['bazett_ms']} ms (Bazett) / {r['fridericia_ms']} ms (Fridericia) "
        f"-- {say(category(r['bazett_ms']))}"
        f" [QT {r['qt_ms']} ms @ {r['rate_bpm']}/min, RR {r['rr_seconds']:.2f} s]"
    )
    if abs(r["bazett_ms"] - r["fridericia_ms"]) >= 20:
        line += "\n" + say(DISAGREEMENT_NOTE)
    return line

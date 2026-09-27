"""
Makes right-to-left text render correctly when it contains English.

A Persian ECG read is full of unavoidable Latin: PR, QRS, QTc, V1-V3, ms,
lead names, numbers. Unicode lays those out by its bidirectional
algorithm, which decides each run's direction from the characters
themselves -- so an RTL sentence with Latin embedded in it comes out with
the fragments visibly in the wrong order, punctuation drifting to the far
end of the line, and ranges like "V1-V3" reading backwards. The content
is correct; the ordering on screen is not, which is exactly the "sentences
are mixed up" a reader sees.

Two marks fix it, and they are what this module inserts:

  - RLM (U+200F) at the start of a line whose content is RTL, to pin the
    line's base direction. Without it a line that happens to BEGIN with
    Latin ("Rate: ...") is laid out left-to-right as a whole, throwing the
    Persian that follows to the wrong side.
  - FSI...PDI (U+2068/U+2069) around each embedded Latin/number run, which
    isolates it so it is placed as one unit at the correct point in the
    RTL flow instead of being reordered against its neighbours.

Isolates rather than the older LRM marks: LRM only nudges a boundary,
while an isolate states that the run is a self-contained island, which is
what keeps "V1-V3" and "120-130 ms" intact.

Applied to outgoing text regardless of whether it was translated here or
written in the user's language by the AI -- this is a rendering fix, not a
translation one, so ui_text.untranslated() does not exempt content from it.
"""

import re

# Languages written right-to-left, of those the bot offers (language.py).
RTL_LANGUAGES = {"persian", "farsi", "arabic", "urdu", "hebrew", "pashto", "kurdish"}

RLM = "‏"  # right-to-left mark: sets a line's base direction
FSI = "⁨"  # first-strong isolate: opens a self-contained run
PDI = "⁩"  # pop directional isolate: closes it

# A run of Latin letters/digits and the punctuation that binds them
# together -- "V1-V3", "120-130 ms", "QTc", "0.04 s". Kept as one unit so
# the hyphen in a range doesn't get reordered away from its endpoints.
_LATIN_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9./:%+_-]*(?:\s+[A-Za-z0-9][A-Za-z0-9./:%+_-]*)*")

_RTL_CHARS = re.compile(r"[֐-׿؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


def is_rtl_language(language: str | None) -> bool:
    return bool(language) and language.strip().lower() in RTL_LANGUAGES


def fix(text: str, language: str | None) -> str:
    """
    Return `text` with bidi marks inserted, or unchanged when the language
    isn't RTL or the text has no RTL content to protect.

    Idempotent: a line that already carries the marks is left alone, so
    text passing through more than once doesn't accumulate them.
    """
    if not text or not is_rtl_language(language):
        return text
    if not _RTL_CHARS.search(text):
        return text  # nothing right-to-left here, e.g. a purely English line

    out = []
    for line in text.split("\n"):
        if not line.strip() or not _RTL_CHARS.search(line):
            out.append(line)  # blank, or wholly Latin -- leave it as it is
            continue
        if FSI in line or line.startswith(RLM):
            out.append(line)  # already processed
            continue
        isolated = _LATIN_RUN.sub(lambda m: f"{FSI}{m.group(0)}{PDI}", line)
        out.append(RLM + isolated)
    return "\n".join(out)

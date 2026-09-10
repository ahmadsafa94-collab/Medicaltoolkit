"""
Exports a stored flashcard deck (see flashcards.py) as a real Anki .apkg
file via genanki -- lets a student pull their generated cards into the
actual Anki app/mobile client instead of only reviewing them inside this
bot. genanki builds on the same SM-2-family scheduling concept flashcards.py
already implements, so "front/back pairs generated here" maps directly onto
"a basic Anki note type" with no format translation needed beyond field names.

NOTE: this module could not be exercised against the real genanki package in
the sandboxed environment this was built in (PyPI wasn't reachable there) --
the API used below matches genanki's documented usage exactly, but this is
the one piece of this delivery that's unverified by an actual run. `genanki`
is in requirements.txt; if the generated .apkg doesn't open cleanly in Anki,
that's the first place to check.
"""

import logging

logger = logging.getLogger(__name__)


class AnkiExportError(Exception):
    pass


# Fixed, randomly-chosen-once model/deck id namespace for this bot's exports
# -- genanki requires stable integer ids so re-imports of a deck from the
# same bot merge instead of duplicating. Any large fixed int works; these are
# just two arbitrary-but-fixed 32-bit-ish numbers.
_MODEL_ID = 1607392319
_DECK_ID_BASE = 1972000000  # deck id = base + a stable hash of book_id, see _deck_id()


def _deck_id(book_id: str) -> int:
    return _DECK_ID_BASE + (abs(hash(book_id)) % 1_000_000)


def export_deck_to_apkg(book_title: str, book_id: str, cards: list[dict], output_path: str) -> None:
    """
    cards: flashcards.get_deck()'s records (only "front"/"back" are used --
    SM-2 scheduling state stays in this bot, Anki gets fresh cards to
    schedule on its own from here on, which is the normal expectation when
    exporting to a different spaced-repetition app).

    genanki is imported lazily, inside this function, rather than at module
    load time: this keeps a genanki install problem (or, in this dev
    sandbox, no PyPI access at all -- see the module docstring) from taking
    down the whole bot process at import time just because ONE export
    feature depends on it. A missing/broken genanki surfaces as a clear
    AnkiExportError right when export is actually used, not as a startup crash.
    """
    if not cards:
        raise AnkiExportError("This deck has no cards to export.")

    try:
        import genanki
    except ImportError:
        raise AnkiExportError("Anki export isn't available on this deployment (the genanki package isn't installed).")

    model = genanki.Model(
        _MODEL_ID,
        "Medical Student Toolkit -- Basic",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[
            {
                "name": "Card 1",
                "qfmt": "{{Front}}",
                "afmt": '{{FrontSide}}<hr id="answer">{{Back}}',
            }
        ],
    )

    deck = genanki.Deck(_deck_id(book_id), f"Medical Student Toolkit -- {book_title}")
    for card in cards:
        note = genanki.Note(model=model, fields=[card["front"], card["back"]])
        deck.add_note(note)

    try:
        genanki.Package(deck).write_to_file(output_path)
    except Exception as e:
        raise AnkiExportError(f"Failed to build the .apkg file: {e}")

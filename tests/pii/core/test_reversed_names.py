"""Layer-1 pass 2: surname-first name forms (2026-08-19).

`John Smith` in the header, `SMITH JOHN` in a fixed-width name column. The two
are unreachable from each other by every mechanism the document already has —
exact and squash matching see a different string, the borrowed fuzzy tier prices
a word swap far above any budget, and grouping's distance runs on the
separator-collapsed form where `johnsmith` and `smithjohn` are most of a string
apart. Only knowing that a name can be printed either way round reaches it.

Third rule of the `derived.py` family, and the same shape as the other two:
hypothesise a plausible printed form from a value some layer READ, then search
for it. Two words, neither an initial (Sergei's scope call).

The placeholders are per surface form here as everywhere — `John Smith` takes
PERSON_1 and `SMITH JOHN` takes PERSON_2 — because the map keys on the value so
that rehydration restores what the document had. That is deliberate and is
tested, not a fork.
"""

from __future__ import annotations

import pytest

from pii.core.derived import KnownValues
from pii.core.detection import Detection
from pii.core.mapping import PseudonymMap
from pii.core.pipeline import apply_plan


def _person(text: str, value: str) -> Detection:
    start = text.index(value)
    return Detection("PERSON", start, start + len(value), 1.0)


def _typed(pipeline, text: str, *people: str, known: KnownValues | None = None):
    spans, _ = pipeline.merge_detections(
        [_person(text, p) for p in people], text, None, known
    )
    return [(s.entity_type, text[s.start : s.end]) for s in spans]


def test_the_surname_first_form_is_found_from_the_header_name(pipeline):
    text = "Holder: John Smith\nName column   SMITH JOHN   42.00\n"
    found = _typed(pipeline, text, "John Smith")
    assert ("PERSON", "SMITH JOHN") in found, found


def test_the_reversal_is_found_on_a_page_that_names_nobody(pipeline):
    """The document-wide half. The header is on another page; this one carries
    only the name column, and nothing here detects a person at all."""
    text = "Name column   SMITH JOHN   42.00\n"
    known = KnownValues.of([("PERSON", "John Smith")])
    assert _typed(pipeline, text, known=known) == [("PERSON", "SMITH JOHN")]
    # ...and with no document behind it, this page knows nothing.
    assert _typed(pipeline, text) == []


@pytest.mark.parametrize(
    "person,printed",
    [
        ("J Smith", "Smith J"),          # leading initial
        ("Smith J", "J Smith"),          # trailing initial
        ("J. Smith", "Smith J."),        # ...with the stop
    ],
)
def test_an_initial_is_never_reversed(pipeline, person, printed):
    """Sergei's scope call, and the reason for it: a single letter is both an
    implausible printing and the largest false-match surface in the family."""
    text = f"Holder: {person}\nColumn   {printed}   42.00\n"
    found = _typed(pipeline, text, person)
    assert ("PERSON", printed) not in found, found


@pytest.mark.parametrize(
    "person", ["Emily Jane Moore", "Moore", "Emily van der Berg"]
)
def test_only_a_two_word_name_is_reversed(pipeline, person):
    """Two words bounds the permutation to ONE candidate. Three words admit
    five, none of them evidently the printed one, and a one-word value has no
    reversal at all."""
    words = person.split()
    text = f"Holder: {person}\nColumn   {' '.join(reversed(words))}\n"
    found = _typed(pipeline, text, person)
    assert [v for kind, v in found] == [person], found


def test_the_reversal_of_a_symmetric_name_is_not_a_second_span(pipeline):
    """`Smith Smith` reversed is itself — emitting it would claim a second
    occurrence where the document has one."""
    text = "Holder: Smith Smith\n"
    found = _typed(pipeline, text, "Smith Smith")
    assert found == [("PERSON", "Smith Smith")], found


def test_a_derived_reversal_is_not_emitted_inside_an_existing_span(pipeline):
    """Same rule the other two derived rules follow: a span already covering
    those characters carries the label, and a second one would double-count."""
    text = "Holder: John Smith\n"
    found = _typed(pipeline, text, "John Smith")
    assert found == [("PERSON", "John Smith")], found


def test_each_surface_form_keeps_its_own_placeholder(pipeline):
    """Not a fork — the documented design. The map keys on the value so that
    rehydration restores the surface form the document actually had; giving one
    person one placeholder would restore `John Smith` where the name column
    printed `SMITH JOHN`."""
    text = "Holder: John Smith\nName column   SMITH JOHN   42.00\n"
    spans, _ = pipeline.merge_detections([_person(text, "John Smith")], text)
    pmap = PseudonymMap()
    out = apply_plan(text, spans, pmap)
    assert "PERSON_1" in out and "PERSON_2" in out, out
    assert "John Smith" not in out and "SMITH JOHN" not in out, out
    assert pmap.rehydrate("PERSON_1") == "John Smith"
    assert pmap.rehydrate("PERSON_2") == "SMITH JOHN"

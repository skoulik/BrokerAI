"""Layer-1 pass 2: joint-account names derived from known people (2026-08-14).

Replaces the tests for the standalone `JointNameRule` pattern, deleted the same
day. That rule guessed a joint name from its shape alone and could not be made
precise — see `test_the_shape_alone_is_never_enough` for the class of failure
that killed it.

These drive through `PiiPipeline.merge_detections`, which is the production
path: it is the one place where every layer's spans exist together, so it is
the only place pass 2 can run. `pipeline.strip()` is pass 1 only and will not
show joint names.
"""

from __future__ import annotations

import pytest

from pii.core.derived import JointNames, parse_joint
from pii.core.detection import Detection
from pii.core.mapping import PseudonymMap
from pii.core.pipeline import apply_plan


def _person(text: str, value: str) -> Detection:
    start = text.index(value)
    return Detection("PERSON", start, start + len(value), 1.0)


def _typed(pipeline, text: str, *people: str):
    """Run the production path with `people` as the layer-0 person spans."""
    spans, _ = pipeline.merge_detections(
        [_person(text, p) for p in people], text
    )
    return [(s.entity_type, text[s.start : s.end]) for s in spans]


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    "value,surname,people",
    [
        ("E & J Moore", ("Moore",), ()),
        ("E&J Moore", ("Moore",), ()),
        ("E. & J. Moore", ("Moore",), ()),
        ("E and J Moore", ("Moore",), ()),
        ("Emily and John Moore", ("Moore",),
         ("Emily Moore", "John Moore")),
        ("Emily Moore and John Moore", ("Moore",),
         ("Emily Moore", "John Moore")),
    ],
)
def test_parses_the_joint_shapes(value, surname, people):
    parsed = parse_joint(value)
    assert parsed is not None, value
    assert parsed.surname == surname
    assert parsed.people == people


@pytest.mark.parametrize(
    "value",
    [
        "John Smith",                      # one person
        "Olga Kulik and Sergei Petrov",    # two people, different surnames
        "Smith & Jones",                   # neither side is an initial
    ],
)
def test_rejects_what_is_not_a_joint_name(value):
    assert parse_joint(value) is None


def test_parse_trusts_its_caller_about_personhood():
    """`parse_joint` answers "what joint form is this value", never "is this a
    person" — a detector already decided that. So 'R&D Team' parses, and it is
    harmless precisely because nothing calls this on a value no layer detected
    (see test_the_shape_alone_is_never_enough).

    Deciding personhood here is what the deleted rule attempted, and what it
    could not do."""
    assert parse_joint("R&D Team") is not None


def test_a_surname_may_be_several_words():
    """'van der Berg' is one surname, not a given name plus two others — so
    the shared part is the longest common TRAILING sequence, not the last
    word."""
    parsed = parse_joint("Emily and John van der Berg")
    assert parsed.surname == ("van", "der", "Berg")
    assert parsed.people == ("Emily van der Berg", "John van der Berg")


def test_a_shared_GIVEN_name_is_not_a_surname():
    """Read as 'any word in both', 'John Smith' + 'John Brown' share John and
    would send us hunting for 'S & B John'."""
    assert parse_joint("John Smith and John Brown") is None


# ------------------------------------------------------- classify + derive


def test_a_person_that_is_really_a_joint_name_is_retyped(pipeline):
    text = "Account holders: Emily Moore and John Moore."
    assert ("PERSON_JOINT", "Emily Moore and John Moore") in _typed(
        pipeline, text, "Emily Moore and John Moore"
    )


def test_the_initials_form_is_found_from_the_full_names(pipeline):
    """The point of the redesign: layer 0 names the couple in the header, and
    the transaction line's initials form is reached from it."""
    text = (
        "Account holders: Emily Moore and John Moore.\n"
        "OSKO P12345678 E & J MOORE RENT\n"
    )
    found = _typed(pipeline, text, "Emily Moore and John Moore")
    assert ("PERSON_JOINT", "E & J MOORE") in found


def test_both_orderings_are_searched(pipeline):
    """(A, B) and (B, A) are different candidates — the same couple can be
    printed either way round and only one of them is in the text."""
    text = (
        "Holders: Emily Moore and John Moore.\n"
        "Rent J & E Moore\nLoan E & J Moore\n"
    )
    found = _typed(pipeline, text, "Emily Moore and John Moore")
    assert ("PERSON_JOINT", "J & E Moore") in found
    assert ("PERSON_JOINT", "E & J Moore") in found


def test_any_given_name_may_supply_the_initial(pipeline):
    """'any word of A that is not a surname' — a middle name counts."""
    text = "Emily Jane Moore. John Moore. Payment J & J MOORE\n"
    found = _typed(pipeline, text, "Emily Jane Moore", "John Moore")
    assert ("PERSON_JOINT", "J & J MOORE") in found


def test_a_multiword_surname_is_found_in_the_initials_form(pipeline):
    text = "Holders: Emily and John van der Berg.\nPay J & E VAN DER BERG\n"
    found = _typed(pipeline, text, "Emily and John van der Berg")
    assert ("PERSON_JOINT", "J & E VAN DER BERG") in found


def test_the_surname_alone_becomes_a_person(pipeline):
    """A joint name proves the surname belongs to a person, so a bare
    occurrence elsewhere strips too — nothing else in the stack catches it."""
    text = "OSKO E & J MOORE RENT\nDirect debit MOORE\n"
    found = _typed(pipeline, text, "E & J MOORE")
    assert ("PERSON", "MOORE") in found


def test_the_surname_inside_a_joint_span_is_not_a_separate_person(pipeline):
    """It is already covered, and by the more specific label."""
    text = "OSKO E & J MOORE RENT\n"
    found = _typed(pipeline, text, "E & J MOORE")
    assert found.count(("PERSON", "MOORE")) == 0
    assert ("PERSON_JOINT", "E & J MOORE") in found


# ----------------------------------------------------------- what it fixes


@pytest.mark.parametrize(
    "text", ["Paid H&M Stores 42.00", "Transfer to P&O Cruises",
             "R&D Team offsite", "Q&A Session notes"]
)
def test_the_shape_alone_is_never_enough(pipeline, text):
    """What killed the old rule. Every one of these matched its pattern and
    stripped as PERSON; none is reachable now without a detected person whose
    surname is Stores / Cruises / Team / Session."""
    assert _typed(pipeline, text) == []


def test_a_corporate_joint_name_needs_people_too(pipeline):
    """'E & J HOLDINGS' needed a hand-written corporate-word list before. Now
    it needs two people surnamed Holdings, and there are none."""
    assert _typed(pipeline, "Invoice from E & J HOLDINGS") == []


# --------------------------------------------------------------- placeholder


def test_the_joint_form_gets_its_own_placeholder(pipeline):
    """Not a third PERSON: two humans must not acquire three identities in the
    map, and nothing there would mark the third as the compound of the rest."""
    text = "Emily Moore and John Moore paid rent. Rent E & J Moore\n"
    spans, _ = pipeline.merge_detections(
        [_person(text, "Emily Moore and John Moore")], text
    )
    out = apply_plan(text, spans, PseudonymMap())
    assert "JOINT_1" in out
    assert "Moore" not in out


def test_pass_two_is_blind_to_which_layer_supplied_a_name():
    """The rule takes person detections, never 'layer 0's output' — so a
    layer-1 PERSON source added later feeds it with no rewiring."""
    text = "Emily Moore and John Moore. Rent E & J Moore\n"
    seed = _person(text, "Emily Moore and John Moore")
    seed.recognizer = "SomeFutureLayer1Rule"
    _, added = JointNames().apply([seed], text)
    assert any(d.entity_type == "PERSON_JOINT" for d in added)

"""Document-wide entity grouping: who groups with whom, and what class wins.

Two properties are under test and they pull in opposite directions:

- grouping must survive the variation that is TRANSCRIPTION — case, spacing,
  a digit read as a letter — because that is the same value written twice;
- and it must not survive the variation that is IDENTITY — a different digit
  in an account number is a different account.

The vote is tested separately from the clustering: it decides a class that
replaces every member's own, in both directions, so its tie-break is the one
knob that can turn a redaction into a keep.

Model-free: findings are constructed directly. Dual coverage per the project
rule — the corpus probe is the other half.
"""

from __future__ import annotations

from pii.core.grouping import (
    CLASS_PRIORITY,
    group_findings,
)
from pii.core.locator import ALL_TIERS
from pii.core.vlm import VlmFinding


def _pages(*pages):
    """Findings per page from `(text, entity_type)` tuples."""
    return [
        [VlmFinding(text=text, entity_type=etype) for text, etype in page]
        for page in pages
    ]


def _texts(group):
    return sorted(v.text for v in group.variants)


def _group_for(grouping, text):
    (group,) = [g for g in grouping.groups if text in _texts(g)]
    return group


# --------------------------------------------------------------------------
# Clustering — transcription variation groups, identity variation does not
# --------------------------------------------------------------------------


def test_case_and_separators_are_transcription_not_identity():
    # The dominant variant pair in these documents: caps in a header, title
    # case in the body. Raw edit distance is blind to it (8 edits).
    grouping = group_findings(
        _pages(
            [("SMITH JOHN", "PERSON")],
            [("Smith John", "PERSON")],
            [("Smith  John", "PERSON")],
        )
    )
    assert len(grouping.groups) == 1
    assert set(_texts(grouping.groups[0])) == {
        "SMITH JOHN", "Smith John", "Smith  John",
    }


def test_a_digit_read_as_a_letter_groups():
    # Cross-class confusion: 'O' where '0' belongs is damage, and the same
    # account number written twice must not fork into two entities.
    grouping = group_findings(
        _pages(
            [("014-936 111873883", "IDENTIFIER_GENERIC")],
            [("Ol4-936 111873883", "IDENTIFIER_GENERIC")],
        )
    )
    assert len(grouping.groups) == 1


def test_a_different_digit_is_a_different_entity():
    # One edit apart, and the permissive budget would merge them. It must not:
    # these are two accounts, and the group table is an audit surface.
    grouping = group_findings(
        _pages([("014-936", "IDENTIFIER_GENERIC"),
                ("014-937", "IDENTIFIER_GENERIC")])
    )
    assert len(grouping.groups) == 2


def test_measured_digit_confusions_do_not_discount_identifiers():
    # 1<->2 and 4<->8 are in fuzzy.CONFUSION_PAIRS from the OCR fidelity
    # sweep. Discounting them is right for the locator (a box pins the region)
    # and wrong here, where nothing does.
    grouping = group_findings(
        _pages([("4936 1174", "IDENTIFIER_GENERIC"),
                ("8936 1174", "IDENTIFIER_GENERIC")])
    )
    assert len(grouping.groups) == 2


def test_a_word_of_digit_confusables_is_not_identifier_shaped():
    # 'boss' is entirely digit homoglyphs (b,o,s,s). Without the real-digit
    # floor it would claim the strict table and stop grouping with its own
    # transcription variants.
    grouping = group_findings(
        _pages([("Boss Logistics", "ORGANIZATION")],
               [("BOSS LOGISTICS", "ORGANIZATION")])
    )
    assert len(grouping.groups) == 1


def test_one_genuine_character_difference_splits():
    # The budget reads: any number of known glyph confusions, but not a single
    # ordinary difference.
    grouping = group_findings(
        _pages([("Julie Summers", "PERSON"), ("Julia Summers", "PERSON")])
    )
    assert len(grouping.groups) == 2


def test_grouping_is_independent_of_the_order_values_arrive_in():
    forward = group_findings(
        _pages([("SMITH JOHN", "PERSON"), ("Smith John", "PERSON"),
                ("014-936", "IDENTIFIER_GENERIC")])
    )
    backward = group_findings(
        _pages([("014-936", "IDENTIFIER_GENERIC"), ("Smith John", "PERSON"),
                ("SMITH JOHN", "PERSON")])
    )
    assert [g.entity_type for g in forward.groups] == [
        g.entity_type for g in backward.groups
    ]
    assert [_texts(g) for g in forward.groups] == [
        _texts(g) for g in backward.groups
    ]


# --------------------------------------------------------------------------
# The vote
# --------------------------------------------------------------------------


def test_the_vote_counts_individual_detections_not_surface_forms():
    # One form seen four times outweighs another seen once, even though there
    # are only two distinct strings.
    grouping = group_findings(
        _pages(
            [("ACME PTY LTD", "ORGANIZATION")],
            [("ACME PTY LTD", "ORGANIZATION")],
            [("ACME PTY LTD", "ORGANIZATION")],
            [("Acme Pty Ltd", "PERSON")],
        )
    )
    (group,) = grouping.groups
    assert group.entity_type == "ORGANIZATION"
    assert group.votes == (("ORGANIZATION", 3), ("PERSON", 1))


def test_the_majority_wins_in_the_un_redacting_direction_too():
    # Deliberate (Sergei, 2026-08-11): if PII_COMPANY wins 10-to-1 the odds
    # are it is a company. The page that read a person is relabelled with the
    # rest — which is why the tally is reported.
    pages = [[("BUDGET DIRECT", "ORGANIZATION")] for _ in range(10)]
    pages.append([("BUDGET DIRECT", "PERSON")])
    (group,) = group_findings(_pages(*pages)).groups
    assert group.entity_type == "ORGANIZATION"


def test_a_tie_goes_to_class_priority_never_to_the_kept_class():
    # ORGANIZATION is the one class layer 0 emits that is KEPT by default, so
    # a tie must not hand it the group.
    (group,) = group_findings(
        _pages([("Kulik Holdings", "ORGANIZATION")],
               [("Kulik Holdings", "PERSON")])
    ).groups
    assert group.entity_type == "PERSON"
    assert CLASS_PRIORITY.index("PERSON") < CLASS_PRIORITY.index("ORGANIZATION")


def test_an_unknown_class_ranks_last_but_still_resolves():
    (group,) = group_findings(
        _pages([("something", "PERSON")], [("something", "WEIRD_TYPE")])
    ).groups
    assert group.entity_type == "PERSON"


# --------------------------------------------------------------------------
# The view the rest of the pipeline consumes
# --------------------------------------------------------------------------


def test_variants_keep_their_original_text_and_their_pages():
    grouping = group_findings(
        _pages(
            [("SMITH JOHN", "PERSON")],
            [("Smith John", "PERSON"), ("SMITH JOHN", "PERSON")],
        )
    )
    (group,) = grouping.groups
    by_text = {v.text: v for v in group.variants}
    assert by_text["SMITH JOHN"].count == 2
    assert by_text["SMITH JOHN"].pages == (1, 2)
    assert by_text["Smith John"].pages == (2,)
    assert group.pages == (1, 2)
    assert group.count == 3


def test_needles_carry_the_elected_class_longest_first():
    grouping = group_findings(
        _pages([("John Smith", "PERSON"), ("John", "ORGANIZATION")])
    )
    needles = grouping.needles()
    # Longest first, so a wider value claims a contested span before a
    # narrower one nested in it.
    assert [n.text for n in needles] == ["John Smith", "John"]
    assert all(n.entity_type == grouping.type_for(n.text) for n in needles)
    # A layer-0 needle is a value the model READ, so every tier is admissible
    # for it — unlike layer 1's, which are restricted (locator.Needle).
    assert all(n.tiers == ALL_TIERS for n in needles)


def test_type_for_is_none_for_a_value_no_page_reported():
    grouping = group_findings(_pages([("John Smith", "PERSON")]))
    assert grouping.type_for("John Smith") == "PERSON"
    assert grouping.type_for("Jane Doe") is None


def test_no_findings_is_an_empty_grouping():
    grouping = group_findings([[], []])
    assert grouping.groups == ()
    assert grouping.needles() == ()

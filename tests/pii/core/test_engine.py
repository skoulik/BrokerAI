"""The detection engine: scoring semantics, context boost, deduplication.

These pin the three Presidio behaviours every score in `recognizers.py` was
tuned against (module docstring in `pii/core/engine.py`). They are the reason
the chassis swap could keep those scores unchanged.
"""

import pytest

from pii.core.detection import Detection
from pii.core.engine import (
    CONTEXT_BOOST,
    CONTEXT_FLOOR,
    CONTEXT_WINDOW_CHARS,
    STRICT,
    Analyzer,
    Pattern,
    PatternRule,
    TextLayout,
)


class _Digits(PatternRule):
    entity = "TEST"
    patterns = (Pattern("digits", r"\d+", 0.15),)


def _rule(cls, **attrs):
    """One-off rule subclass with attributes overridden."""
    return type("_Tmp", (cls,), attrs)()


# ------------------------------------------------------- validation semantics


def test_validate_none_keeps_the_pattern_score():
    found = _Digits().detect("value 12345")
    assert [(d.entity_type, d.score) for d in found] == [("TEST", 0.15)]


def test_validate_true_promotes_to_full_confidence():
    """A passing checksum is proof — the pattern's own (deliberately low)
    score must not cap it."""
    rule = _rule(_Digits, validate=lambda self, m: True)
    assert rule.detect("12345")[0].score == 1.0


def test_validate_false_drops_the_match_entirely():
    rule = _rule(_Digits, validate=lambda self, m: False)
    assert rule.detect("12345") == []


def test_span_is_the_match_not_the_pattern():
    found = _Digits().detect("ab 12345 cd")
    assert (found[0].start, found[0].end) == (3, 8)


def test_detection_records_its_rule():
    """`recognizer` is load-bearing: invalid-candidate suppression keys on
    it, so that an unvalidated guess can never silence a finding."""
    assert _Digits().detect("12345")[0].recognizer == "_Digits"


# ------------------------------------------------------------ context boost


def test_context_word_promotes_a_subthreshold_match():
    rule = _rule(_Digits, context=("account",))
    found = Analyzer([rule]).analyze("account 12345", threshold=0.4)
    assert len(found) == 1
    assert found[0].score == pytest.approx(max(0.15 + CONTEXT_BOOST, CONTEXT_FLOOR))


def test_without_context_the_same_match_is_dropped():
    rule = _rule(_Digits, context=("account",))
    assert Analyzer([rule]).analyze("reference 12345", threshold=0.4) == []


def test_context_matches_as_a_substring_through_punctuation():
    """The whole point of matching characters rather than spaCy lemmas: 'a/c'
    and 'TFN:' never surfaced as tokens, so the label never reached the
    enhancer (2026-07-15 source review)."""
    rule = _rule(_Digits, context=("acct",))
    assert Analyzer([rule]).analyze("Acct.No:12345", threshold=0.4)


def test_context_only_looks_backwards():
    """Presidio looked 5 tokens back and 0 forward; a trailing label must not
    promote."""
    rule = _rule(_Digits, context=("account",))
    assert Analyzer([rule]).analyze("12345 account", threshold=0.4) == []


def test_context_window_is_bounded():
    rule = _rule(_Digits, context=("account",))
    far = "account" + " " * (CONTEXT_WINDOW_CHARS + 10) + "12345"
    assert Analyzer([rule]).analyze(far, threshold=0.4) == []


def test_boost_never_exceeds_one():
    rule = _rule(_Digits, context=("account",),
                 patterns=(Pattern("d", r"\d+", 0.9),))
    found = Analyzer([rule]).analyze("account 12345", threshold=0.4)
    assert found[0].score == 1.0


# --------------------------------------------------------------- the analyzer


def test_threshold_filters_by_score():
    rule = _rule(_Digits, patterns=(Pattern("d", r"\d+", 0.3),))
    assert Analyzer([rule]).analyze("12345", threshold=0.4) == []
    assert Analyzer([rule]).analyze("12345", threshold=0.3)


def test_identical_spans_collapse_to_the_highest_score():
    rule = _rule(
        _Digits,
        patterns=(Pattern("low", r"\d+", 0.5), Pattern("high", r"\d+", 0.8)),
    )
    found = Analyzer([rule]).analyze("12345", threshold=0.4)
    assert len(found) == 1
    assert found[0].score == 0.8


def test_results_are_sorted_by_position():
    found = Analyzer([_Digits()]).analyze("11 22 33", threshold=0.1)
    assert [d.start for d in found] == [0, 3, 6]


def test_rules_are_reachable_by_name():
    analyzer = Analyzer([_Digits()])
    assert analyzer.rule("_Digits") is not None
    assert analyzer.rule("nope") is None


# ------------------------------------------------------- label attachment
#
# The 2026-08-14 replacement for the flat character window. `WindowLayout` is
# still the default and the tests above still pin it; these pin what replaces
# it. Design and rationale: `pii/core/TODO.md`.


class _Labelled(PatternRule):
    """A sub-threshold digit run that only a label can promote."""

    entity = "TEST"
    context = ("account",)
    patterns = (Pattern("digits", r"\d+", 0.15),)


def _analyze(rule, text, threshold=0.4):
    return Analyzer([rule]).analyze(text, threshold, TextLayout(text))


def test_a_label_on_the_previous_line_does_not_reach():
    """The specimen in miniature: `013795` sat at the start of its own line and
    was promoted by the `Account Number` label of the CARD one line up."""
    text = "Account Number 4564 9427 0001 0443\n013795 Statement"
    found = {d.start for d in _analyze(_Labelled(), text)}
    assert 15 in found, found  # the label's OWN value still attaches
    assert text.index("013795") not in found, found


def test_a_left_margin_label_reaches_a_right_aligned_value():
    """The dominant statement layout: the gap is arbitrarily wide, so the band
    is sized in WORDS, not characters."""
    text = "ACCOUNT STATEMENT" + " " * 40 + "Account Number   :   12345678"
    found = _analyze(_Labelled(), text)
    assert [round(d.score, 2) for d in found] == [0.5]


def test_the_word_floor_comes_from_the_rules_own_longest_label():
    rule = _rule(_Labelled, context=("australian financial services licence",))
    assert rule.label_words == 4 + 2
    assert _rule(_Labelled, context=("tfn",)).label_words == 1 + 2


# -- strict


def _strict(**attrs):
    return _rule(
        _Labelled,
        # A STRICT pattern must stand above threshold on its own: it is
        # gated by its label, not promoted by it.
        patterns=(Pattern("digits", r"\d+", 0.5, attach=STRICT),),
        **attrs,
    )


def test_strict_accepts_separators_and_fillers_between_label_and_value():
    for text in ("Account 12345678", "Account No. 12345678", "Account #: 12345678"):
        assert [d.start for d in _analyze(_strict(), text)], text


def test_strict_rejects_a_word_between_the_label_and_the_value():
    """`Account enquiries 13 22 66` is the failure this prevents: the label is
    present, but it is not what introduces the number."""
    assert _analyze(_strict(), "Account enquiries 12345678") == []


def test_strict_drops_an_unattached_match_that_near_would_merely_not_boost():
    """A STRICT pattern is a GATE, exactly as the lookbehind it replaces was —
    not a boost that happens to fall below threshold."""
    text = "reference 12345678"
    assert _analyze(_strict(), text) == []
    assert _analyze(_strict(), text, threshold=0.1) == []
    assert [d.score for d in _analyze(_Labelled(), text, threshold=0.1)] == [0.15]


def test_near_tolerates_a_word_between_where_strict_does_not():
    """The ONLY difference between the strengths, once the word floor has
    bounded the distance for both: whether the gap may say something of its
    own. `for` is not a filler, so strict declines and near does not."""
    text = "Account for 12345678"
    assert [round(d.score, 2) for d in _analyze(_Labelled(), text)] == [0.5]
    assert _analyze(_strict(), text) == []


def test_an_attached_strict_pattern_keeps_its_own_score():
    """Score neutrality, and it is what makes stage 4 safe: a lookbehind
    becomes a STRICT pattern without any score moving. The label is the gate,
    not a promotion, so boosting on top would double-count it."""
    found = _analyze(_strict(), "Account No. 12345678")
    assert found[0].score == 0.5
    assert found[0].attachment.term == "Account"


def test_a_strict_pattern_without_labels_cannot_be_constructed():
    """A gate with nothing to open it would match nothing, silently."""
    with pytest.raises(ValueError):
        _strict(context=())


# -- the audit record


def test_the_attachment_records_which_label_promoted_the_span():
    text = "Account Number   :   12345678"
    # The label is reported as the page SPELLS it, not as the rule declares
    # it: the audit surface should show what a reader would see.
    found = _analyze(_Labelled(), text)[0]
    assert found.attachment.term == "Account"
    assert found.attachment.relation == "left"
    assert text[found.attachment.start : found.attachment.end] == "Account"


def test_the_nearest_label_wins():
    """On `BSB 013 795 Account 12345678` both labels are in the band and only
    one introduces the value."""
    rule = _rule(_Labelled, context=("bsb", "account"))
    found = _analyze(rule, "BSB Account 12345678")[0]
    assert found.attachment.term == "Account"


def test_an_unattached_near_match_records_no_attachment():
    found = _analyze(_Labelled(), "reference 12345678", threshold=0.1)[0]
    assert found.attachment is None


# ------------------------------------------------------------------ detection


def test_inverted_span_is_rejected():
    with pytest.raises(ValueError):
        Detection(entity_type="X", start=5, end=1, score=1.0)

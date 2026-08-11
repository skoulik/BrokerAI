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
    Analyzer,
    Pattern,
    PatternRule,
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


# ------------------------------------------------------------------ detection


def test_inverted_span_is_rejected():
    with pytest.raises(ValueError):
        Detection(entity_type="X", start=5, end=1, score=1.0)

"""One rule per checksummed identifier: valid and invalid from one pattern set.

The merge that produced `ChecksumRule` (2026-08-09) closed a live leak, and
the first test here is that leak. Presidio owned the valid classes with
SPACE-only patterns while our shadows owned the invalid ones with `[- ]`, so a
hyphen-grouped **valid** identifier matched Presidio not at all and was dropped
by the shadow for passing its checksum — detected by nothing. The eval corpus
could not see it: `pii_eval/au.py` only ever emits space-grouped forms, which
is why the corpus probe added alongside these tests generates hyphenated ones.
"""

import pytest

from pii.core.engine import Analyzer
from pii.core.recognizers import (
    INVALID_ENTITY_TYPES,
    AuAbnRule,
    AuAcnRule,
    AuMedicareRule,
    AuTfnRule,
    CreditCardRule,
    build_rules,
)

# (rule, valid value, the same value with one digit changed)
CASES = [
    (AuTfnRule, "AU_TFN", "123 456 782", "123 456 780"),
    (AuAbnRule, "AU_ABN", "51 824 753 556", "51 824 753 557"),
    (AuAcnRule, "AU_ACN", "004 085 616", "004 085 617"),
    # NB the last digit is the IRN, not the check digit (that is the 9th),
    # so the typo has to land inside the card number to fail the checksum.
    (AuMedicareRule, "AU_MEDICARE", "2123 45670 1", "2123 45671 1"),
]


def _types(rule, text):
    return {d.entity_type for d in rule.detect(text)}


@pytest.mark.parametrize("rule_cls,entity,valid,_invalid", CASES)
def test_hyphen_grouped_valid_identifier_is_detected(rule_cls, entity, valid, _invalid):
    """THE REGRESSION. Before the merge this was detected by nothing at all:
    the valid recognizer's pattern accepted spaces only, and the shadow that
    did match hyphens dropped it for passing its checksum."""
    hyphenated = valid.replace(" ", "-")
    assert entity in _types(rule_cls(), hyphenated), hyphenated


@pytest.mark.parametrize("rule_cls,entity,valid,_invalid", CASES)
def test_space_grouped_and_bare_forms_still_detected(rule_cls, entity, valid, _invalid):
    assert entity in _types(rule_cls(), valid)
    assert entity in _types(rule_cls(), valid.replace(" ", ""))


@pytest.mark.parametrize("rule_cls,entity,_valid,invalid", CASES)
def test_checksum_failure_emits_the_shadow_not_the_class(
    rule_cls, entity, _valid, invalid
):
    found = _types(rule_cls(), invalid)
    assert entity not in found
    assert found & INVALID_ENTITY_TYPES


@pytest.mark.parametrize("rule_cls,entity,valid,_invalid", CASES)
def test_one_rule_partitions_the_digit_space(rule_cls, entity, valid, _invalid):
    """The property the merge exists to guarantee: for any value the rule
    matches, exactly one of {valid class, invalid shadow} fires — never both,
    never neither."""
    rule = rule_cls("all")
    for form in (valid, valid.replace(" ", "-"), valid.replace(" ", "")):
        found = _types(rule, form)
        assert (entity in found) ^ bool(found & INVALID_ENTITY_TYPES), form


def test_label_is_evidence_not_part_of_the_value():
    """A label inside the span keys the pseudonym map on a different string
    than a bare occurrence, forking one identifier into TFN_1 and TFN_2."""
    rule = AuTfnRule()
    spans = {
        (d.start, d.end)
        for d in rule.detect("TFN: 123 456 782")
        if d.entity_type == "AU_TFN"
    }
    assert spans == {(5, 16)}  # the digits only, never "TFN: "


def test_labeled_form_is_still_what_makes_it_in_span_evidence():
    """The label promotes an unspaced run that no grouping would mark: at the
    `likely` tier a bare invalid run is not collected, a labeled one is."""
    likely = AuTfnRule("likely")
    assert _types(likely, "reference 123456780") == set()
    assert "AU_TFN_INVALID" in _types(likely, "TFN 123456780")


def test_luhn_card_valid_and_invalid():
    rule = CreditCardRule()
    assert "CREDIT_CARD" in _types(rule, "4111 1111 1111 1111")
    assert "CREDIT_CARD_INVALID" in _types(rule, "4111 1111 1111 1112")


def test_tier_governs_only_the_invalid_branch():
    """Every tier detects valid identifiers; they differ only in which failing
    candidates they collect."""
    for tier in ("ignore", "likely", "context", "all"):
        found = Analyzer(build_rules(tier)).analyze("TFN 123 456 782", 0.4)
        assert "AU_TFN" in {d.entity_type for d in found}, tier


def test_bare_invalid_run_needs_the_all_tier():
    assert _types(AuTfnRule("likely"), "ref 123456780") == set()
    assert _types(AuTfnRule("all"), "ref 123456780") == {"AU_TFN_INVALID"}


def test_wrong_digit_count_matches_nothing():
    """The domain check keeps a rule off values that are not its shape at
    all — an 8-digit run is not a failed TFN, it is not a TFN."""
    assert _types(AuTfnRule("all"), "ref 12345678") == set()

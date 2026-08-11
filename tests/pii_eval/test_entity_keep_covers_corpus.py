"""The eval's keep list must name every merchant the generator emits.

Since 2026-08-11 an organization survives only by being on a keep list, so the
scorers run against the corpus's own list (pii_eval/entity_keep.txt) rather
than the shipped one. That list is hand-written, and the generator's merchant
pool is not: without this test a new merchant would be silently over-stripped
and read as a regression in the tool rather than a gap in the fixture."""

import pytest

from pii.core.entity_keep import load_keep
from pii_eval.build import CORPUS_KEEP_FILE
from pii_eval.personas import MERCHANTS


@pytest.fixture(scope="module")
def corpus_keep():
    return load_keep(CORPUS_KEEP_FILE)


@pytest.mark.parametrize("merchant", MERCHANTS)
def test_every_generated_merchant_is_kept(corpus_keep, merchant):
    assert corpus_keep.keeps("ORGANIZATION", merchant) is True


def test_suffixed_narrative_forms_are_kept(corpus_keep):
    # The generator also emits "<MERCHANT> <TOWN>" and "EFTPOS <MERCHANT> 1234"
    # forms; matching is substring-based so the bare token covers them.
    assert corpus_keep.keeps("ORGANIZATION", "WOOLWORTHS NEWTOWN") is True
    assert corpus_keep.keeps("ORGANIZATION", "EFTPOS COLES EXPRESS 4821 AU") is True
    assert corpus_keep.keeps("ORGANIZATION", "BUDGET DIRECT INSURANCE") is True


def test_account_holder_entities_are_not_kept(corpus_keep):
    # The other half of the axis: the corpus's private entities must NOT be
    # rescued by the fixture, or the recall side would score against a list
    # that quietly exempts what it is supposed to catch.
    for name in ("SK BUSINESS TRUST", "SK BUSINESS TRUS",
                 "OAKFIELD CONSULTING PTY LTD", "Kulik Family Trust"):
        assert corpus_keep.keeps("ORGANIZATION", name) is False

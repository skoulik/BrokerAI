"""Pattern-layer pipeline behaviour and overlap merging (no NER)."""

from presidio_analyzer import RecognizerResult

from pii.mapping import PseudonymMap
from pii.pipeline import _merge_overlaps

# Checksum-valid literals (pii_eval.au generators, fixed seeds).
VALID_TFN = "291 417 774"
VALID_CARD = "4783 5337 4068 1247"


def _rr(etype, start, end, score):
    return RecognizerResult(entity_type=etype, start=start, end=end, score=score)


def test_strip_valid_tfn(pipeline):
    text = f"Tax file number: {VALID_TFN} on record."
    out, spans, _ = pipeline.strip(text, PseudonymMap())
    assert VALID_TFN not in out
    assert "TFN_1" in out
    assert any(s.entity_type == "AU_TFN" for s in spans)


def test_strip_labeled_account(pipeline):
    # "A/C" never survives tokenization as a context word; the labeled
    # pattern matches it in-span (recall-first, label lands in placeholder).
    out, _, _ = pipeline.strip("Interest Charged From A/C 7412154728", PseudonymMap())
    assert "7412154728" not in out
    assert "ACCOUNT_1" in out


def test_strip_credit_card(pipeline):
    out, _, _ = pipeline.strip(f"Card for repayments: {VALID_CARD}", PseudonymMap())
    assert VALID_CARD not in out
    assert "CARD_1" in out


def test_strip_email(pipeline):
    out, _, _ = pipeline.strip("PAYID PAYMENT FROM olga@example.com", PseudonymMap())
    assert "olga@example.com" not in out
    assert "EMAIL_1" in out


def test_consistent_placeholders_across_calls(pipeline):
    pmap = PseudonymMap()
    out1, _, _ = pipeline.strip(f"TFN: {VALID_TFN}", pmap)
    out2, _, _ = pipeline.strip(f"quoted TFN {VALID_TFN} again", pmap)
    assert "TFN_1" in out1 and "TFN_1" in out2


def test_merge_overlaps_unions_extents_higher_score_wins_type():
    # A small high-score span must not evict the wider covering span —
    # extents union, label follows the higher score.
    merged = _merge_overlaps(
        [_rr("AU_BANK_ACCOUNT", 0, 20, 0.52), _rr("AU_BSB", 0, 7, 0.55)]
    )
    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (0, 20)
    assert merged[0].entity_type == "AU_BSB"


def test_merge_overlaps_keeps_disjoint_spans():
    merged = _merge_overlaps(
        [_rr("AU_TFN", 0, 11, 1.0), _rr("EMAIL_ADDRESS", 20, 35, 1.0)]
    )
    assert len(merged) == 2


def test_merge_overlaps_chains_adjacent_overlaps():
    merged = _merge_overlaps(
        [_rr("A", 0, 10, 0.5), _rr("B", 8, 15, 0.6), _rr("C", 14, 30, 0.4)]
    )
    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (0, 30)
    assert merged[0].entity_type == "B"

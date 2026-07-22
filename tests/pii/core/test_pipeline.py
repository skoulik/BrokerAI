"""Pattern-layer pipeline behaviour and overlap merging (no NER)."""

from presidio_analyzer import RecognizerResult

from pii.core.mapping import PseudonymMap
from pii.core.pipeline import _merge_overlaps
from pii.core.recognizers import AuAccountNumberRecognizer

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


def test_strip_labeled_account_au_forms(pipeline):
    # The a/c label family in its popular Australian spellings, with
    # space-grouped digits (2026-07-14; Sergei: a/c, A/C, AC, Ac., Ac: are
    # all common on real statements).
    for label in ("a/c", "A/C", "A/c.", "Ac.", "Ac:", "AC", "acct", "acc"):
        out, _, _ = pipeline.strip(
            f"Salary {label} 1234 5678 credited", PseudonymMap()
        )
        assert "1234 5678" not in out, label


def test_strip_spaced_account_with_context_word(pipeline):
    # Bare space-grouped digits promoted by an account context word.
    out, _, _ = pipeline.strip("Account Number : 0007 3111 4", PseudonymMap())
    assert "0007 3111 4" not in out


def test_year_range_near_account_word_kept(pipeline):
    # The grouped pattern's lookahead spares year ranges even when the word
    # 'account' would otherwise promote them past the threshold.
    out, _, _ = pipeline.strip(
        "account statement period 2023 2024", PseudonymMap()
    )
    assert "2023 2024" in out


def test_grouped_digits_without_account_context_kept(pipeline):
    # No account label / context word: sub-threshold, stays put.
    out, _, _ = pipeline.strip(
        "invoice 1234 5678 and 9012 3456", PseudonymMap()
    )
    assert "1234 5678" in out and "9012 3456" in out


def test_account_digit_floor_rejects_fragments(pipeline):
    # validate_result: fewer than 5 digits in total is never an account,
    # even directly behind an a/c label.
    out, _, _ = pipeline.strip("ref a/c 12 34 only", PseudonymMap())
    assert "12 34" in out


def test_transaction_amount_columns_not_account(pipeline):
    # Issue #3: the 'account grouped' pattern matched the decimal-fraction of
    # one amount + the integer start of the next across the column gap
    # ('2,148.74 377,970.04DR' -> '74 377'), promoted past threshold by the
    # nearby 'LOAN'/'PAYMENT' context word. Formatted-number fragments are
    # now excluded, so the whole transaction line survives intact.
    # Both lines carry an account context word (LOAN/PAYMENT) so the fragment
    # WOULD be promoted past threshold without the guard.
    for text in (
        "03 APR LOAN PAYMENT 2,148.74 377,970.04DR",
        "24 APR LOAN TRANSFER 2,206.74 375,705.30DR",
    ):
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert out == text, text


def test_labeled_account_pattern_releases_trailing_amount():
    # Issue #11: the 'labeled account' pattern lacked the issue-#3 amount
    # guard, so its grouped alternative ate the integer part of a following
    # amount ('A/C 30-743-3257 148.74CR' -> 'A/C 30-743-3257 148',
    # 'A/C ... 1.50' -> 'A/C ... 1'). Recognizer-level: no emitted span may
    # reach past the account itself.
    rec = AuAccountNumberRecognizer()
    for amount in ("148.74CR", "1.50"):
        text = f"A/C 32-151-6825 {amount}"
        spans = rec.analyze(text, ["AU_BANK_ACCOUNT"])
        assert spans, text
        assert max(r.end for r in spans) <= len("A/C 32-151-6825"), (
            [text[r.start:r.end] for r in spans]
        )


def test_labeled_account_releases_trailing_amount_e2e(pipeline):
    # Pipeline-level: the account strips in full (the guard only backtracks
    # the amount group off) and the amount survives. Both amount shapes:
    # '1.50' additionally exercises the issue-#11 follow-up — with US phone
    # regions libphonenumber read '32-151-6825 1' as a valid US number
    # (3215168251) and _merge_overlaps draped that span over the amount;
    # AU-only regions keep PhoneRecognizer silent here.
    for amount in ("148.74CR", "1.50"):
        text = f"Interest Charged From A/C 32-151-6825 {amount}"
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert "32-151-6825" not in out, out       # account stripped
        assert "A/C" not in out, out               # label in-span, stripped
        assert out.endswith(f" {amount}"), out     # amount released intact


def test_phone_au_only_regions_keep_all_real_forms(pipeline):
    # The AU-only sacrifice must not touch the forms that actually occur:
    # AU 13-numbers/mobiles and international '+'-prefixed numbers (parsed
    # region-independently, so foreign contacts still strip).
    for number in ("13 22 65", "0412 345 678", "+613 8536 7870",
                   "+1 305 555 0123"):
        out, _, _ = pipeline.strip(f"Contact {number} today", PseudonymMap())
        assert number not in out, out


def test_labeled_account_without_amount_still_full_match(pipeline):
    # The guard must not truncate a genuinely grouped trailing segment
    # (nothing distinguishes it from a 4-group account when no decimal
    # follows) nor break the contiguous/sentence-final forms.
    for account in ("30-743-3257 148", "7412154728", "1234 5678"):
        out, _, _ = pipeline.strip(f"From A/C {account}.", PseudonymMap())
        assert account not in out, out


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


def test_kept_org_does_not_shield_nested_address(pipeline, monkeypatch):
    # The 2026-07-14 image-demo wart, pinned: detect() filters kept-type
    # spans out BEFORE overlap merging (a kept span must never shadow PII),
    # so an ADDRESS nested inside a kept ORGANIZATION still strips and the
    # merchant name loses its suburb. The eval corpus measures the same
    # wart on the over-strip axis (suburb-suffixed merchants). If the
    # overlaps-merging task (pii/TODO.md) changes this policy, update the
    # test and the corpus expectation together.
    text = "EFTPOS WOOLWORTHS NEWTOWN 4821 AU"
    results = [
        _rr("ORGANIZATION", 7, 25, 0.95),  # WOOLWORTHS NEWTOWN (kept type)
        _rr("ADDRESS", 18, 25, 0.6),       # NEWTOWN
    ]
    monkeypatch.setattr(pipeline.analyzer, "analyze", lambda **kw: results)
    out, _, _ = pipeline.strip(text, PseudonymMap())
    assert out == "EFTPOS WOOLWORTHS ADDRESS_1 4821 AU"


def test_private_org_stripped_institution_and_merchant_kept(pipeline, monkeypatch):
    # ORGANIZATION is kept by default, EXCEPT account-holder private entities
    # (legal-form marker, not a known institution) — org_policy. The strip
    # keeps the ORG_n placeholder (issue #2/#5).
    text = "ACCOUNT OF SK BUSINESS TRUST at ANZ paid WOOLWORTHS"
    results = [
        _rr("ORGANIZATION", 11, 28, 0.78),  # SK BUSINESS TRUST -> strip
        _rr("ORGANIZATION", 32, 35, 0.97),  # ANZ (keep-listed) -> keep
        _rr("ORGANIZATION", 41, 51, 0.95),  # WOOLWORTHS (no marker) -> keep
    ]
    monkeypatch.setattr(pipeline.analyzer, "analyze", lambda **kw: results)
    out, _, _ = pipeline.strip(text, PseudonymMap())
    assert "SK BUSINESS TRUST" not in out
    assert "ORG_1" in out
    assert "ANZ" in out and "WOOLWORTHS" in out


def test_strip_orgs_forces_all_including_institutions(make_pipeline, monkeypatch):
    # --strip-orgs (ORGANIZATION in strip_entities) overrides the private-
    # entity policy: every org, institutions included, is stripped.
    from pii.core import DEFAULT_STRIP_ENTITIES

    pipeline = make_pipeline(
        strip_entities=set(DEFAULT_STRIP_ENTITIES) | {"ORGANIZATION"}
    )
    text = "paid ANZ and WOOLWORTHS today"
    results = [
        _rr("ORGANIZATION", 5, 8, 0.97),    # ANZ
        _rr("ORGANIZATION", 13, 23, 0.95),  # WOOLWORTHS
    ]
    monkeypatch.setattr(pipeline.analyzer, "analyze", lambda **kw: results)
    out, _, _ = pipeline.strip(text, PseudonymMap())
    assert "ANZ" not in out and "WOOLWORTHS" not in out


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

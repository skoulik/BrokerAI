"""Pattern-layer pipeline behaviour and overlap merging (no NER)."""

from pii.core.detection import Detection

from pii.core.mapping import PseudonymMap
from pii.core.pipeline import _merge_overlaps
from pii.core.recognizers import AuAccountNumberRule

# Checksum-valid literals (pii_eval.au generators, fixed seeds).
VALID_TFN = "291 417 774"
VALID_CARD = "4783 5337 4068 1247"


def _rr(etype, start, end, score):
    return Detection(entity_type=etype, start=start, end=end, score=score)


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
    rule = AuAccountNumberRule()
    for amount in ("148.74CR", "1.50"):
        text = f"A/C 32-151-6825 {amount}"
        spans = rule.detect(text)
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
    # AU-only regions keep the phone rule silent here.
    for amount in ("148.74CR", "1.50"):
        text = f"Interest Charged From A/C 32-151-6825 {amount}"
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert "32-151-6825" not in out, out       # account stripped
        assert "A/C" not in out, out               # label in-span, stripped
        assert out.endswith(f" {amount}"), out     # amount released intact


def test_bsb_account_combined_splits_into_two_spans(pipeline):
    # Issue #8b: '014-936 111873883' used to emit ONE span labeled AU_BSB —
    # the account half hid under a BSB_n placeholder and a bare
    # '111873883' elsewhere aliased to a DIFFERENT placeholder. Split
    # patterns now give each half its own span/placeholder, and the bare
    # re-mention reuses the account's.
    text = ("BSB Cash Account Number 014-936 111873883 and later "
            "account 111873883 again")
    pmap = PseudonymMap()
    out, _, _ = pipeline.strip(text, pmap)
    assert "014-936" not in out and "111873883" not in out, out
    assert "BSB_1" in out and "ACCOUNT_1" in out, out
    # the context-promoted re-mention aliases to the SAME placeholder —
    # the point of the split (one BSB_n-covered blob can't do this)
    assert out.count("ACCOUNT_1") == 2, out


def test_atf_tail_stripped_including_truncated_forms(pipeline):
    # Issue #9: '<company> ATF <trust>' — the doc truncates the field
    # mid-word ('ATF SK BU', '... SK BUSINESS TRU'), defeating NER
    # confidence; the layer-1 ATF-tail pattern covers the clause to
    # end-of-line regardless, and no keep list names it so it strips. The
    # next line must stay untouched.
    for tail in ("ATF SK BU", "ATF SK BUSINESS TRU",
                 "as trustees for THE KULIK FAMILY TRUST"):
        text = f"ACCOUNT NAME PTY LTD {tail}\nStatement starts 22 February"
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert tail not in out, out
        assert "ORG_" in out, out
        assert out.endswith("Statement starts 22 February"), out


def test_corporate_licence_numbers_strip_under_their_own_classes(pipeline):
    # Issue #8c / other-finding #1: AFSL and Australian Credit Licence numbers
    # are public corporate identifiers, KEPT until 2026-08-14 and stripped
    # since (Sergei, "for now"). They keep their own classes either way, so a
    # report still discriminates them from AU_DRIVERS_LICENCE and the reversal
    # stays an operator keep-list section rather than a code change.
    text = ("ANZ ABN 11 005 357 522. Australian Credit Licence 234527. "
            "Advice under AFSL 233714.")
    detections = {
        (r.entity_type, text[r.start:r.end]) for r in pipeline.analyze(text)
    }
    assert ("AU_CREDIT_LICENCE", "234527") in detections, detections
    assert ("AU_AFSL", "233714") in detections, detections
    out, _, _ = pipeline.strip(text, PseudonymMap())
    assert "234527" not in out, out
    assert "233714" not in out, out
    # The LABEL is evidence, not part of the value: it is matched as a
    # lookbehind, so it survives and the map keys on the bare number. A span
    # covering "AFSL 233714" would fork one licence into AFSL_1 and AFSL_2 the
    # moment the same number appeared unlabelled.
    assert "Australian Credit Licence ACL_1" in out, out
    assert "AFSL AFSL_1" in out, out


def test_afsl_matches_the_half_abbreviated_label_a_real_footer_prints(pipeline):
    # `AFS` abbreviated with `Licence` spelled out is what an insurance
    # certificate footer actually prints, and it matched NOTHING until
    # 2026-08-14: the acronym was the only spelling this test and the pii_eval
    # probe ever exercised, so neither could see the gap. The first two numbers
    # are the real specimen (116832820_7_Insurance_Certificate.pdf p2, where
    # layer 1 found neither); the other two pin the spellings that already
    # worked, so widening the alternation cannot cost them.
    text = ("Auto & General Insurance Company Limited AFS Licence No 285571. "
            "Auto & General Services Pty Ltd AFS Licence 241411. "
            "Underwritten under AFS Lic 447985 and AFS Lic. No 511823. "
            "Advice under AFSL 233714. "
            "Australian Financial Services Licence No 244616.")
    numbers = ("285571", "241411", "447985", "511823", "233714", "244616")
    detections = {
        (r.entity_type, text[r.start:r.end]) for r in pipeline.analyze(text)
    }
    for number in numbers:
        assert ("AU_AFSL", number) in detections, (number, detections)
    # The label is evidence, not value, in the new spellings too — the digits
    # go and the words stay, or one licence forks into AFSL_1 and AFSL_2.
    out, _, _ = pipeline.strip(text, PseudonymMap())
    for number in numbers:
        assert number not in out, out
    assert "AFS Licence No AFSL_" in out, out
    assert "AFS Licence AFSL_" in out, out
    assert "AFS Lic AFSL_" in out, out
    assert "AFS Lic. No AFSL_" in out, out


def test_a_service_line_labelled_enquiries_types_as_a_phone(pipeline):
    """`enquir` earns its place by DECIDING a collision, not by detecting.

    A bank's service number is also a grouped digit run, so
    AuAccountNumberRule matches it and the word "Account" beside it promotes
    that candidate to 0.5 — above PhoneRule's flat 0.4. Whichever scores
    higher takes the span, so without a phone label of its own the service
    line strips as an account number. Third case: with no phone label at all
    the account candidate SHOULD win, which is what makes this a label test
    rather than a thumb on the scale.
    """
    for text in ("Account enquiries 13 22 66", "Statement Enquiries 13 22 66"):
        found = {(d.entity_type, text[d.start:d.end]) for d in pipeline.analyze(text)}
        assert found == {("PHONE_NUMBER", "13 22 66")}, (text, found)
    bare = "Account 13 22 66"
    found = {(d.entity_type, bare[d.start:d.end]) for d in pipeline.analyze(bare)}
    assert found == {("AU_BANK_ACCOUNT", "13 22 66")}, found


def test_credit_licence_abbreviates_its_label_like_its_afsl_sibling(pipeline):
    # The siblings label the same kind of number in the same kind of footer, so
    # a spelling one accepts and the other does not is a gap waiting to be
    # found on a document. `Lic` is anchored to `credit` here exactly as it is
    # to `afs` there, so a bare `lic` elsewhere in a footer still matches
    # nothing.
    text = ("Broking under Credit Lic 234527, Australian Credit Lic. No 682144, "
            "Australian Credit Licence 387892, ACL 917392.")
    numbers = ("234527", "682144", "387892", "917392")
    detections = {
        (r.entity_type, text[r.start:r.end]) for r in pipeline.analyze(text)
    }
    for number in numbers:
        assert ("AU_CREDIT_LICENCE", number) in detections, (number, detections)
    out, _, _ = pipeline.strip(text, PseudonymMap())
    for number in numbers:
        assert number not in out, out
    assert "Credit Lic ACL_" in out, out
    assert "Australian Credit Lic. No ACL_" in out, out


def test_a_corporate_licence_is_reversible_by_the_keep_list(tmp_path):
    """The stated escape hatch for "reconsidered later" — an operator section,
    no code change. Written through the real file path, since that is what an
    operator would actually do."""
    from pii.core import PiiPipeline
    from pii.core.entity_keep import load_keep

    keep = tmp_path / "keeps.txt"
    keep.write_text("[AU_AFSL]\n\\d{5,6}\n", encoding="utf-8")
    p = PiiPipeline(entity_keep=load_keep(str(keep)))
    text = "Advice under AFSL 233714."
    assert p.strip(text, PseudonymMap())[0] == text


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


def test_a_keep_match_exempts_only_itself_not_the_span_around_it(
    pipeline, monkeypatch
):
    # A keep pattern covers the name on the list and nothing more (2026-08-11):
    # 'WOOLWORTHS' survives, the suburb fused into the same detected span does
    # not. Both halves matter — the merchant name is the analytical substance,
    # and whatever a model fused around it may be identifying. The nested
    # ADDRESS is redundant here, which is the point: the org span's own
    # remainder already covers NEWTOWN, and the merged label follows the higher
    # score (0.95). The eval corpus measures the same behaviour on the
    # over-strip axis (suburb-suffixed merchants).
    text = "EFTPOS WOOLWORTHS NEWTOWN 4821 AU"
    results = [
        _rr("ORGANIZATION", 7, 25, 0.95),  # WOOLWORTHS NEWTOWN
        _rr("ADDRESS", 18, 25, 0.6),       # NEWTOWN
    ]
    monkeypatch.setattr(pipeline.analyzer, "analyze", lambda *a, **kw: results)
    out, _, _ = pipeline.strip(text, PseudonymMap())
    assert out == "EFTPOS WOOLWORTHS ORG_1 4821 AU"
    assert "NEWTOWN" not in out


def test_a_fused_span_cannot_ride_a_keep_listed_token(pipeline, monkeypatch):
    """The leak this rule exists for, measured on a real statement.

    Layer 0 reads a whole narrative field as ONE organization —
    'SK BUSINESS TRUS ANZ HIGHETT LOAN' — and it contains ANZ, which is on the
    keep list. Exempting the span wholesale kept the account holder's own trust
    name, three times on one page (2026-08-11). ANZ survives; nothing else in
    the span does."""
    text = "FROM SK BUSINESS TRUS ANZ HIGHETT LOAN"
    results = [_rr("ORGANIZATION", 5, len(text), 0.9)]
    monkeypatch.setattr(pipeline.analyzer, "analyze", lambda *a, **kw: results)
    out, _, _ = pipeline.strip(text, PseudonymMap())
    assert "SK BUSINESS TRUS" not in out
    assert "HIGHETT" not in out
    assert "ANZ" in out


def test_private_org_stripped_institution_and_merchant_kept(pipeline, monkeypatch):
    # An organization strips unless the keep list names it: the holder's own
    # trust goes, the bank and the merchant stay (pii.core.entity_keep). The
    # strip keeps the ORG_n placeholder (issue #2/#5).
    text = "ACCOUNT OF SK BUSINESS TRUST at ANZ paid WOOLWORTHS"
    results = [
        _rr("ORGANIZATION", 11, 28, 0.78),  # SK BUSINESS TRUST -> strip
        _rr("ORGANIZATION", 32, 35, 0.97),  # ANZ (keep-listed) -> keep
        _rr("ORGANIZATION", 41, 51, 0.95),  # WOOLWORTHS (no marker) -> keep
    ]
    monkeypatch.setattr(pipeline.analyzer, "analyze", lambda *a, **kw: results)
    out, _, _ = pipeline.strip(text, PseudonymMap())
    assert "SK BUSINESS TRUST" not in out
    assert "ORG_1" in out
    assert "ANZ" in out and "WOOLWORTHS" in out


def test_strip_orgs_forces_all_including_institutions(make_pipeline, monkeypatch):
    # --strip-orgs drops the ORGANIZATION section of the keep list, so every
    # org — institutions included — is stripped. Expressed as data rather than
    # as a flag the pipeline checks (2026-08-11).
    from pii.core.entity_keep import load_keep

    pipeline = make_pipeline(entity_keep=load_keep().without("ORGANIZATION"))
    text = "paid ANZ and WOOLWORTHS today"
    results = [
        _rr("ORGANIZATION", 5, 8, 0.97),    # ANZ
        _rr("ORGANIZATION", 13, 23, 0.95),  # WOOLWORTHS
    ]
    monkeypatch.setattr(pipeline.analyzer, "analyze", lambda *a, **kw: results)
    out, _, _ = pipeline.strip(text, PseudonymMap())
    assert "ANZ" not in out and "WOOLWORTHS" not in out


def test_merge_overlaps_unions_extents_higher_score_wins_type():
    # A small high-score span must not evict the wider covering span —
    # extents union. The label used to follow score alone, which let a
    # context-promoted BSB name a union hiding an account number; AU_BSB
    # now ranks below every other valid type (issue #8b), so the account
    # names the merged span regardless of score.
    merged = _merge_overlaps(
        [_rr("AU_BANK_ACCOUNT", 0, 20, 0.52), _rr("AU_BSB", 0, 7, 0.55)]
    )
    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (0, 20)
    assert merged[0].entity_type == "AU_BANK_ACCOUNT"


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

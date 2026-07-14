"""Checksum-invalid identifier handling: collection tiers, suppression,
masking, overlap ranking (design decided 2026-07-14, record in pii/DONE.md)."""

import pytest
from presidio_analyzer import RecognizerResult

from pii.invalid_recognizers import INVALID_ENTITY_TYPES, make_invalid_recognizers
from pii.mapping import PseudonymMap
from pii.pipeline import _merge_overlaps

# Literals with verified checksum status (see pii_eval.au validators):
VALID_TFN = "291 417 774"      # passes TFN mod-11
INVALID_TFN = "291 417 775"    # single-digit typo; fails TFN and ACN
INVALID_TFN_BARE = "123456789"  # fails TFN and ACN
VALID_ACN_9D = "526 018 155"   # passes ACN, fails TFN
INVALID_ABN = "24 787 782 328"  # fails ABN; its 3-3-3 tail fails TFN/ACN
MALFORMED_MEDICARE = "9317 06695 1"  # first digit outside 2-6
INVALID_CARD = "4783 5337 4068 1248"  # Luhn typo


def types(findings):
    return {f.entity_type for f in findings}


def test_tier_validation():
    with pytest.raises(ValueError):
        make_invalid_recognizers("bogus")
    assert make_invalid_recognizers("ignore") == []


def test_likely_collects_labeled_typo_without_masking(make_pipeline):
    p = make_pipeline(invalid_identifiers="likely")
    out, _, findings = p.strip(f"TFN: {INVALID_TFN}", PseudonymMap())
    assert "AU_TFN_INVALID" in types(findings)
    assert INVALID_TFN in out  # collected, not masked (mask=no default)
    f = next(f for f in findings if f.entity_type == "AU_TFN_INVALID")
    assert INVALID_TFN in f.value
    assert "mod-11" in f.rule


def test_valid_tfn_produces_no_findings(make_pipeline):
    p = make_pipeline(invalid_identifiers="likely")
    out, _, findings = p.strip(f"TFN: {VALID_TFN}", PseudonymMap())
    assert findings == []
    assert "TFN_1" in out


def test_ignore_tier_collects_nothing(make_pipeline):
    p = make_pipeline(invalid_identifiers="ignore")
    out, _, findings = p.strip(f"TFN: {INVALID_TFN}", PseudonymMap())
    assert findings == []
    assert INVALID_TFN in out


def test_bare_run_needs_context_tier(make_pipeline):
    # bare, unformatted digits next to a context word: invisible to
    # "likely" (no in-span evidence), promoted by the enhancer in "context"
    text = f"TFN quoted as {INVALID_TFN_BARE} on the form"
    _, _, likely = make_pipeline(invalid_identifiers="likely").strip(
        text, PseudonymMap()
    )
    _, _, context = make_pipeline(invalid_identifiers="context").strip(
        text, PseudonymMap()
    )
    assert "AU_TFN_INVALID" not in types(likely)
    assert "AU_TFN_INVALID" in types(context)


def test_bare_run_without_context_needs_all_tier(make_pipeline):
    text = f"receipt {INVALID_TFN_BARE} processed"
    _, _, context = make_pipeline(invalid_identifiers="context").strip(
        text, PseudonymMap()
    )
    _, _, everything = make_pipeline(invalid_identifiers="all").strip(
        text, PseudonymMap()
    )
    assert "AU_TFN_INVALID" not in types(context)
    assert "AU_TFN_INVALID" in types(everything)


def test_malformed_medicare_distinct_class(make_pipeline):
    p = make_pipeline(invalid_identifiers="likely")
    _, _, findings = p.strip(
        f"Medicare card: {MALFORMED_MEDICARE}", PseudonymMap()
    )
    assert "AU_MEDICARE_MALFORMED" in types(findings)
    f = next(f for f in findings if f.entity_type == "AU_MEDICARE_MALFORMED")
    assert "structurally impossible" in f.rule


def test_luhn_failed_card_collected(make_pipeline):
    p = make_pipeline(invalid_identifiers="likely")
    _, _, findings = p.strip(f"Card: {INVALID_CARD}", PseudonymMap())
    assert "CREDIT_CARD_INVALID" in types(findings)


def test_valid_sibling_identifier_suppresses_finding(make_pipeline):
    # A valid ACN fails the TFN checksum by arithmetic necessity; the
    # covering validated AU_ACN detection must suppress the shadow finding.
    p = make_pipeline(invalid_identifiers="likely")
    out, _, findings = p.strip(f"ACN: {VALID_ACN_9D}", PseudonymMap())
    assert findings == []
    assert "ACN_1" in out


def test_contained_fragment_deduplicated(make_pipeline):
    # The grouped 3-3-3 tail of an 11-digit ABN matches the TFN/ACN shadow
    # patterns; fragments strictly inside a longer finding are noise.
    p = make_pipeline(invalid_identifiers="likely")
    _, _, findings = p.strip(f"ABN: {INVALID_ABN}", PseudonymMap())
    assert "AU_ABN_INVALID" in types(findings)
    tail = INVALID_ABN[3:]
    assert not any(f.value == tail for f in findings)


def test_mask_invalid_pseudonymizes_and_rehydrates(make_pipeline):
    p = make_pipeline(invalid_identifiers="likely", mask_invalid=True)
    pmap = PseudonymMap()
    out, _, findings = p.strip(f"TFN: {INVALID_TFN}", pmap)
    assert INVALID_TFN not in out
    assert "TFN_INVALID_1" in out
    assert "AU_TFN_INVALID" in types(findings)  # still collected
    # multi-underscore placeholder survives the rehydration regex
    assert INVALID_TFN in pmap.rehydrate(out)


def test_masking_off_by_default(make_pipeline):
    p = make_pipeline(invalid_identifiers="likely")
    assert not (p.strip_entities & INVALID_ENTITY_TYPES)


def test_ner_guess_does_not_suppress_finding():
    # GLiNER2 emits PHONE_NUMBER/CREDIT_CARD as unvalidated guesses;
    # suppression must key on the VALIDATING recognizer's name, or an NER
    # phone guess over a typo'd TFN silently swallows the finding
    # (regression: 'ATO PAYMENT TFN 982 827 379' on the tier-1 corpus).
    from pii.pipeline import _collect_invalid

    text = "ATO PAYMENT TFN 982 827 379"
    shadow = RecognizerResult(
        entity_type="AU_TFN_INVALID", start=12, end=27, score=0.85
    )

    ner_guess = RecognizerResult(
        entity_type="PHONE_NUMBER", start=12, end=27, score=0.9
    )
    assert types(_collect_invalid([shadow, ner_guess], text)) == {
        "AU_TFN_INVALID"
    }

    validated = RecognizerResult(
        entity_type="PHONE_NUMBER",
        start=12,
        end=27,
        score=0.75,
        recognition_metadata={
            RecognizerResult.RECOGNIZER_NAME_KEY: "PhoneRecognizer"
        },
    )
    assert _collect_invalid([shadow, validated], text) == []


def test_merge_ranks_invalid_below_any_valid_type():
    # Overlap rule (decided): union the extents, the valid class wins the
    # placeholder regardless of score — the loser's tail must never leak.
    def rr(etype, start, end, score):
        return RecognizerResult(
            entity_type=etype, start=start, end=end, score=score
        )

    merged = _merge_overlaps(
        [
            rr("AU_TFN_INVALID", 0, 15, 0.9),
            rr("AU_BANK_ACCOUNT", 4, 20, 0.45),
        ]
    )
    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (0, 20)
    assert merged[0].entity_type == "AU_BANK_ACCOUNT"


def test_csv_mode_collects_and_masks_per_cell(make_pipeline):
    from pii.csv_mode import strip_csv

    text = (
        "Date,Description,Amount\n"
        f"01/02/2024,ATO PAYMENT TFN {INVALID_TFN},50.00\n"
    )
    p = make_pipeline(invalid_identifiers="likely")
    out, _, findings = strip_csv(
        text, p, PseudonymMap(), columns=["Description"]
    )
    assert "AU_TFN_INVALID" in types(findings)
    assert INVALID_TFN in out  # not masked

    p_mask = make_pipeline(invalid_identifiers="likely", mask_invalid=True)
    out, _, findings = strip_csv(
        text, p_mask, PseudonymMap(), columns=["Description"]
    )
    assert INVALID_TFN not in out
    assert "TFN_INVALID_1" in out
    assert out.splitlines()[0] == "Date,Description,Amount"
    assert "50.00" in out


def test_cli_mask_all_warns_and_logs(tmp_path, capsys):
    from pii.cli import main

    doc = tmp_path / "doc.txt"
    doc.write_text(f"TFN: {INVALID_TFN}\n", encoding="utf-8")
    out_file = tmp_path / "out.txt"
    rc = main(
        [
            "strip", str(doc), "-o", str(out_file),
            "--map", str(tmp_path / "map.json"), "--no-ner",
            "--invalid-identifiers", "all",
            "--mask-invalid-identifiers", "yes",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "warning" in err and "analytical utility" in err
    assert "checksum-invalid identifier candidate" in err  # log default yes
    assert "TFN_INVALID_1" in out_file.read_text(encoding="utf-8")

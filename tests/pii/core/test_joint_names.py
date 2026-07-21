"""Layer-1 joint-name recognizer (2026-07-15).

The mechanical joint-account name forms GLiNER2 loses to transaction-line
junk (glue spans, dropped initials, split pairs — the diagnostic in
pii/DONE.md). Model-free: the default pipeline fixture stubs NER, so every
strip asserted here is the pattern's own work. The corpus counterparts are
the PERSON_JOINT probes and joint-form PERSON draws in pii_eval/txbank.py.
"""

import pytest

from pii.core.mapping import PseudonymMap


def test_joint_initials_in_transaction_junk(pipeline):
    # The diagnostic line: GLiNER2's best emission was the glue span
    # 'LAWRENCE RENT'@0.55 with the initials dropped.
    out, _, _ = pipeline.strip(
        "OSKO P12345678 J & E LAWRENCE RENT", PseudonymMap()
    )
    assert "LAWRENCE" not in out
    assert "RENT" in out  # the keyword survives — no glue over-strip


def test_joint_initials_title_case(pipeline):
    out, _, _ = pipeline.strip("Loan Repayment E & J Moore", PseudonymMap())
    assert "Moore" not in out


# The shared-surname FULL-name form ('Julie and Brian Summers') is no longer
# a layer-1 pattern (2026-07-21, issue #4) — it's handled by expanding
# GLiNER2's own PERSON detections across the ' and '/' & ' connector, so it's
# model-dependent. Those cases moved to the connector-merge tests in
# test_gliner2_windows.py (fake model) and the model-marked check below.


def test_statement_phrases_not_matched_as_joint(pipeline):
    # 'X AND Y Z' caps triples that are prose, not couples. With the full-name
    # pattern retired (issue #4) no layer-1 rule matches them, and GLiNER2
    # (stubbed here) doesn't emit persons for them — so they stay put. A
    # regression guard against re-introducing a lexical full-name pattern.
    for text in (
        "PRINCIPAL AND INTEREST PAYMENT",
        "LOAN TERMS AND CONDITIONS APPLY",
        "SALARY AND WAGES CREDIT",
        "HOME AND CONTENTS INSURANCE",
    ):
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert out == text, text


@pytest.mark.model
def test_colliding_surname_couple_given_names_strip(make_pipeline):
    # Fee/Card are surnames that are also statement vocabulary. The full-name
    # form is now GLiNER2-driven (connector-merge), and GLiNER2 doesn't
    # recognise the word-like surname — so the GIVEN names strip (the couple
    # merges) but the colliding surname leaks. Accepted 2026-07-21; the
    # non-gated PERSON_COLLIDING eval probe measures the residual.
    pipeline = make_pipeline(stub_ner=False)
    out, _, _ = pipeline.strip(
        "Loan Repayment Julie and Brian Fee", PseudonymMap()
    )
    assert "Julie" not in out and "Brian" not in out  # the couple is stripped


def test_full_name_org_not_matched_as_joint(pipeline):
    # With the full-name pattern retired, 'X AND Y Z' org names are no longer
    # mis-split into joint persons by any layer-1 rule (NER stubbed here). A
    # regression guard. (Under the full pipeline the PTY LTD ones are stripped
    # as private ORGANIZATION entities by org_policy, issue #2 — a different
    # path; the connector-merge only fires on GLiNER2 PERSON detections.)
    for text in (
        "EFTPOS ANGUS AND ROBERTSON PTY LTD 4821 AU",
        "PAYMENT TO TAYLOR AND SCOTT LAWYERS PTY LTD",
        "TFR HARVEY AND MILLER HOLDINGS",
        "EFTPOS ANGUS AND ROBERTSON BOOKSHOP 4821 AU",  # was over-stripped
    ):
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert out == text, text


def test_initials_org_bare_still_sacrificed(pipeline):
    # 'P & O CRUISES' still matches the INITIALS pattern (P & O + surname slot)
    # and is stripped — the documented recall-first loss the initials pattern
    # keeps (ORGANIZATION_AND_BARE keep-probe measures it).
    out, _, _ = pipeline.strip("EFTPOS P & O CRUISES 4821 AU", PseudonymMap())
    assert "CRUISES" not in out


def test_initials_corporate_surname_kept(pipeline):
    # The one guard left on the initials pattern: a corporate marker in the
    # surname slot is an org, not a couple.
    out, _, _ = pipeline.strip("TFR E & J HOLDINGS", PseudonymMap())
    assert out == "TFR E & J HOLDINGS"


def test_lowercase_prose_untouched(pipeline):
    out, _, _ = pipeline.strip(
        "fees and charges apply to loans and savings accounts",
        PseudonymMap(),
    )
    assert "loans and savings" in out


def test_lowercase_nonvocab_prose_not_joint_name(pipeline):
    # Issue #4: presidio's default IGNORECASE turned the [A-Z] name-word class
    # into "any letter", so lowercase prose with NO statement-vocabulary word
    # (the guard can't catch it) matched the joint pattern. The recognizer
    # drops IGNORECASE, so these stay put.
    for text in (
        "a simple and convenient online option",
        "quick and easy setup",
        "date and the amount shown",
    ):
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert out == text, text

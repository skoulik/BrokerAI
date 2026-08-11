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


# The shared-surname FULL-name form ('Julie and Brian Summers') is not a
# layer-1 pattern (2026-07-21, issue #4): matching three words joined by 'and'
# is indistinguishable from prose by any lexical rule. It belongs to layer 0,
# which reads it semantically — so there is nothing model-free to assert about
# it here, and the eval corpus (PERSON_JOINT, 100% on all seeds) measures it.


def test_statement_phrases_not_matched_as_joint(pipeline):
    # 'X AND Y Z' caps triples that are prose, not couples. With the full-name
    # pattern retired (issue #4) no layer-1 rule matches them, so they stay
    # put. A regression guard against re-introducing a lexical full-name
    # pattern.
    for text in (
        "PRINCIPAL AND INTEREST PAYMENT",
        "LOAN TERMS AND CONDITIONS APPLY",
        "SALARY AND WAGES CREDIT",
        "HOME AND CONTENTS INSURANCE",
    ):
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert out == text, text


def test_full_name_org_not_matched_as_joint(pipeline):
    # With the full-name pattern retired, 'X AND Y Z' org names are no longer
    # mis-split into joint persons by any layer-1 rule. A regression guard.
    # (End to end, the PTY LTD ones strip as unrecognized ORGANIZATIONs —
    # no keep list names them, issue #2 — a different path.)
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
    # Issue #4: a default IGNORECASE turns the [A-Z] name-word class
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

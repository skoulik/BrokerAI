"""Layer-1 joint-name recognizer (2026-07-15).

The mechanical joint-account name forms GLiNER2 loses to transaction-line
junk (glue spans, dropped initials, split pairs — the diagnostic in
pii/DONE.md). Model-free: the default pipeline fixture stubs NER, so every
strip asserted here is the pattern's own work. The corpus counterparts are
the PERSON_JOINT probes and joint-form PERSON draws in pii_eval/txbank.py.
"""

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


def test_joint_full_all_caps(pipeline):
    # GLiNER2 split this shape into 'BRIAN SUMMERS'@0.98 + 'JULIE'@0.49
    # (connector leaked).
    out, _, _ = pipeline.strip(
        "OSKO P4X92K11QR JULIE AND BRIAN SUMMERS RENT", PseudonymMap()
    )
    assert "JULIE" not in out and "SUMMERS" not in out
    assert "RENT" in out


def test_joint_full_title_case(pipeline):
    out, _, _ = pipeline.strip(
        "Transfer to other Bank NetBank To Jeffrey and Randall Lawrence",
        PseudonymMap(),
    )
    assert "Lawrence" not in out


def test_joint_full_beyond_context_window(pipeline):
    # The context enhancer looks only 5 tokens back, and this corpus line
    # ('Online W... Loan to ORG ... NAME') puts the name further out — the
    # pattern must strip on its own confidence, not context promotion.
    out, _, _ = pipeline.strip(
        "ONLINE W123456789 LOAN TO OAKFIELD CONSULTING PTY LTD "
        "JULIE AND BRIAN SUMMERS",
        PseudonymMap(),
    )
    assert "SUMMERS" not in out
    assert "OAKFIELD CONSULTING PTY LTD" in out  # payee org untouched


def test_statement_phrases_not_stripped(pipeline):
    # 'X AND Y Z' caps triples that are statement vocabulary, not couples —
    # the giveaway word sits in a given-name slot.
    for text in (
        "PRINCIPAL AND INTEREST PAYMENT",
        "LOAN TERMS AND CONDITIONS APPLY",
        "SALARY AND WAGES CREDIT",
        "HOME AND CONTENTS INSURANCE",
    ):
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert out == text, text


def test_surname_colliding_with_statement_word_still_strips(pipeline):
    # Fee and Card are real surnames; the positional guard must not
    # sacrifice them (statement vocabulary only rejects in the given-name
    # slots, never the surname slot).
    for text, surname in (
        ("Loan Repayment Julie and Brian Fee", "Fee"),
        ("OSKO P4X92K11QR JULIE AND BRIAN CARD RENT", "CARD"),
    ):
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert surname not in out.replace("Repayment", ""), text
        assert "Julie" not in out and "JULIE" not in out, text


def test_corporate_and_name_not_stripped(pipeline):
    # 'X AND Y Z' organizations stay intact when a corporate marker is
    # visible — in the surname slot or trailing the three-word match.
    for text in (
        "EFTPOS ANGUS AND ROBERTSON PTY LTD 4821 AU",   # tail lookahead
        "PAYMENT TO TAYLOR AND SCOTT LAWYERS PTY LTD",  # tail lookahead
        "TFR HARVEY AND MILLER HOLDINGS",               # surname slot
    ):
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert out == text, text


def test_bare_and_org_sacrifice_pinned(pipeline):
    # The documented recall-first loss: org names with no corporate marker
    # anywhere match the person patterns and get stripped. Pinned so a
    # change in either direction is noticed; the eval measures the same
    # class as the ORGANIZATION_AND_BARE keep-probe.
    for text, marker in (
        ("EFTPOS P & O CRUISES 4821 AU", "CRUISES"),
        ("EFTPOS ANGUS AND ROBERTSON BOOKSHOP 4821 AU", "BOOKSHOP"),
    ):
        out, _, _ = pipeline.strip(text, PseudonymMap())
        assert marker not in out, text


def test_lowercase_prose_untouched(pipeline):
    out, _, _ = pipeline.strip(
        "fees and charges apply to loans and savings accounts",
        PseudonymMap(),
    )
    assert "loans and savings" in out

"""Plain-text document templates.

legacy_statement: fixed-column monospace bank statement in the style of the
plain-text legacy format in the reference corpus (no graphics, ALL-CAPS
particulars, right-aligned numbers) — the NER stress case (ALL-CAPS text
collapsed the removed GLiNER v1 backend's recall).

loan_application: broker-style applicant summary — the one document class
that carries the full PII battery (TFN, Medicare, DOB, licence, card), plus
a free-text notes paragraph with contextual identifiers.
"""

import random

from pii_eval import au, txbank
from pii_eval.build import Doc
from pii_eval.personas import SHORT_SUBURBS, TOWNS, Pool


def _date(rng: random.Random, year: int) -> str:
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    return f"{rng.randrange(1, 29):02d}{rng.choice(months)}{year % 100:02d}"


def legacy_statement(pool: Pool) -> Doc:
    rng = pool.rng
    biz = pool.business()
    p = pool.person()
    acct = pool.account()
    year = rng.choice([2022, 2023, 2024])
    doc = Doc()

    doc.raw("ACCOUNT STATEMENT").nl(2)
    doc.raw(" " * 8 + "PREMIUM BUSINESS SAVER").pad_to(50)
    doc.raw("BUSINESS ACCESS SAVER STATEMENT").nl(2)

    addressee = rng.choice(["THE DIRECTOR", "THE TRUSTEE", p.caps])
    doc.raw(" " * 8)
    if addressee == p.caps:
        doc.pii(addressee, "PERSON")
    else:
        doc.raw(addressee)
    doc.pad_to(46).raw("Account Number   : ")
    doc.pii(acct.number, "AU_BANK_ACCOUNT").nl()
    doc.raw(" " * 8).pii(p.street.upper(), "ADDRESS")
    doc.pad_to(46).raw(f"Statement Period : {_date(rng, year)}").nl()
    doc.raw(" " * 8).pii(
        f"{p.suburb.upper()}{' ' * max(24 - len(p.suburb), 1)}{p.state} {p.postcode}",
        "ADDRESS",
    )
    doc.pad_to(46).raw(f"Statement Number :{rng.randrange(1, 60):>8}").nl(2)

    # the account holder's own business/trust entity — private-entity PII
    # stripped by org_policy (a legal-form marker, not a known institution),
    # so ground-truthed ORGANIZATION_PRIVATE on the recall axis (2026-07-21)
    account_of = biz.trust if biz.trust and rng.random() < 0.5 else biz.name
    doc.raw("ACCOUNT OF: ").private_org(account_of).nl(2)
    doc.raw("Date    Particulars").pad_to(55).raw("Debit     Credit       Balance").nl()

    balance = round(rng.uniform(100, 90000), 2)
    doc.raw(f"{_date(rng, year)} OPENING BALANCE").pad_to(66).raw(f"{balance:>14,.2f}").nl()
    for _ in range(rng.randrange(8, 16)):
        doc.raw(f"{_date(rng, year)} ")
        for part in txbank.description(pool):
            if isinstance(part, str):
                doc.raw(part.upper())
            else:
                value, etype, *keep = part
                doc.pii(value.upper(), etype, *keep)
        debit, credit, balance = txbank.amounts(rng, balance)
        doc.pad_to(52).raw(f"{debit:>10}{credit:>11}{balance:>14,.2f}").nl()
    doc.raw(f"{_date(rng, year)} CLOSING BALANCE").pad_to(66).raw(f"{balance:>14,.2f}").nl(2)
    doc.raw(" " * 8 + "TOTAL DEBITS").pad_to(38).raw("TOTAL CREDITS").nl()
    return doc


def loan_application(pool: Pool, invalid: bool = False) -> Doc:
    """invalid=True renders the checksum-invalid variant: applicant 1's TFN,
    applicant 2's Medicare (structurally impossible first digit), the ABN
    and the repayment card carry injected single-digit errors, annotated as
    *_INVALID / *_MALFORMED with evidence="in-span" (the field label sits
    immediately before the value and the digit grouping is canonical). The
    sibling fields stay valid so the document mixes both classes."""
    rng = pool.rng
    a, b = pool.couple()
    biz = pool.business()
    acct = pool.account()
    doc = Doc()

    doc.raw("HOME LOAN APPLICATION - APPLICANT SUMMARY").nl()
    doc.raw(f"Broker ref: BRK-{rng.randrange(10**5, 10**6)}").nl(2)

    for i, person in enumerate((a, b), 1):
        doc.raw(f"Applicant {i}\n")
        doc.raw("  Name:            ").pii(f"{person.title} {person.full}", "PERSON").nl()
        doc.raw("  Date of birth:   ").pii(person.dob, "DATE_OF_BIRTH").nl()
        doc.raw("  TFN:             ")
        if invalid and i == 1:
            doc.pii(au.invalid_tfn(rng), "AU_TFN_INVALID",
                    strip_expected=False, evidence="in-span")
        else:
            doc.pii(au.tfn(rng), "AU_TFN")
        doc.nl()
        doc.raw("  Medicare card:   ")
        if invalid and i == 2:
            doc.pii(au.malformed_medicare(rng), "AU_MEDICARE_MALFORMED",
                    strip_expected=False, evidence="in-span")
        else:
            doc.pii(au.medicare(rng), "AU_MEDICARE")
        doc.nl()
        doc.raw("  Driver licence:  ").pii(au.drivers_licence(rng), "AU_DRIVERS_LICENCE").nl()
        doc.raw("  Mobile:          ").pii(person.mobile, "PHONE_NUMBER").nl()
        doc.raw("  Email:           ").pii(person.email, "EMAIL_ADDRESS").nl()
        doc.raw("  Current address: ").pii(person.address_oneline, "ADDRESS").nl()
        if i == 1:
            # PO Box — a mailing-address surface form the one-line street
            # addresses don't exercise
            po_box = (f"PO Box {rng.randrange(1, 999)}, "
                      f"{person.suburb} {person.state} {person.postcode}")
            doc.raw("  Postal address:  ").pii(po_box, "ADDRESS").nl()
        doc.nl()

    doc.raw("Self-employment\n")
    doc.raw("  Entity:          ").private_org(biz.name).nl()
    # Always rendered: a TRUST-marker private org is a guaranteed corpus
    # feature (test_generate asserts its presence; found coincidence-
    # dependent on pool draws 2026-07-22), stripped by org_policy — same
    # stance as the PTY LTD name above. Derive a name when the pool
    # business carries no trust.
    trust = biz.trust or f"{biz.name.split()[0]} FAMILY TRUST"
    doc.raw("  Trustee for:     ").private_org(trust).nl()
    # ORGANIZATION_ATF probe (issue #9): the '<company> ATF <trust>' line
    # form with the DOC-TRUNCATED trust name real statements produce
    # ('... ATF SK BU') — the layer-1 ATF-tail pattern must strip the
    # clause even though the truncation removes the trust's own marker.
    truncated_trust = f"ATF {biz.name.split()[0]} FAMILY TRU"
    doc.raw("  Account name:    ").private_org(biz.name).raw(" ")
    doc.pii(truncated_trust, "ORGANIZATION_ATF").nl()
    doc.raw("  ABN:             ")
    if invalid:
        doc.pii(au.invalid_abn(rng), "AU_ABN_INVALID",
                strip_expected=False, evidence="in-span")
    else:
        doc.pii(biz.abn, "AU_ABN")
    doc.nl()
    doc.raw("  ACN:             ").pii(biz.acn, "AU_ACN").nl(2)

    doc.raw("Salary credit account\n")
    doc.raw(f"  Bank:            {acct.bank}\n")
    doc.raw("  BSB:             ").pii(acct.bsb, "AU_BSB").nl()
    doc.raw("  Account:         ").pii(acct.number, "AU_BANK_ACCOUNT").nl()
    doc.raw("  Card for repayments: ")
    if invalid:
        doc.pii(au.invalid_card(rng), "CREDIT_CARD_INVALID",
                strip_expected=False, evidence="in-span")
    else:
        doc.pii(au.card_number(rng), "CREDIT_CARD")
    doc.nl()
    # AMOUNT_COLUMN keep-probe (issue #3): adjacent formatted amounts in a
    # loan/payment context — the decimal-fraction+next-integer boundary
    # ('...74 377...') must NOT be mistaken for a grouped account number.
    doc.raw("  Recent loan payment: ").pii(
        "2,148.74 377,970.04", "AMOUNT_COLUMN", strip_expected=False
    ).nl()
    # Identifier post-validation keep-probes (issue #10): a letter+10-digit
    # receipt reference (GLiNER2 mislabels the shape TFN/licence/passport),
    # a >16-digit run (can never be an AU account+BSB), and a masked last-4
    # card disclosure (the deliberate stance: a last-4 fragment alone is
    # not strip-worthy — it falls under the digit floors, consistent with
    # layer-1). All three must survive unstripped.
    doc.raw("  Deposit receipt:     ").pii(
        txbank.receipt_reference(rng), "REFERENCE_NUMBER",
        strip_expected=False,
    ).nl()
    doc.raw("  Batch trace:         ").pii(
        txbank.overlong_digits(rng), "DIGITS_OVERLONG", strip_expected=False
    ).nl()
    doc.raw("  Repayments drawn from card ending ").pii(
        f"{rng.randrange(0, 10000):04d}", "CARD_LAST4", strip_expected=False
    ).nl()
    # TRAILING_AMOUNT keep-probe (issue #11): a decimal amount right after a
    # labeled grouped account — the labeled-account pattern must strip the
    # account in full yet release the amount (the issue-#3 guard extended to
    # the labeled form; without it the grouped tail ate the amount's integer
    # part: 'A/C 30-743-3257 148.74' -> '... 148').
    grouped_acct = (f"{rng.randrange(10, 100)}-{rng.randrange(100, 1000)}-"
                    f"{rng.randrange(1000, 10000)}")
    doc.raw("  Interest charged from A/C ").pii(
        grouped_acct, "AU_BANK_ACCOUNT"
    ).raw(" ").pii(
        f"{rng.uniform(1, 300):.2f}", "TRAILING_AMOUNT", strip_expected=False
    ).raw("CR").nl(2)

    occupation = rng.choice(["dentist", "electrician", "GP", "teacher"])
    town = rng.choice(["Wagga Wagga", "Ballarat", "Dubbo", "Cairns"])
    doc.raw("Notes\n")
    doc.raw("  Applicant 2 is ")
    # Layer-3 (LLM audit) target: identifying by occupation+place, invisible
    # to patterns and NER. Distinct type so it reports as a known gap instead
    # of tripping the critical-leak gate on layers 1-2.
    doc.pii(f"a {occupation} in {town}", "CONTEXTUAL_ID")
    doc.raw(
        "; income verified from last two BAS lodgements. "
        "Genuine savings held with "
    )
    # PROSE_AND keep-probe (issue #4): lowercase 'X and Y Z' prose with no
    # statement-vocabulary word must NOT be mis-detected as a joint name — the
    # case the IGNORECASE bug hit and the vocabulary guard cannot catch.
    doc.raw(f"{acct.bank}. Repayments are ")
    doc.pii("simple and convenient online", "PROSE_AND",
            strip_expected=False).raw(".").nl()
    # Bare-town mentions: LOCATION measures the GLiNER2 location pass on
    # standalone names (no address context); LOCATION_SHORT is the real
    # 3-letter-suburb class the LOCATION_MIN_CHARS=4 floor knowingly
    # sacrifices — expected to leak until the gazetteer task lands, so it
    # reports per-form (the PERSON_JOINT precedent). Neither is in
    # build.CRITICAL.
    doc.raw("  Security property is in ")
    doc.pii(rng.choice(TOWNS), "LOCATION")
    doc.raw(". Applicant 1 previously resided in ")
    doc.pii(rng.choice(SHORT_SUBURBS), "LOCATION_SHORT")
    doc.raw(".").nl(2)
    # Corporate-licence keep-probes (issue #8c / other-finding #1): AFSL
    # and Australian Credit Licence numbers are public corporate
    # identifiers — kept classes AU_AFSL/AU_CREDIT_LICENCE — and the bare
    # number must not strip as a driver licence (GLiNER2's footer
    # mislabel, suppressed by its corporate-licence context guard).
    doc.raw(f"Credit services arranged by {acct.bank} ")
    doc.pii(f"AFSL {rng.randrange(10**5, 10**6)}", "AU_AFSL",
            strip_expected=False)
    doc.raw(", ")
    doc.pii(f"Australian Credit Licence {rng.randrange(10**5, 10**6)}",
            "AU_CREDIT_LICENCE", strip_expected=False)
    doc.raw(".").nl()
    return doc

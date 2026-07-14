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
from pii_eval.personas import Pool


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

    doc.raw("ACCOUNT OF: ").org(biz.name).nl(2)
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
        doc.raw("  Current address: ").pii(person.address_oneline, "ADDRESS").nl(2)

    doc.raw("Self-employment\n")
    doc.raw("  Entity:          ").org(biz.name).nl()
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
    doc.nl(2)

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
    doc.raw(f"{acct.bank}.").nl()
    return doc

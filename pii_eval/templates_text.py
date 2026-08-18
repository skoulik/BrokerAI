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
from pii_eval.personas import TOWNS, Pool


def _date(rng: random.Random, year: int) -> str:
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
              "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    return f"{rng.randrange(1, 29):02d}{rng.choice(months)}{year % 100:02d}"


# Statements are the realistic multi-page class, and the continuation header
# is what makes them a cross-page test rather than merely a long document:
# the holder and the account number are reprinted on every page, so a
# detector that reads page 1 and overlooks page 3 leaks a value it has
# already seen. Fixed rather than drawn, so page counts stay comparable
# across seeds.
STATEMENT_PAGES = 3


def legacy_statement(pool: Pool, pages: int = STATEMENT_PAGES) -> Doc:
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
    # The addressee block is TWO COLUMNS, and the address wraps inside the
    # left one — so on the rendered image the right column's field lands
    # between its halves in the page string, which is what an OCR page banded
    # visually looks like. Its own truth type per the convention for
    # known-hard forms: no contiguous search reaches across the splice, and
    # before 2026-08-13 a perfectly boxed, perfectly read address fell all the
    # way through to the model's own geometry. Reprinted, wrapped the same
    # way, by `_statement_continuation`.
    doc.raw(" " * 8).pii(p.street.upper(), "ADDRESS_WRAPPED")
    doc.pad_to(46).raw(f"Statement Period : {_date(rng, year)}").nl()
    doc.raw(" " * 8).pii(
        f"{p.suburb.upper()}{' ' * max(24 - len(p.suburb), 1)}{p.state} {p.postcode}",
        "ADDRESS_WRAPPED",
    )
    doc.pad_to(46).raw(f"Statement Number :{rng.randrange(1, 60):>8}").nl(2)

    # the account holder's own business/trust entity — private-entity PII
    # stripped because no keep list names it (pii.core.entity_keep), so
    # ground-truthed ORGANIZATION_PRIVATE on the recall axis (2026-07-21)
    account_of = biz.trust if biz.trust and rng.random() < 0.5 else biz.name
    doc.raw("ACCOUNT OF: ").private_org(account_of).nl()
    # The holder in CAPS, unconditionally — the addressee above is a draw, so
    # without this the caps form appears on page 1 only two seeds in three and
    # the caps-vs-title-case grouping probe would be luck. The continuation
    # header prints the same person in title case.
    doc.raw("HELD BY:    ").pii(p.caps, "PERSON").nl()
    # Both account holders in full, which is what makes the initials form in
    # the transaction lines DERIVABLE (pii.core.derived, 2026-08-14): the joint
    # rule pairs people some layer already detected, so a document that never
    # names them cannot produce 'E & J CHAVEZ'. Ground-truthed PERSON_JOINT —
    # the value IS a joint name, and layer 0 reads it as one span.
    holder_a, holder_b = pool.holders
    doc.raw("JOINT ACCOUNT: ").pii(
        f"{holder_a.full} and {holder_b.full}".upper(), "PERSON_JOINT"
    ).nl(2)

    # Label-attachment probes (2026-08-14). Both are about WHERE a label sits
    # relative to its value, which is the whole of what the geometric
    # attachment decides, and this document renders monospace (`legacy*` is a
    # fixed-column doc) so the image tier sees real columns rather than
    # reflowed prose.
    #
    # 1. The label sits directly ABOVE its value in the same column, with
    #    unrelated text on both lines — the case a lookbehind structurally
    #    cannot see. Expected to strip on the IMAGE tier and to MISS on the
    #    text tier, because text mode is left-only by decision (Sergei,
    #    2026-08-14) and has no vertical band. Non-gated, and left in the
    #    corpus rather than deleted so the difference between the tiers is
    #    scored on every run instead of being remembered.
    doc.raw("Interest to date").pad_to(46).raw("Account Number").nl()
    doc.raw(f"${round(rng.uniform(1, 400), 2)}").pad_to(46)
    doc.pii(f"{rng.randrange(10**7, 10**8)}", "ACCOUNT_LABELLED_ABOVE").nl(2)
    # 2. The mirror image, and the reason the word floor is a word COUNT: a
    #    left column that happens to contain an account label, beside a right
    #    column carrying an unrelated reference. The 60-character lookback
    #    promotes it (`cheque` reached a bank's phone number 48 characters and
    #    one column away on a real statement); the band must not. MUST NOT
    #    strip, on either tier.
    doc.raw("Mail this slip with your cheque to Group Card Services").pad_to(60)
    doc.raw("Ref ").pii(
        f"{rng.randrange(10**5, 10**6)}",
        "REFERENCE_ACROSS_COLUMN",
        strip_expected=False,
    ).nl(2)
    # 3. The geometric sibling of the AMOUNT_COLUMN probe: a left column ENDING
    #    in a number beside a right column BEGINNING with one. `linearize` joins
    #    them with a single space, so a grouped pattern reads `2022 133 174` as
    #    one account number spanning two columns — a value printed nowhere on
    #    the page (found on a real statement, 2026-08-14). The year must
    #    survive; only geometry can reject the join, so this is an image-tier
    #    probe and a known text-tier over-strip.
    doc.raw("Account enquiries to 30 June ")
    doc.pii(str(year), "YEAR_ACROSS_COLUMN", strip_expected=False).pad_to(60)
    doc.raw(f"{rng.randrange(100, 1000)} {rng.randrange(100, 1000)}").nl(2)

    balance = round(rng.uniform(100, 90000), 2)
    for page in range(1, pages + 1):
        if page > 1:
            doc.page_break()
            _statement_continuation(doc, p, acct, account_of, page, pages)
        doc.raw("Date    Particulars").pad_to(55)
        doc.raw("Debit     Credit       Balance").nl()
        if page == 1:
            doc.raw(f"{_date(rng, year)} OPENING BALANCE").pad_to(66)
            doc.raw(f"{balance:>14,.2f}").nl()
        for _ in range(rng.randrange(6, 11)):
            doc.raw(f"{_date(rng, year)} ")
            for part in txbank.description(pool):
                if isinstance(part, str):
                    doc.raw(part.upper())
                else:
                    value, etype, *keep = part
                    doc.pii(value.upper(), etype, *keep)
            debit, credit, balance = txbank.amounts(rng, balance)
            doc.pad_to(52).raw(f"{debit:>10}{credit:>11}{balance:>14,.2f}").nl()
        if page < pages:
            doc.pad_to(60).raw("CONTINUED OVERLEAF").nl()

    doc.raw(f"{_date(rng, year)} CLOSING BALANCE").pad_to(66).raw(f"{balance:>14,.2f}").nl(2)
    doc.raw(" " * 8 + "TOTAL DEBITS").pad_to(38).raw("TOTAL CREDITS").nl()
    return doc


# How much of the account name a continuation header's fixed-width field
# keeps. Two characters is enough to defeat exact and squash matching while
# staying an obvious truncation to a reader.
_TRUNCATE_BY = 2


def _statement_continuation(
    doc: Doc, p, acct, account_of: str, page: int, pages: int
) -> None:
    """The header a real statement reprints on every continuation page.

    Three probes live here, and all three exist because a corpus printing one
    surface form per entity would pass whether or not the matching works:

    - the holder's name in TITLE case, where page 1 has it in caps — the
      case-folded comparison in `pii.core.grouping`;
    - the account name TRUNCATED to a fixed-width field, which is what
      statements do and what defeats both certain tiers of
      `locator.locate_borrowed` (the known value is a strict SUPERSTRING of
      what the page prints). Its own truth type, per the convention for
      known-hard forms. Truncation used to remove the legal-form
      marker the org policy keyed on as well, which is what made this probe a
      leak rather than a geometry question; since 2026-08-11 an unrecognized
      name strips either way, so it now isolates the fuzzy borrowed tier alone.
    - the address WRAPPED inside the left column, as page 1 prints it, with
      the right column's field between its halves. Layer 0 is far less likely
      to name it again this far into a document, so this printing is what the
      wrapped tier of `locator.locate_borrowed` has to reach — and it has no
      box to lean on, only the column its two lines share.
    """
    doc.raw("ACCOUNT STATEMENT").pad_to(46).raw("Account Number   : ")
    doc.pii(acct.number, "AU_BANK_ACCOUNT").nl()
    doc.raw(" " * 8).pii(p.full, "PERSON")
    doc.pad_to(46).raw(f"Page {page} of {pages}").nl()
    doc.raw(" " * 8).pii(p.street.upper(), "ADDRESS_WRAPPED")
    doc.pad_to(46).raw("A/C TYPE         : SAVINGS").nl()
    doc.raw(" " * 8).pii(
        f"{p.suburb.upper()}{' ' * max(24 - len(p.suburb), 1)}{p.state} {p.postcode}",
        "ADDRESS_WRAPPED",
    )
    doc.pad_to(46).raw("CURRENCY         : AUD").nl()
    doc.raw(" " * 8).raw("A/C NAME: ")
    doc.pii(account_of[:-_TRUNCATE_BY], "ORGANIZATION_TRUNCATED").nl(2)


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

    broker_ref = f"BRK-{rng.randrange(10**5, 10**6)}"
    doc.raw("HOME LOAN APPLICATION - APPLICANT SUMMARY").nl()
    doc.raw(f"Broker ref: {broker_ref}").pad_to(60).raw("Page 1 of 2").nl(2)

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
        doc.raw("  ABN:             ")
        # Hyphen-grouped on purpose (2026-08-09): the space-only surface forms
        # hid a leak where a VALID hyphenated identifier matched no rule at
        # all. See au.hyphenate.
        doc.pii(au.hyphenate(au.abn(rng)), "AU_ABN").nl()
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

    # Page 2. The continuation header reprints applicant 1 and the broker
    # ref, so the cross-page grouping has both a PERSON and an identifier to
    # carry over — and the name is in CAPS here against title case on page 1.
    doc.page_break()
    doc.raw("HOME LOAN APPLICATION (CONTINUED) - ").pii(a.caps, "PERSON").nl()
    doc.raw(f"Broker ref: {broker_ref}").pad_to(60).raw("Page 2 of 2").nl(2)

    doc.raw("Self-employment\n")
    doc.raw("  Entity:          ").private_org(biz.name).nl()
    # Always rendered: a TRUST-marker private org is a guaranteed corpus
    # feature (test_generate asserts its presence; found coincidence-
    # dependent on pool draws 2026-07-22), stripped as an unrecognized org — same
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
    # Leading-zero ABN probe: 11 digits starting with 0 are not a real ABN,
    # but presidio's mod-89 check accepts some and 2.2.364 changed which —
    # must strip as AU_ABN rather than fall through both AU_ABN and the
    # AU_ABN_INVALID shadow (pii/core/checksums.py:abn_checksum). Drawn from
    # a Random derived from the already-drawn ABN rather than from pool.rng:
    # consuming the shared stream would shift every downstream draw and
    # re-roll every existing seed's corpus, making eval runs across the
    # change incomparable (and the tier-1 gate is seed-fragile on PERSON).
    zero_rng = random.Random(int(au.digits(biz.abn)) ^ int(au.digits(acct.number)))
    doc.raw("  Prior ABN:       ").pii(au.abn_leading_zero(zero_rng), "AU_ABN").nl()
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
    # Bare-town mention: standalone LOCATION detection was retired 2026-07-23
    # (a bare place name is acceptable verbatim in financial docs), so this is
    # a KEEP probe — the town must survive. The ADDRESS passes still own
    # address-shaped lines, so a suburb in clearly address-flavored context can
    # still strip; this prose deliberately keeps the mention plain. Not in
    # build.CRITICAL.
    doc.raw("  Security property is in ")
    doc.pii(rng.choice(TOWNS), "LOCATION", strip_expected=False)
    doc.raw(".").nl(2)
    # Corporate-licence probes (issue #8c / other-finding #1): AFSL and
    # Australian Credit Licence numbers. Public corporate identifiers, kept
    # until 2026-08-14 and stripped since (Sergei, "for now") — under their
    # OWN classes either way, which is what keeps them distinguishable from a
    # driver licence in a report. The label is matched as a lookbehind, so the
    # probe value is the bare number: a span covering the label would key the
    # map on a different string than an unlabelled occurrence of it.
    doc.raw(f"Credit services arranged by {acct.bank} AFSL ")
    doc.pii(f"{rng.randrange(10**5, 10**6)}", "AU_AFSL")
    doc.raw(", Australian Credit Licence ")
    doc.pii(f"{rng.randrange(10**5, 10**6)}", "AU_CREDIT_LICENCE")
    doc.raw(".").nl()
    # The half-abbreviated label spellings a real insurance-certificate footer
    # prints — `AFS` abbreviated, the licence word spelled out or abbreviated
    # after it, and the same abbreviation on the credit-licence sibling. Their
    # own probes rather than a swap of the spellings above, because the acronym
    # being the corpus' ONLY spelling is precisely why these forms matched
    # nothing (2026-08-14).
    doc.raw("Product issuer AFS Licence No ")
    doc.pii(f"{rng.randrange(10**5, 10**6)}", "AU_AFSL")
    doc.raw(", underwriter AFS Lic ")
    doc.pii(f"{rng.randrange(10**5, 10**6)}", "AU_AFSL")
    doc.raw(", broking under Credit Lic ")
    doc.pii(f"{rng.randrange(10**5, 10**6)}", "AU_CREDIT_LICENCE")
    doc.raw(".").nl()
    return doc

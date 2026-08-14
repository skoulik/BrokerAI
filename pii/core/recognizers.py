"""Layer 1 — every pattern/checksum rule, in one module.

Merged from the old `recognizers.py` + `invalid_recognizers.py` + Presidio's
built-ins on 2026-08-09, and the merge is the point rather than tidying:

**A checksummed identifier has ONE rule, not two.** Presidio owned the valid
class and our shadow owned the invalid one, each with its own pattern set and
its own copy of the arithmetic, and the pair was supposed to *partition* the
digit space. It did not. Presidio's AU patterns only accept SPACE-grouped
digits while the shadows accept `[- ]`, so a hyphen-grouped **valid**
identifier (`123-456-782`) matched Presidio's rule not at all and was dropped
by the shadow for passing its checksum — detected by nothing, in every class:
TFN, ABN, ACN and Medicare alike. Verified before the merge; the corpus never
caught it because `pii_eval/au.py` only ever emits space-grouped forms.

Now `ChecksumRule` matches once, extracts the digits once, calls the checksum
once, and branches: pass emits the valid class at 1.0, fail emits the
`*_INVALID` shadow at its tier score. The two halves cannot disagree, because
there is only one of everything.

Collection tiers (`likely` / `context` / `all` / `ignore`) survive unchanged
and now govern only the invalid branch — the valid branch always fires, exactly
as Presidio's did:

- ``likely``  — only in-span evidence collects: canonical grouping
  ("123 456 782") or a label captured by the regex ("TFN: 123456780").
- ``context`` — additionally, bare digit runs promoted past threshold by a
  nearby context word (base 0.15 + the engine's +0.35 boost).
- ``all``     — every failing match, bare runs at 0.5. Noisy by design: ~90%
  of random 9-digit runs fail the TFN checksum.
- ``ignore``  — the invalid branch emits nothing.

Scores are recall-first: base pattern scores sit below the pipeline threshold
and rely on the context boost, except where the form is unambiguous on its own.
"""

from __future__ import annotations

import regex

from pii.core.checksums import (
    abn_checksum,
    acn_checksum,
    digits,
    iban_checksum,
    luhn_checksum,
    medicare_checksum,
    tfn_checksum,
)
from pii.core.detection import Detection
from pii.core.labels import Exact
from pii.core.engine import MAX_SCORE, STRICT, Pattern, PatternRule, Rule

TIERS = ("ignore", "likely", "context", "all")

# Score for an invalid candidate whose evidence is inside the matched span.
_IN_SPAN_SCORE = 0.5
# ...and for a bare digit run, per tier (None = not collected at that tier).
_BARE_SCORE = {"ignore": None, "likely": None, "context": 0.15, "all": 0.5}

# The separator between the groups of a formatted identifier.
#
# NOT `[- ]`, which is what these patterns used until 2026-08-12: exactly one
# space or one hyphen. A scanned statement in fixed-width columns routinely
# prints two spaces, and OCR emits tabs and non-breaking spaces — at which
# point a VALID TFN / ABN / ACN / Medicare / BSB matched NOTHING, neither the
# valid class nor its `*_INVALID` shadow. Same shape as the split-ownership
# failure narrated in the module docstring, and invisible for the same reason:
# `pii_eval/au.py` only ever emits single-space forms, so no corpus run could
# have caught it. Found by Sergei on a real statement.
#
# Bounded, and a NEWLINE is deliberately excluded. `{1,3}` of horizontal space
# keeps a match inside one printed field; an unbounded run or a line break
# would let two unrelated columns join into a single candidate with nothing
# but the checksum in the way — and a TFN's mod-11 lets 1 in 11 random runs
# through.
#
# `*` and `+` were both measured against this on the eval corpus (2026-08-12).
# `*` is the trap: it matches ZERO separators, so "grouped" stops meaning
# grouped and collapses onto bare digit runs — `\b\d{3}[- ]*\d{3}\b` is just
# `\b\d{6}\b`, every six-digit number becomes a BSB candidate (30 -> 44), and a
# bare run inherits the in-span score its shadow is not entitled to
# (invalid findings 117 -> 201). This class and `+` both cost exactly zero
# there; this one additionally covers tab, en-dash and NBSP.
_SEP = r"[-\u2010-\u2015\u00a0\t ]{1,3}"
# The same, where the separator is optional — a labeled form may be unspaced.
_SEP_OPT = r"[-\u2010-\u2015\u00a0\t ]{0,3}"


# ---------------------------------------------------------------------------
# Checksummed identifiers: one rule, two outcomes
# ---------------------------------------------------------------------------


class ChecksumRule(PatternRule):
    """Pattern set + checksum -> the valid class or its `*_INVALID` shadow.

    Subclasses declare the entity pair, the accepted digit counts, the
    in-span and bare patterns, and `checksum`.
    """

    INVALID_ENTITY: str = ""
    RULE_TEXT: str = ""  # the failed rule, for the report
    DIGIT_COUNTS: tuple[int, ...] = ()
    # Two kinds of evidence, and neither is ever part of the value:
    #
    # GROUPED  — canonical digit grouping, evidence inside the span itself.
    # LABELLED — a looser digit shape that only counts when a LABEL is
    #            attached to it. Until 2026-08-14 the label was spelled as a
    #            regex lookbehind here; it is now `context` plus STRICT
    #            attachment, which is the same gate judged on the page instead
    #            of in the assembled string, so a label in the column ABOVE
    #            its value now counts too. Scores did not move: a STRICT
    #            pattern is gated by its label, not boosted by it.
    #
    # Either way the label stays OUTSIDE the span — it is evidence, not part
    # of the value. A span covering "TFN: 123 456 782" keys the pseudonym map
    # on a different string than a bare occurrence of the same TFN, so one
    # identifier forks into TFN_1 and TFN_2 inside a single document.
    GROUPED_PATTERNS: tuple[tuple[str, str], ...] = ()
    LABELLED_PATTERNS: tuple[tuple[str, str], ...] = ()
    BARE_PATTERNS: tuple[tuple[str, str], ...] = ()

    def __init__(self, tier: str = "likely") -> None:
        if tier not in TIERS:
            raise ValueError(f"invalid_identifiers tier {tier!r}, expected one of {TIERS}")
        self.tier = tier
        self._bare = {name for name, _ in self.BARE_PATTERNS}
        self.patterns = (
            *(Pattern(n, rx, _IN_SPAN_SCORE) for n, rx in self.GROUPED_PATTERNS),
            *(
                Pattern(n, rx, _IN_SPAN_SCORE, attach=STRICT)
                for n, rx in self.LABELLED_PATTERNS
            ),
            *(Pattern(n, rx, _IN_SPAN_SCORE) for n, rx in self.BARE_PATTERNS),
        )
        super().__init__()

    @property
    def entities(self) -> tuple[str, ...]:
        return tuple(e for e in (self.entity, self.INVALID_ENTITY) if e)

    def checksum(self, d: str) -> bool:
        raise NotImplementedError

    def emit(self, matched: str, pattern: Pattern):
        d = digits(matched)
        if len(d) not in self.DIGIT_COUNTS:
            return None
        if self.checksum(d):
            # The valid branch ignores the pattern's score, exactly as
            # Presidio's validate_result did: a passing checksum is proof.
            return self.entity, MAX_SCORE
        score = self._invalid_score(pattern)
        if score is None:
            return None
        return self.INVALID_ENTITY, score

    def _invalid_score(self, pattern: Pattern) -> float | None:
        if self.tier == "ignore":
            return None
        if pattern.name in self._bare:
            return _BARE_SCORE[self.tier]
        return _IN_SPAN_SCORE


class AuTfnRule(ChecksumRule):
    entity = "AU_TFN"
    INVALID_ENTITY = "AU_TFN_INVALID"
    RULE_TEXT = "TFN mod-11 checksum failed"
    DIGIT_COUNTS = (9,)
    GROUPED_PATTERNS = (
        ("tfn grouped", r"\b\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3}\b"),
    )
    LABELLED_PATTERNS = (
        ("tfn labeled",
         r"\b\d{3}" + _SEP_OPT + r"\d{3}" + _SEP_OPT + r"\d{3}\b"),
    )
    BARE_PATTERNS = (("tfn bare", r"\b\d{9}\b"),)
    context = ("tax file number", "tax file no", "tfn")

    def checksum(self, d: str) -> bool:
        return tfn_checksum(d)


class AuMedicareRule(ChecksumRule):
    entity = "AU_MEDICARE"
    INVALID_ENTITY = "AU_MEDICARE_INVALID"
    RULE_TEXT = "Medicare mod-10 checksum failed"
    DIGIT_COUNTS = (10,)
    GROUPED_PATTERNS = (
        ("medicare grouped",
         r"\b[2-6]\d{3}" + _SEP + r"\d{5}" + _SEP + r"\d\b"),
    )
    LABELLED_PATTERNS = (
        ("medicare labeled",
         r"\b[2-6]\d{3}" + _SEP_OPT + r"\d{5}" + _SEP_OPT + r"\d\b"),
    )
    BARE_PATTERNS = (("medicare bare", r"\b[2-6]\d{9}\b"),)
    context = ("medicare",)

    def checksum(self, d: str) -> bool:
        return medicare_checksum(d)


class AuMedicareMalformedRule(ChecksumRule):
    """RELAXED shadow: a first digit outside 2-6 is structurally impossible,
    so such values never reach the real validator at all — this rule exists
    precisely to see them. The checksum is irrelevant; the structure alone
    invalidates the value, so there is no valid branch."""

    entity = ""  # never emitted
    INVALID_ENTITY = "AU_MEDICARE_MALFORMED"
    RULE_TEXT = "Medicare first digit outside 2-6 (structurally impossible)"
    DIGIT_COUNTS = (10,)
    GROUPED_PATTERNS = (
        ("medicare malformed grouped",
         r"\b[017-9]\d{3}" + _SEP + r"\d{5}" + _SEP + r"\d\b"),
    )
    LABELLED_PATTERNS = (
        ("medicare malformed labeled",
         r"\b[017-9]\d{3}" + _SEP_OPT + r"\d{5}" + _SEP_OPT + r"\d\b"),
    )
    BARE_PATTERNS = (("medicare malformed bare", r"\b[017-9]\d{9}\b"),)
    context = ("medicare",)

    def checksum(self, d: str) -> bool:
        return False  # malformed by pattern construction


class AuAbnRule(ChecksumRule):
    entity = "AU_ABN"
    INVALID_ENTITY = "AU_ABN_INVALID"
    RULE_TEXT = "ABN mod-89 checksum failed"
    DIGIT_COUNTS = (11,)
    GROUPED_PATTERNS = (
        ("abn grouped",
         r"\b\d{2}" + _SEP + r"\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3}\b"),
    )
    LABELLED_PATTERNS = (
        ("abn labeled",
         r"\b\d{2}" + _SEP_OPT + r"\d{3}" + _SEP_OPT + r"\d{3}"
         + _SEP_OPT + r"\d{3}\b"),
    )
    BARE_PATTERNS = (("abn bare", r"\b\d{11}\b"),)
    context = ("australian business number", "abn")

    def checksum(self, d: str) -> bool:
        return abn_checksum(d)


class AuAcnRule(ChecksumRule):
    entity = "AU_ACN"
    INVALID_ENTITY = "AU_ACN_INVALID"
    RULE_TEXT = "ACN complement checksum failed"
    DIGIT_COUNTS = (9,)
    GROUPED_PATTERNS = (
        ("acn grouped", r"\b\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3}\b"),
    )
    LABELLED_PATTERNS = (
        ("acn labeled",
         r"\b\d{3}" + _SEP_OPT + r"\d{3}" + _SEP_OPT + r"\d{3}\b"),
    )
    BARE_PATTERNS = (("acn bare", r"\b\d{9}\b"),)
    context = ("australian company number", "acn")

    def checksum(self, d: str) -> bool:
        return acn_checksum(d)


class CreditCardRule(ChecksumRule):
    entity = "CREDIT_CARD"
    INVALID_ENTITY = "CREDIT_CARD_INVALID"
    RULE_TEXT = "Luhn checksum failed"
    DIGIT_COUNTS = tuple(range(12, 20))
    GROUPED_PATTERNS = (
        ("card grouped 4-4-4-4",
         r"\b\d{4}" + _SEP + r"\d{4}" + _SEP + r"\d{4}" + _SEP + r"\d{4}\b"),
        ("card grouped amex",
         r"\b\d{4}" + _SEP + r"\d{6}" + _SEP + r"\d{5}\b"),
    )
    LABELLED_PATTERNS = (("card labeled", r"\b\d{12,19}\b"),)
    BARE_PATTERNS = (("card bare", r"\b\d{15,16}\b"),)
    context = ("credit", "card", "visa", "mastercard", "amex", "debit")

    def checksum(self, d: str) -> bool:
        return luhn_checksum(d)


# ---------------------------------------------------------------------------
# AU financial identifiers without a checksum
# ---------------------------------------------------------------------------


class AuBsbRule(PatternRule):
    """BSB codes: 3 digits, separator, 3 digits; a following account number
    makes the BSB unambiguous enough to score high without context words.

    The combined BSB+account forms emit TWO spans, not one (issue #8b,
    2026-07-22): a single AU_BSB span over both mislabeled the account half
    and broke pseudonym aliasing ('014-936 111873883' -> BSB_n hiding an
    account). The BSB patterns here match only the BSB digits with the account
    as a lookahead, and AuAccountNumberRule carries the mirror-image
    lookbehind patterns — two spans, two placeholders, so a bare '111873883'
    elsewhere aliases to the SAME ACCOUNT_n.
    """

    entity = "AU_BSB"
    patterns = (
        Pattern("bsb before account",
                r"\b\d{3}" + _SEP + r"\d{3}(?=" + _SEP_OPT + r"\d{5,10}\b)",
                0.6),
        # Transaction-description form: unseparated BSB directly followed by
        # an account number ("from 944600 000731114") — the dominant form
        # inside statement descriptions, where no context words appear.
        Pattern("bsb bare before account",
                r"\b\d{6}(?=" + _SEP + r"\d{5,10}\b)", 0.55),
        Pattern("bsb", r"\b\d{3}" + _SEP + r"\d{3}\b", 0.2),
    )
    context = ("bsb", "branch", "bank", "deposit", "transfer")


class AuAccountNumberRule(PatternRule):
    """Bare Australian bank account numbers (5-10 digits). Hopelessly
    ambiguous without context, so the base score is below threshold and only
    context words promote a match.

    The exception is the a/c label family (a/c, A/C, AC, acct — the dominant
    written form on Australian statements): the slash never survives
    tokenization into a context term, so those labels are matched inside the
    pattern itself. `validate` rejects matches carrying fewer than 5 digits in
    total — a bound regex alone cannot express that across digit groups.
    """

    entity = "AU_BANK_ACCOUNT"
    patterns = (
        Pattern("account-number", r"\b\d{5,10}\b", 0.15),
        # Space/hyphen-grouped digit runs ("0007 3111 4", "000 731 114").
        # The leading lookahead spares year ranges ("2023 2024"); the
        # (?<![.,]) / (?![.,]?\d) guards exclude a digit run that is part of a
        # formatted amount — transaction columns put the fraction of one
        # amount beside the integer of the next ("2,148.74 377,970.04DR" ->
        # "74 377"), promoted past threshold by a nearby 'LOAN'/'PAYMENT'
        # context word (issue #3).
        Pattern(
            "account grouped",
            r"\b(?<![.,])(?!(?:19|20)\d{2}[ -](?:19|20)?\d{2}\b)"
            r"\d{2,6}(?:[ -]\d{1,6}){1,3}(?![.,]?\d)\b",
            0.15,
        ),
        # Hyphenated account styles seen on real statements — confident enough
        # to strip without context words ("From A/C 30-743-3257" leaked
        # because "A/C" doesn't tokenize into a context term).
        Pattern("account 2-3-3", r"\b\d{2,4}-\d{3}-\d{3,4}\b", 0.45),
        # 6874-72521 / 289078-666 style; the lookahead spares year ranges
        # ("2023-2024", "2023-24"), the one common statement token this would
        # otherwise eat.
        Pattern(
            "account 4-5",
            r"\b(?!(?:19|20)\d{2}-(?:(?:19|20)?\d{2})\b)\d{4,6}-\d{2,6}\b",
            0.45,
        ),
        # The account half of the combined BSB+account forms (issue #8b — see
        # AuBsbRule): the preceding BSB is the unambiguity signal, carried as
        # a variable-length lookbehind. This is why the engine compiles with
        # `regex` rather than `re`.
        # Mirror-image of the BSB lookaheads above, so a widened separator on
        # one side cannot leave the other behind: a double-spaced BSB would
        # otherwise emit a BSB span with no account span beside it.
        Pattern("account after bsb",
                r"(?<=\b\d{3}" + _SEP + r"\d{3}" + _SEP_OPT + r")\d{5,10}\b",
                0.55),
        Pattern("account after bare bsb",
                r"(?<=\b\d{6}" + _SEP + r")\d{5,10}\b", 0.55),
        # "A/C 7412154728", "a/c 1234 5678", "Acct No: 000 731 114": the
        # a/c-family label matched in-span (the label lands inside the
        # placeholder — harmless, recall-first). Contiguous alternative first
        # so an unbroken run isn't truncated by the grouped one. The trailing
        # (?![.,]?\d) is the issue-#3 amount guard (issue #11): without it the
        # grouped alternative eats the integer part of a following amount
        # ('A/C 30-743-3257 148.74' -> '... 148').
        # The a/c-family form. The label moved OUT of the span on 2026-08-14
        # (it is `a/c` in `context` now, attached STRICTly): matching it in
        # span put the label inside the placeholder, which is the one place
        # the standing "a label is evidence, not part of the value" rule was
        # knowingly broken. The digit shape is unchanged, including the
        # issue-#3 amount guard.
        Pattern(
            "labeled account",
            r"\b(?:\d{5,10}|\d{1,6}(?:[ -]\d{1,6}){1,3})(?![.,]?\d)\b",
            0.5,
            attach=STRICT,
        ),
    )
    context = (
        "account", "acct", "acc", "a/c", Exact("ac"), "savings", "cheque",
        "offset", "loan", "repayment", "redraw",
    )

    def validate(self, matched: str) -> bool | None:
        """Reject fragments: a real AU account carries >=5 digits in total.
        None (not True) on pass — True would boost the score to 1.0 and bypass
        the context gating the bare patterns rely on."""
        if sum(c.isdigit() for c in matched) < 5:
            return False
        return None


class PayIdRule(PatternRule):
    """PayID identifiers. Email- and phone-form PayIDs are already caught by
    the email/phone rules; this adds the ABN-form and org-ID-form PayIDs that
    appear next to the word PayID in transaction descriptions."""

    entity = "AU_PAYID"
    patterns = (Pattern("payid-digits", r"\b\d{9,11}\b", 0.15),)
    context = ("payid", "pay id", "osko", "npp")


class AuAfslRule(PatternRule):
    """AFSL (Australian Financial Services Licence) numbers — public corporate
    identifiers from bank document footers, stripped since 2026-08-14 (Sergei:
    "for now, can be reconsidered later"). Its own class rather than a generic
    identifier so reports discriminate it from AU_DRIVERS_LICENCE, and so the
    decision can be reversed by an operator with an `[AU_AFSL]` keep section
    rather than by code. The label word is the AFSL-vs-credit-licence
    discriminator; both are 5-6 digit numbers with no public checksum.

    The alternation carries THREE label spellings, not one: the acronym
    (`AFSL`), the words (`Australian Financial Services Licence`), and the
    half-and-half form real footers print — `AFS` abbreviated with the licence
    word following it, itself spelled out or abbreviated (`AFS Licence No
    285571`, `AFS Lic 285571`). Do not collapse it back to the acronym: the
    third form matched nothing at all until 2026-08-14, because the acronym was
    the only spelling either the test or the `pii_eval` probe exercised.

    The label is a LOOKBEHIND, so the span is the digits alone — see the module
    docstring: a span covering "AFSL 233714" would key the pseudonym map on a
    different string than a bare occurrence of the same number and fork one
    licence into AFSL_1 and AFSL_2."""

    entity = "AU_AFSL"
    patterns = (Pattern("afsl labeled", r"\b\d{5,6}\b", 0.7, attach=STRICT),)
    # Three spellings, as data rather than as a regex alternation (2026-08-14).
    # `afs lic` is a STEM: a label match runs to the end of its own word, so
    # one entry covers `AFS Licence`, `AFS License`, `AFS Lic` and `AFS Lic.`,
    # and `financial services lic` covers the spelled-out form with or without
    # `Australian` in front. The filler between label and value (`No`, `#`) is
    # the shared list in `pii.core.labels`, not this rule's business.
    context = ("afsl", "afs lic", "financial services lic")


class AuCreditLicenceRule(PatternRule):
    """Australian Credit Licence numbers — the sibling of AuAfslRule (same
    rationale, same footer habitat, same label-as-lookbehind rule,
    discriminated by label word). The licence word abbreviates the same way it
    does there (`Credit Lic 234527`), and for the same reason: the siblings
    label the same kind of number in the same kind of footer, so a spelling one
    accepts and the other does not is a gap waiting to be found on a document
    rather than a distinction anyone intended."""

    entity = "AU_CREDIT_LICENCE"
    patterns = (
        Pattern("credit licence labeled", r"\b\d{5,6}\b", 0.7, attach=STRICT),
    )
    context = ("acl", "credit lic")


class AtfTailRule(PatternRule):
    """The trustee clause of '<company> ATF <trust name>' lines — issue #9.
    Real statements truncate the account-name field mid-word ('SK MANAGEMENT
    VICTORIA PTY LTD ATF SK BU'), which defeats a model's confidence on the
    truncated fragment. The connector itself is the one reliable signal, so the
    mechanical form is owned at layer 1: match 'ATF' / 'as trustee(s) for' plus
    the rest of the line (capped) as one ORGANIZATION span, so the trust name
    is covered no matter where the document cut it off. A false 'ATF' hit in
    prose costs an over-strip of one line tail (safe direction).

    What made this rule necessary has changed and it is worth being precise
    about: it used to also supply the EVIDENCE that the span was private, since
    the old policy stripped an organization only when it carried a legal-form
    marker and 'TRU'/'BU' are not 'TRUST'. Since 2026-08-11 an organization
    strips unless the keep list exempts it, so a detected trust fragment needs
    no marker. The rule survives for the other half of its job — CREATING the
    span over a fragment layer 0 may not report at all."""

    entity = "ORGANIZATION"
    patterns = (
        Pattern("atf tail", r"\b(?:atf|as\s+trustees?\s+for)\s+\S[^\n]{0,60}", 0.45),
    )


# ---------------------------------------------------------------------------
# Generic identifiers
# ---------------------------------------------------------------------------


class EmailRule(PatternRule):
    """Email addresses. The pattern is Presidio's (harvested 2026-08-09) — it
    allows internal hyphen groups per label so punycode/IDN domains match —
    and validation is a public-suffix check via `tldextract`, which is what
    keeps 'x@y' style noise out."""

    entity = "EMAIL_ADDRESS"
    patterns = (
        Pattern(
            "email",
            r"\b((([!#$%&'*+\-/=?^_`{|}~\w])|([!#$%&'*+\-/=?^_`{|}~\w]"
            r"[!#$%&'*+\-/=?^_`{|}~\.\w]{0,}[!#$%&'*+\-/=?^_`{|}~\w]))"
            r"[@]\w+(?:-+\w+)*(?:\.\w+(?:-+\w+)*)+)\b",
            0.5,
        ),
    )
    context = ("email",)

    def validate(self, matched: str) -> bool | None:
        import tldextract

        return tldextract.extract(matched).fqdn != ""


class IbanRule(PatternRule):
    """IBANs, by shape plus the ISO 13616 mod-97 check.

    A generic pattern and the checksum, rather than Presidio's ~90-entry
    per-country format table: mod-97 is what actually validates an IBAN, the
    country table only tightens the shape, and Australian documents carry
    IBANs rarely enough that the table is not worth maintaining. A wrong-shape
    value that passes mod-97 is a false positive that over-strips."""

    entity = "IBAN_CODE"
    patterns = (
        Pattern(
            "iban",
            r"\b[A-Z]{2}\d{2}(?:[ -]?[A-Za-z0-9]{4}){2,7}(?:[ -]?[A-Za-z0-9]{1,3})?\b",
            0.5,
        ),
    )
    context = ("iban", "swift", "bank")

    def validate(self, matched: str) -> bool | None:
        return iban_checksum(matched)


class PhoneRule(Rule):
    """Phone numbers via `phonenumbers` (the libphonenumber port), AU region
    only.

    AU-only on purpose (issue #11, 2026-07-22): with US in the region list,
    libphonenumber read account+amount digit runs ('A/C 30-743-3257 1.50' ->
    '3074332571') as valid US numbers, and the merge draped the phone span over
    the account, eating the amount's integer part. International '+'-prefixed
    numbers parse region-independently, so '+1 305 555 0123' still strips — the
    only sacrifice is bare US/GB-domestic-format numbers, which don't occur on
    AU statements.

    Not a PatternRule: matching is the library's, and it is a *validating*
    detector (see VALIDATED_RULES)."""

    name = "PhoneRule"
    entity = "PHONE_NUMBER"
    # `enquir` / `inquir` are STEMS, not words — context matching is
    # substring-based, so one entry covers enquiry/enquiries/enquire and the
    # US spellings. "Account enquiries 13 22 66" is how a bank prints its
    # service line, and the label is the only thing separating that number
    # from a grouped account number: AuAccountNumberRule matches the same run
    # and promotes it to 0.5 off the word "Account", while this rule's base
    # score is 0.4, so without a phone label of its own the account candidate
    # wins the merge and the service line strips as ACCOUNT_n
    # (2026-08-14, Sergei, on AmplifyBusiness-...-24Sep2023.pdf p1).
    context = ("phone", "number", "telephone", "cell", "cellphone", "mobile",
               "call", "enquir", "inquir")
    SCORE = 0.4
    REGIONS = ("AU",)
    LENIENCY = 1

    def detect(self, text: str) -> list[Detection]:
        import phonenumbers

        out = []
        for region in self.REGIONS:
            for match in phonenumbers.PhoneNumberMatcher(
                text, region, leniency=self.LENIENCY
            ):
                out.append(
                    Detection(
                        entity_type=self.entity,
                        start=match.start,
                        end=match.end,
                        score=self.SCORE,
                        recognizer=self.name,
                    )
                )
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# The checksummed rules, in the order their classes are reported.
CHECKSUM_RULES = (
    AuTfnRule,
    AuMedicareRule,
    AuMedicareMalformedRule,
    AuAbnRule,
    AuAcnRule,
    CreditCardRule,
)

# Rules whose surviving VALID detections are backed by a passing validator
# (AU checksums, Luhn, libphonenumber). A shadow finding covered by one of
# their valid detections is a correct identifier of another class, not a
# mangled one — every valid TFN fails the ACN checksum, every bare mobile
# number matches the relaxed Medicare shape. Keyed by rule NAME so an
# unvalidated guess of the same entity type can never suppress a finding.
VALIDATED_RULES = frozenset(
    {cls.__name__ for cls in CHECKSUM_RULES} | {"PhoneRule"}
)

INVALID_ENTITY_TYPES = frozenset(
    cls.INVALID_ENTITY for cls in CHECKSUM_RULES if cls.INVALID_ENTITY
)

# entity type -> the precise failed rule, for the report/log
INVALID_RULES = {
    cls.INVALID_ENTITY: cls.RULE_TEXT
    for cls in CHECKSUM_RULES
    if cls.INVALID_ENTITY
}


def build_rules(invalid_identifiers: str = "likely") -> list[Rule]:
    """Every layer-1 rule, with the invalid-collection tier applied."""
    if invalid_identifiers not in TIERS:
        raise ValueError(
            f"invalid_identifiers tier {invalid_identifiers!r}, "
            f"expected one of {TIERS}"
        )
    rules: list[Rule] = [cls(invalid_identifiers) for cls in CHECKSUM_RULES]
    rules += [
        AuBsbRule(),
        AuAccountNumberRule(),
        PayIdRule(),
        # KEPT classes (not in DEFAULT_STRIP_ENTITIES): public corporate
        # licence numbers, detected so reports discriminate them from
        # AU_DRIVERS_LICENCE.
        AuAfslRule(),
        AuCreditLicenceRule(),
        AtfTailRule(),
        EmailRule(),
        IbanRule(),
        PhoneRule(),
    ]
    return rules

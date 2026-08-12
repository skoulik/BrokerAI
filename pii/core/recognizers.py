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
from pii.core.engine import MAX_SCORE, Pattern, PatternRule, Rule

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
    # In-span patterns carry their own evidence: canonical digit grouping, or
    # a label matched as a LOOKBEHIND. The label must stay OUTSIDE the span —
    # it is evidence, not part of the value. A span covering "TFN: 123 456 782"
    # keys the pseudonym map on a different string than a bare occurrence of
    # the same TFN, so one identifier forks into TFN_1 and TFN_2 inside a
    # single document. (The old shadow recognizers matched the label in-span
    # and got away with it: an invalid candidate is reported, not aliased.)
    IN_SPAN_PATTERNS: tuple[tuple[str, str], ...] = ()
    BARE_PATTERNS: tuple[tuple[str, str], ...] = ()

    def __init__(self, tier: str = "likely") -> None:
        if tier not in TIERS:
            raise ValueError(f"invalid_identifiers tier {tier!r}, expected one of {TIERS}")
        self.tier = tier
        self._bare = {name for name, _ in self.BARE_PATTERNS}
        self.patterns = tuple(
            Pattern(name, rx, _IN_SPAN_SCORE)
            for name, rx in (*self.IN_SPAN_PATTERNS, *self.BARE_PATTERNS)
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
    IN_SPAN_PATTERNS = (
        ("tfn grouped", r"\b\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3}\b"),
        ("tfn labeled",
         r"(?<=\b(?:tfn|tax file (?:no\.?|number))\s{0,4}:?\s{0,4}#?\s{0,4})"
         r"\d{3}" + _SEP_OPT + r"\d{3}" + _SEP_OPT + r"\d{3}\b"),
    )
    BARE_PATTERNS = (("tfn bare", r"\b\d{9}\b"),)
    context = ("tax file number", "tfn")

    def checksum(self, d: str) -> bool:
        return tfn_checksum(d)


class AuMedicareRule(ChecksumRule):
    entity = "AU_MEDICARE"
    INVALID_ENTITY = "AU_MEDICARE_INVALID"
    RULE_TEXT = "Medicare mod-10 checksum failed"
    DIGIT_COUNTS = (10,)
    IN_SPAN_PATTERNS = (
        ("medicare grouped",
         r"\b[2-6]\d{3}" + _SEP + r"\d{5}" + _SEP + r"\d\b"),
        ("medicare labeled",
         r"(?<=\bmedicare\s{0,4}(?:card|no\.?|number)?\s{0,4}:?\s{0,4})"
         r"[2-6]\d{3}" + _SEP_OPT + r"\d{5}" + _SEP_OPT + r"\d\b"),
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
    IN_SPAN_PATTERNS = (
        ("medicare malformed grouped",
         r"\b[017-9]\d{3}" + _SEP + r"\d{5}" + _SEP + r"\d\b"),
        ("medicare malformed labeled",
         r"(?<=\bmedicare\s{0,4}(?:card|no\.?|number)?\s{0,4}:?\s{0,4})"
         r"[017-9]\d{3}" + _SEP_OPT + r"\d{5}" + _SEP_OPT + r"\d\b"),
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
    IN_SPAN_PATTERNS = (
        ("abn grouped",
         r"\b\d{2}" + _SEP + r"\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3}\b"),
        ("abn labeled",
         r"(?<=\babn\s{0,4}(?:no\.?|number)?\s{0,4}:?\s{0,4})"
         r"\d{2}" + _SEP_OPT + r"\d{3}" + _SEP_OPT + r"\d{3}"
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
    IN_SPAN_PATTERNS = (
        ("acn grouped", r"\b\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3}\b"),
        ("acn labeled",
         r"(?<=\bacn\s{0,4}(?:no\.?|number)?\s{0,4}:?\s{0,4})"
         r"\d{3}" + _SEP_OPT + r"\d{3}" + _SEP_OPT + r"\d{3}\b"),
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
    IN_SPAN_PATTERNS = (
        ("card grouped 4-4-4-4",
         r"\b\d{4}" + _SEP + r"\d{4}" + _SEP + r"\d{4}" + _SEP + r"\d{4}\b"),
        ("card grouped amex",
         r"\b\d{4}" + _SEP + r"\d{6}" + _SEP + r"\d{5}\b"),
        ("card labeled",
         r"(?<=\bcard\s{0,4}(?:no\.?|number)?\s{0,4}:?\s{0,4})\d{12,19}\b"),
    )
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
        Pattern(
            "labeled account",
            r"\b(?:a/?c|acct?)\b\.?\s*(?:no\.?|number|#)?\s*:?\s*"
            r"(?:\d{5,10}|\d{1,6}(?:[ -]\d{1,6}){1,3})(?![.,]?\d)\b",
            0.5,
        ),
    )
    context = (
        "account", "acct", "acc", "savings", "cheque", "offset", "loan",
        "repayment", "redraw",
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
    """AFSL (Australian Financial Services Licence) numbers — a KEPT class:
    public corporate identifiers from bank document footers, analytical value,
    not personal PII (AU_AFSL is not in DEFAULT_STRIP_ENTITIES). Detected so
    reports discriminate them from AU_DRIVERS_LICENCE. The label word is the
    AFSL-vs-credit-licence discriminator; both are 5-6 digit numbers with no
    public checksum."""

    entity = "AU_AFSL"
    patterns = (
        Pattern(
            "afsl labeled",
            r"\b(?:afsl|(?:australian\s+)?financial\s+services\s+licen[cs]e)"
            r"\s*(?:no\.?|number|#)?\s*:?\s*\d{5,6}\b",
            0.7,
        ),
    )


class AuCreditLicenceRule(PatternRule):
    """Australian Credit Licence numbers — the sibling of AuAfslRule (same
    rationale, same footer habitat, discriminated by label word). KEPT class."""

    entity = "AU_CREDIT_LICENCE"
    patterns = (
        Pattern(
            "credit licence labeled",
            r"\b(?:(?:australian\s+)?credit\s+licen[cs]e|acl)"
            r"\s*(?:no\.?|number|#)?\s*:?\s*\d{5,6}\b",
            0.7,
        ),
    )


class JointNameRule(PatternRule):
    """Joint-account INITIALS form as a layer-1 pattern: 'E & J Moore' /
    'J & E LAWRENCE'.

    Single letters carry no name signal for a model to latch onto, so the
    mechanical initials form is owned here — a deterministic floor under a
    stochastic detector. The shared-surname FULL-name form ('Julie and Brian
    Summers') is deliberately NOT a pattern (2026-07-21, issue #4): matching
    three words joined by 'and' is indistinguishable from prose by any lexical
    rule, so layer 0 owns it.

    Only guard left: an initials pair whose surname slot is a corporate marker
    ('E & J HOLDINGS') is an organization, not a couple — rejected in
    `validate`, and a corporate-tail lookahead keeps 'E & J MOORE LAWYERS'-style
    names off the pattern too. The flags drop IGNORECASE so the initials and
    surname classes stay case-sensitive (uppercase initials are the real form;
    this keeps lowercase noise like 'r & d team' from matching).
    """

    entity = "PERSON"
    flags = regex.MULTILINE | regex.DOTALL

    # A name word: capitalised, 2+ chars, allows O'Brien / Smith-Jones /
    # McDonald and their ALL-CAPS forms.
    _NAME = r"[A-Z][A-Za-z'’-]+"
    CORPORATE_WORDS = frozenset({
        "PTY", "LTD", "LIMITED", "CO", "GROUP", "TRUST", "HOLDINGS",
        "SERVICES", "CONSULTING", "MANAGEMENT", "PARTNERS", "ASSOCIATES",
        "LAWYERS", "SOLICITORS", "ACCOUNTANTS", "BROTHERS", "SONS",
        "TRADING",
    })
    _NO_CORP_TAIL = rf"(?!\s+(?i:{'|'.join(sorted(CORPORATE_WORDS))})\b)"
    patterns = (
        Pattern(
            "joint initials",
            rf"\b[A-Z]\s?&\s?[A-Z]\s+{_NAME}\b{_NO_CORP_TAIL}",
            0.5,
        ),
    )

    def validate(self, matched: str) -> bool | None:
        """Reject an initials pair whose surname slot is a corporate marker
        ('E & J HOLDINGS' is an org). None (not True) on pass — True would
        boost the score to 1.0 and erase the pattern's deliberate score."""
        if matched.split()[-1].upper() in self.CORPORATE_WORDS:
            return False
        return None


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
    context = ("phone", "number", "telephone", "cell", "cellphone", "mobile", "call")
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
        JointNameRule(),
        AtfTailRule(),
        EmailRule(),
        IbanRule(),
        PhoneRule(),
    ]
    return rules

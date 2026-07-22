"""Custom Presidio recognizers for Australian financial documents.

Presidio ships AU_TFN, AU_MEDICARE, AU_ABN, AU_ACN with checksum validation.
Added here: BSB (+ combined BSB+account), account numbers, PayID context,
and the joint-account name forms GLiNER2 loses inside transaction junk.
Recall-first: base pattern scores are low and rely on the context enhancer
(+0.35) to cross the pipeline threshold, except unambiguous combined forms.
"""

import regex

from presidio_analyzer import Pattern, PatternRecognizer


class AuBsbRecognizer(PatternRecognizer):
    """BSB codes: 3 digits, separator, 3 digits; a following account number
    makes the BSB unambiguous enough to score high without context words.

    The combined BSB+account forms used to emit ONE span labeled AU_BSB,
    which mislabeled the account half and broke pseudonym aliasing (issue
    #8b, 2026-07-22: '014-936 111873883' -> placeholder BSB_n hiding an
    account). They are now split: the BSB patterns here match only the BSB
    digits with the account as a lookahead, and AuAccountNumberRecognizer
    carries the mirror-image lookbehind patterns for the account half — two
    spans, two placeholders, so a bare '111873883' elsewhere aliases to the
    SAME ACCOUNT_n. (When GLiNER2 also emits the combined run as one
    account guess, _merge_overlaps unions the split back into a single
    AU_BANK_ACCOUNT span — still an account label, never BSB-over-account.)
    """

    PATTERNS = [
        Pattern(
            "bsb before account",
            r"\b\d{3}[- ]\d{3}(?=[- ]?\s?\d{5,10}\b)",
            0.6,
        ),
        # Transaction-description form: unseparated BSB directly followed by
        # an account number ("from 944600 000731114"). Found leaking by the
        # Tier-1 corpus (pii_eval) — the dominant form inside statement
        # descriptions, where no context words appear.
        Pattern("bsb bare before account", r"\b\d{6}(?=[ -]\d{5,10}\b)", 0.55),
        Pattern("bsb", r"\b\d{3}[- ]\d{3}\b", 0.2),
    ]
    CONTEXT = ["bsb", "branch", "bank", "deposit", "transfer"]

    def __init__(self, **kwargs):
        super().__init__(
            supported_entity="AU_BSB",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name="AuBsbRecognizer",
            **kwargs,
        )


class AuAccountNumberRecognizer(PatternRecognizer):
    """Bare Australian bank account numbers (5-10 digits). Hopelessly
    ambiguous without context, so the base score is below threshold and only
    context words (account, acct...) promote a match.

    Space/hyphen-grouped forms ("1234 5678", "000 731 114") follow the same
    idiom: sub-threshold bare pattern + context promotion. The exception is
    the a/c label family (a/c, A/C, AC, acct — the dominant written form on
    Australian statements): the slash never survives tokenization into a
    context term, so those labels are matched inside the pattern itself.
    validate_result rejects matches carrying fewer than 5 digits in total —
    a bound regex alone can't express across digit groups (2026-07-14)."""

    PATTERNS = [
        Pattern("account-number", r"\b\d{5,10}\b", 0.15),
        # Space/hyphen-grouped digit runs ("0007 3111 4", "000 731 114").
        # Context-promoted like the bare run above; the leading lookahead
        # spares year ranges ("2023 2024") — observed FP: "account statement
        # period 2023 2024" promotes via the word 'account'. The (?<![.,])
        # / (?![.,]?\d) guards exclude a digit run that is part of a
        # formatted decimal/thousands amount: transaction columns put the
        # fraction of one amount beside the integer of the next
        # ("2,148.74 377,970.04DR" -> "74 377"), promoted past threshold by
        # the nearby 'LOAN'/'PAYMENT' context word (issue #3).
        Pattern(
            "account grouped",
            r"\b(?<![.,])(?!(?:19|20)\d{2}[ -](?:19|20)?\d{2}\b)"
            r"\d{2,6}(?:[ -]\d{1,6}){1,3}(?![.,]?\d)\b",
            0.15,
        ),
        # Hyphenated account styles seen on real statements — confident
        # enough to strip without context words ("From A/C 30-743-3257"
        # leaked because "A/C" doesn't tokenize into a context term):
        # 17-182-278 / 32-151-6825 style
        Pattern("account 2-3-3", r"\b\d{2,4}-\d{3}-\d{3,4}\b", 0.45),
        # 6874-72521 / 289078-666 style; the lookahead spares year ranges
        # ("2023-2024", "2023-24") — the one common statement token this
        # would eat.
        Pattern(
            "account 4-5",
            r"\b(?!(?:19|20)\d{2}-(?:(?:19|20)?\d{2})\b)\d{4,6}-\d{2,6}\b",
            0.45,
        ),
        # The account half of the combined BSB+account forms (issue #8b —
        # split spans, see AuBsbRecognizer): the preceding BSB is the
        # unambiguity signal, carried as a variable-length lookbehind
        # (presidio compiles with the `regex` library, which supports it).
        Pattern(
            "account after bsb",
            r"(?<=\b\d{3}[- ]\d{3}[- ]?\s?)\d{5,10}\b",
            0.55,
        ),
        Pattern("account after bare bsb", r"(?<=\b\d{6}[ -])\d{5,10}\b", 0.55),
        # "A/C 7412154728", "a/c 1234 5678", "Acct No: 000 731 114": the
        # a/c-family label matched in-span (see class docstring; the label
        # lands inside the placeholder — harmless, recall-first). Contiguous
        # alternative first so an unbroken run isn't truncated by the
        # grouped one. The trailing (?![.,]?\d) is the issue-#3 amount guard
        # (issue #11, 2026-07-22): without it the grouped alternative eats
        # the integer part of a following amount ('A/C 30-743-3257 148.74'
        # -> '... 148'); with it the regex backtracks to drop the amount
        # group, so the account itself still matches in full.
        Pattern(
            "labeled account",
            r"(?i)\b(?:a/?c|acct?)\b\.?\s*(?:no\.?|number|#)?\s*:?\s*"
            r"(?:\d{5,10}|\d{1,6}(?:[ -]\d{1,6}){1,3})(?![.,]?\d)\b",
            0.5,
        ),
    ]

    def validate_result(self, pattern_text: str) -> bool | None:
        """Reject fragments: a real AU account carries >=5 digits in total.
        None (not True) on pass — True would boost the score to 1.0 and
        bypass the context gating the bare patterns rely on."""
        if sum(c.isdigit() for c in pattern_text) < 5:
            return False
        return None
    CONTEXT = [
        "account", "acct", "acc", "savings", "cheque", "offset", "loan",
        "repayment", "redraw",
    ]

    def __init__(self, **kwargs):
        super().__init__(
            supported_entity="AU_BANK_ACCOUNT",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name="AuAccountNumberRecognizer",
            **kwargs,
        )


class JointNameRecognizer(PatternRecognizer):
    """Joint-account INITIALS form as a layer-1 pattern: 'E & J Moore' /
    'J & E LAWRENCE'.

    GLiNER2 loses this form in transaction-line junk — it drops the bare
    initials and glues the surname to an adjacent keyword ('LAWRENCE RENT',
    the 2026-07-15 diagnostic in DONE.md) — and single letters carry no name
    signal for the model to latch onto, so the mechanical initials form is
    owned here.

    The shared-surname FULL-name form ('Julie and Brian Summers') is NOT a
    pattern any more (2026-07-21, issue #4): matching three words joined by
    'and' is indistinguishable from prose ('CAREFULLY AND IMMEDIATELY NOTIFY')
    by any lexical rule — capitalisation is no signal on ALL-CAPS statements,
    and a vocabulary stop-list can't be complete. GLiNER2 emits a PERSON
    fragment on each side of the connector for real names, so the full form is
    now handled by expanding those detections — the joint-connector merge in
    gliner2_recognizer._mergeable — which is name-signal-gated by construction.

    Only guard left: an initials pair whose surname slot is a corporate marker
    ('E & J HOLDINGS') is an organization, not a couple — rejected in
    validate_result, and a corporate-tail lookahead keeps 'E & J MOORE
    LAWYERS'-style names off the pattern too. __init__ drops presidio's default
    IGNORECASE so the initials '[A-Z]' and surname '[A-Z]...' classes stay
    case-sensitive (uppercase initials are the real form; this keeps lowercase
    noise like 'r & d team' from matching)."""

    # A name word: capitalised, 2+ chars, allows O'Brien / Smith-Jones /
    # McDonald and their ALL-CAPS forms.
    _NAME = r"[A-Z][A-Za-z'’-]+"
    # Corporate markers — rejected in the surname slot, and looked ahead for
    # right past the match (an initials-fronted organization, not a couple).
    CORPORATE_WORDS = {
        "PTY", "LTD", "LIMITED", "CO", "GROUP", "TRUST", "HOLDINGS",
        "SERVICES", "CONSULTING", "MANAGEMENT", "PARTNERS", "ASSOCIATES",
        "LAWYERS", "SOLICITORS", "ACCOUNTANTS", "BROTHERS", "SONS",
        "TRADING",
    }
    _NO_CORP_TAIL = rf"(?!\s+(?i:{'|'.join(sorted(CORPORATE_WORDS))})\b)"
    PATTERNS = [
        Pattern(
            "joint initials",
            rf"\b[A-Z]\s?&\s?[A-Z]\s+{_NAME}\b{_NO_CORP_TAIL}",
            0.5,
        ),
    ]

    def validate_result(self, pattern_text: str) -> bool | None:
        """Reject an initials pair whose surname slot is a corporate marker
        ('E & J HOLDINGS' is an org). None (not True) on pass — True would
        boost the score to 1.0 and erase the pattern's deliberate score."""
        tokens = [t.upper() for t in pattern_text.split()]
        if tokens[-1] in self.CORPORATE_WORDS:
            return False
        return None

    def __init__(self, **kwargs):
        super().__init__(
            supported_entity="PERSON",
            patterns=self.PATTERNS,
            name="JointNameRecognizer",
            # Drop presidio's default IGNORECASE so the initials '[A-Z]' and
            # surname classes stay case-sensitive (issue #4).
            global_regex_flags=regex.MULTILINE | regex.DOTALL,
            **kwargs,
        )


class AuAfslRecognizer(PatternRecognizer):
    """AFSL (Australian Financial Services Licence) numbers — issue #8c /
    review other-finding #1 (2026-07-22). A KEPT class: public corporate
    identifiers from bank document footers, analytical value, not personal
    PII — AU_AFSL is not in DEFAULT_STRIP_ENTITIES. Detected so reports
    discriminate them from AU_DRIVERS_LICENCE (GLiNER2 used to label the
    bare numbers 'driver licence'; its corporate-licence context guard is
    the strip-suppression half of this fix). The label word is the
    AFSL-vs-credit-licence discriminator; both are 5-6 digit numbers with
    no public checksum."""

    PATTERNS = [
        Pattern(
            "afsl labeled",
            r"\b(?:afsl|(?:australian\s+)?financial\s+services\s+licen[cs]e)"
            r"\s*(?:no\.?|number|#)?\s*:?\s*\d{5,6}\b",
            0.7,
        ),
    ]

    def __init__(self, **kwargs):
        super().__init__(
            supported_entity="AU_AFSL",
            patterns=self.PATTERNS,
            name="AuAfslRecognizer",
            **kwargs,
        )


class AuCreditLicenceRecognizer(PatternRecognizer):
    """Australian Credit Licence numbers — the sibling of AuAfslRecognizer
    (same rationale, same footer habitat, discriminated by label word).
    KEPT class: AU_CREDIT_LICENCE is not in DEFAULT_STRIP_ENTITIES."""

    PATTERNS = [
        Pattern(
            "credit licence labeled",
            r"\b(?:(?:australian\s+)?credit\s+licen[cs]e|acl)"
            r"\s*(?:no\.?|number|#)?\s*:?\s*\d{5,6}\b",
            0.7,
        ),
    ]

    def __init__(self, **kwargs):
        super().__init__(
            supported_entity="AU_CREDIT_LICENCE",
            patterns=self.PATTERNS,
            name="AuCreditLicenceRecognizer",
            **kwargs,
        )


class PayIdRecognizer(PatternRecognizer):
    """PayID identifiers. Email- and phone-form PayIDs are already caught by
    the EMAIL_ADDRESS / PHONE_NUMBER recognizers; this adds the ABN-form and
    org-ID-form PayIDs that appear next to the word PayID in transaction
    descriptions."""

    PATTERNS = [
        # An 11-digit ABN-form PayID or an OrgId up to 254 chars is too broad
        # to pattern-match alone; catch digit runs near the PayID keyword.
        Pattern("payid-digits", r"\b\d{9,11}\b", 0.15),
    ]
    CONTEXT = ["payid", "pay id", "osko", "npp"]

    def __init__(self, **kwargs):
        super().__init__(
            supported_entity="AU_PAYID",
            patterns=self.PATTERNS,
            context=self.CONTEXT,
            name="PayIdRecognizer",
            **kwargs,
        )

"""Custom Presidio recognizers for Australian entities.

Presidio ships AU_TFN, AU_MEDICARE, AU_ABN, AU_ACN with checksum validation.
Added here: BSB (+ combined BSB+account), account numbers, and PayID context.
Recall-first: base pattern scores are low and rely on the context enhancer
(+0.35) to cross the pipeline threshold, except unambiguous combined forms.
"""

from presidio_analyzer import Pattern, PatternRecognizer


class AuBsbRecognizer(PatternRecognizer):
    """BSB codes: 3 digits, separator, 3 digits; optionally followed by an
    account number (the combined form is unambiguous enough to score high
    without context words)."""

    PATTERNS = [
        Pattern("bsb+account", r"\b\d{3}[- ]\d{3}[- ]?\s?\d{5,10}\b", 0.6),
        # Transaction-description form: unseparated BSB directly followed by
        # an account number ("from 944600 000731114"). Found leaking by the
        # Tier-1 corpus (pii_eval) — the dominant form inside statement
        # descriptions, where no context words appear.
        Pattern("bsb+account bare", r"\b\d{6}[ -]\d{5,10}\b", 0.55),
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
        # Context-promoted like the bare run above; the lookahead spares
        # year ranges ("2023 2024") — observed FP: "account statement
        # period 2023 2024" promotes via the word 'account'.
        Pattern(
            "account grouped",
            r"\b(?!(?:19|20)\d{2}[ -](?:19|20)?\d{2}\b)"
            r"\d{2,6}(?:[ -]\d{1,6}){1,3}\b",
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
        # "A/C 7412154728", "a/c 1234 5678", "Acct No: 000 731 114": the
        # a/c-family label matched in-span (see class docstring; the label
        # lands inside the placeholder — harmless, recall-first). Contiguous
        # alternative first so an unbroken run isn't truncated by the
        # grouped one.
        Pattern(
            "labeled account",
            r"(?i)\b(?:a/?c|acct?)\b\.?\s*(?:no\.?|number|#)?\s*:?\s*"
            r"(?:\d{5,10}|\d{1,6}(?:[ -]\d{1,6}){1,3})\b",
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

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
    context words (account, acct...) promote a match."""

    PATTERNS = [
        Pattern("account-number", r"\b\d{5,10}\b", 0.15),
    ]
    CONTEXT = ["account", "acct", "acc", "savings", "cheque", "offset", "loan"]

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

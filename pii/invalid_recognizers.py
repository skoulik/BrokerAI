"""Shadow recognizers for checksum-invalid identifier candidates.

Each recognizer mirrors a checksum-validated one (Presidio's AU_TFN,
AU_MEDICARE, AU_ABN, AU_ACN, CREDIT_CARD) but with the validation
INVERTED: it emits only when the mirrored rule FAILS, under a distinct
entity type. Purpose (ROADMAP): surface typos, wrong OCR output and
outright forgery — a value that looks exactly like a TFN but fails the
mod-11 arithmetic is one of those three.

Two placeholder classes per identifier, by failure mode:
- ``*_INVALID``   — pattern matches, checksum fails (inverted validation);
- ``*_MALFORMED`` — structurally impossible (a RELAXED shadow pattern for
  shapes the strict pattern refuses, e.g. a Medicare first digit outside
  2-6 — such values never reach the real validator at all).

Collection tiers are pure per-pattern base-score configuration on top of
the pipeline threshold (0.4 by default); no Presidio hooks, no extra pass:
- ``likely``  — only in-span evidence collects: canonical digit grouping
  ("123 456 782") or an immediately adjacent label captured by the regex
  itself ("TFN: 123456780"). Purely lexical; accidental digit runs almost
  never carry either. In-span patterns score 0.5.
- ``context`` — additionally, bare digit runs promoted by nearby context
  words: base 0.15 + Presidio's lemma context enhancer (+0.35), exactly
  the AuAccountNumberRecognizer mechanism (label in a form header, value
  in a cell — patterns can't reach that; the enhancer can).
- ``all``     — every pattern match that fails its rule (bare runs at
  0.5). Noisy by design: ~90% of random 9-digit runs fail the TFN
  checksum.
The tiers are cumulative (each includes the previous); ``ignore``
registers nothing — the historical silent drop.
"""

from presidio_analyzer import Pattern, PatternRecognizer

TIERS = ("ignore", "likely", "context", "all")

_IN_SPAN_SCORE = 0.5
_BARE_SCORE = {"likely": None, "context": 0.15, "all": 0.5}

# Recognizers whose surviving detections are backed by a PASSING validator
# (AU checksums, Luhn, libphonenumber). A shadow finding COVERED by one of
# their detections is a valid identifier of another class, not a mangled
# one — e.g. every valid TFN fails the ACN checksum, every bare mobile
# number matches the relaxed Medicare shape — and is not collected.
# Keyed by recognizer NAME, not entity type: GLiNER2 emits the same types
# (PHONE_NUMBER, CREDIT_CARD) as unvalidated guesses, and an NER guess
# must never suppress a finding.
VALIDATED_RECOGNIZERS = {
    "AuTfnRecognizer", "AuMedicareRecognizer", "AuAbnRecognizer",
    "AuAcnRecognizer", "CreditCardRecognizer", "PhoneRecognizer",
}


def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def _tfn_checksum(d: str) -> bool:
    weights = (1, 4, 3, 7, 5, 8, 6, 9, 10)
    return sum(w * int(x) for w, x in zip(weights, d)) % 11 == 0


def _medicare_checksum(d: str) -> bool:
    weights = (1, 3, 7, 9, 1, 3, 7, 9)
    return sum(w * int(x) for w, x in zip(weights, d)) % 10 == int(d[8])


def _abn_checksum(d: str) -> bool:
    weights = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    nums = [int(x) for x in d]
    nums[0] = 9 if nums[0] == 0 else nums[0] - 1
    return sum(w * x for w, x in zip(weights, nums)) % 89 == 0


def _acn_checksum(d: str) -> bool:
    weights = (8, 7, 6, 5, 4, 3, 2, 1)
    remainder = sum(w * int(x) for w, x in zip(weights, d)) % 10
    return (10 - remainder) % 10 == int(d[8])


def _luhn_checksum(d: str) -> bool:
    total = 0
    for i, c in enumerate(reversed(d)):
        x = int(c)
        if i % 2 == 1:
            x *= 2
        total += x - 9 if x > 9 else x
    return total % 10 == 0


class _ShadowRecognizer(PatternRecognizer):
    """Base: tier-dependent pattern set, inverted validation.

    Subclasses define ENTITY, RULE (human-readable failed rule for the
    report), IN_SPAN_PATTERNS / BARE_PATTERNS as (name, regex) pairs,
    CONTEXT words, and rule_fails(digits).
    """

    ENTITY: str
    RULE: str
    IN_SPAN_PATTERNS: list
    BARE_PATTERNS: list
    CONTEXT: list

    def __init__(self, tier: str, **kwargs):
        patterns = [
            Pattern(name, regex, _IN_SPAN_SCORE)
            for name, regex in self.IN_SPAN_PATTERNS
        ]
        bare_score = _BARE_SCORE[tier]
        if bare_score is not None:
            patterns += [
                Pattern(name, regex, bare_score)
                for name, regex in self.BARE_PATTERNS
            ]
        super().__init__(
            supported_entity=self.ENTITY,
            patterns=patterns,
            context=self.CONTEXT,
            name=type(self).__name__,
            **kwargs,
        )

    def rule_fails(self, d: str) -> bool:
        raise NotImplementedError

    def validate_result(self, pattern_text: str):
        # Inverted: None (keep the pattern's tier score, context enhancer
        # still applies) when the mirrored rule FAILS; False (drop) when it
        # passes — the real recognizer owns that match.
        return None if self.rule_fails(_digits(pattern_text)) else False


class InvalidAuTfnRecognizer(_ShadowRecognizer):
    ENTITY = "AU_TFN_INVALID"
    RULE = "TFN mod-11 checksum failed"
    IN_SPAN_PATTERNS = [
        ("tfn grouped", r"\b\d{3}[- ]\d{3}[- ]\d{3}\b"),
        ("tfn labeled",
         r"(?i)\b(?:tfn|tax file (?:no\.?|number))\s*:?\s*#?\s*"
         r"\d{3}[- ]?\d{3}[- ]?\d{3}\b"),
    ]
    BARE_PATTERNS = [("tfn bare", r"\b\d{9}\b")]
    CONTEXT = ["tax file number", "tfn"]

    def rule_fails(self, d: str) -> bool:
        return len(d) == 9 and not _tfn_checksum(d)


class InvalidAuMedicareRecognizer(_ShadowRecognizer):
    ENTITY = "AU_MEDICARE_INVALID"
    RULE = "Medicare mod-10 checksum failed"
    IN_SPAN_PATTERNS = [
        ("medicare grouped", r"\b[2-6]\d{3} \d{5} \d\b"),
        ("medicare labeled",
         r"(?i)\bmedicare\s*(?:card|no\.?|number)?\s*:?\s*"
         r"[2-6]\d{3}[- ]?\d{5}[- ]?\d\b"),
    ]
    BARE_PATTERNS = [("medicare bare", r"\b[2-6]\d{9}\b")]
    CONTEXT = ["medicare"]

    def rule_fails(self, d: str) -> bool:
        return len(d) == 10 and not _medicare_checksum(d)


class MalformedAuMedicareRecognizer(_ShadowRecognizer):
    """RELAXED shadow: first digit outside 2-6 is structurally impossible,
    so the strict Medicare pattern never lets such values reach the
    validator — this pattern class exists precisely to see them. The
    checksum is irrelevant: the structure alone invalidates the value."""

    ENTITY = "AU_MEDICARE_MALFORMED"
    RULE = "Medicare first digit outside 2-6 (structurally impossible)"
    IN_SPAN_PATTERNS = [
        ("medicare malformed grouped", r"\b[017-9]\d{3} \d{5} \d\b"),
        ("medicare malformed labeled",
         r"(?i)\bmedicare\s*(?:card|no\.?|number)?\s*:?\s*"
         r"[017-9]\d{3}[- ]?\d{5}[- ]?\d\b"),
    ]
    BARE_PATTERNS = [("medicare malformed bare", r"\b[017-9]\d{9}\b")]
    CONTEXT = ["medicare"]

    def rule_fails(self, d: str) -> bool:
        return True  # malformed by pattern construction


class InvalidAuAbnRecognizer(_ShadowRecognizer):
    ENTITY = "AU_ABN_INVALID"
    RULE = "ABN mod-89 checksum failed"
    IN_SPAN_PATTERNS = [
        ("abn grouped", r"\b\d{2}[- ]\d{3}[- ]\d{3}[- ]\d{3}\b"),
        ("abn labeled",
         r"(?i)\babn\s*(?:no\.?|number)?\s*:?\s*"
         r"\d{2}[- ]?\d{3}[- ]?\d{3}[- ]?\d{3}\b"),
    ]
    BARE_PATTERNS = [("abn bare", r"\b\d{11}\b")]
    CONTEXT = ["australian business number", "abn"]

    def rule_fails(self, d: str) -> bool:
        return len(d) == 11 and not _abn_checksum(d)


class InvalidAuAcnRecognizer(_ShadowRecognizer):
    ENTITY = "AU_ACN_INVALID"
    RULE = "ACN complement checksum failed"
    IN_SPAN_PATTERNS = [
        ("acn grouped", r"\b\d{3}[- ]\d{3}[- ]\d{3}\b"),
        ("acn labeled",
         r"(?i)\bacn\s*(?:no\.?|number)?\s*:?\s*"
         r"\d{3}[- ]?\d{3}[- ]?\d{3}\b"),
    ]
    BARE_PATTERNS = [("acn bare", r"\b\d{9}\b")]
    CONTEXT = ["australian company number", "acn"]

    def rule_fails(self, d: str) -> bool:
        return len(d) == 9 and not _acn_checksum(d)


class InvalidCreditCardRecognizer(_ShadowRecognizer):
    ENTITY = "CREDIT_CARD_INVALID"
    RULE = "Luhn checksum failed"
    IN_SPAN_PATTERNS = [
        ("card grouped 4-4-4-4", r"\b\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}\b"),
        ("card grouped amex", r"\b\d{4}[- ]\d{6}[- ]\d{5}\b"),
        ("card labeled",
         r"(?i)\bcard\s*(?:no\.?|number)?\s*:?\s*\d{12,19}\b"),
    ]
    BARE_PATTERNS = [("card bare", r"\b\d{15,16}\b")]
    CONTEXT = ["credit", "card", "visa", "mastercard", "amex", "debit"]

    def rule_fails(self, d: str) -> bool:
        return 12 <= len(d) <= 19 and not _luhn_checksum(d)


_RECOGNIZERS = (
    InvalidAuTfnRecognizer,
    InvalidAuMedicareRecognizer,
    MalformedAuMedicareRecognizer,
    InvalidAuAbnRecognizer,
    InvalidAuAcnRecognizer,
    InvalidCreditCardRecognizer,
)

INVALID_ENTITY_TYPES = {cls.ENTITY for cls in _RECOGNIZERS}

# entity type -> the precise failed rule, for the report/log
INVALID_RULES = {cls.ENTITY: cls.RULE for cls in _RECOGNIZERS}


def make_invalid_recognizers(tier: str) -> list:
    if tier not in TIERS:
        raise ValueError(f"invalid_identifiers tier {tier!r}, expected one of {TIERS}")
    if tier == "ignore":
        return []
    return [cls(tier) for cls in _RECOGNIZERS]

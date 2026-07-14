"""Layered PII detection + pseudonymization pipeline.

Layer 1: Presidio pattern/checksum recognizers — built-in AU_TFN, AU_ABN,
         AU_ACN, AU_MEDICARE, credit cards, emails, URLs, IPs — plus the
         custom AU recognizers in pii.recognizers (BSB, account, PayID) and
         an AU-region phone recognizer.
Layer 2: zero-shot NER (names, addresses, DOB, person-vs-org) — GLiNER2.
Layer 3 (future): local-LLM audit pass via llama-server.

Detected spans are resolved for overlaps and replaced with consistent
placeholders from a PseudonymMap.

Checksum-invalid identifier candidates (a value shaped like a TFN whose
mod-11 arithmetic fails — a typo, bad OCR, or forgery) are collected by
the shadow recognizers in pii.invalid_recognizers, controlled by the
`invalid_identifiers` tier, and returned by detect()/strip() as
InvalidFinding records. They are only *masked* when mask_invalid=True
adds the invalid classes to strip_entities. The findings are near-PII
(a typo'd TFN is a real TFN minus a digit) — treat any log of them as a
local-only artifact, like the pseudonym map.
"""

from dataclasses import dataclass

from presidio_analyzer import (
    AnalyzerEngine,
    RecognizerRegistry,
    RecognizerResult,
)
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import (
    AuAbnRecognizer,
    AuAcnRecognizer,
    AuMedicareRecognizer,
    AuTfnRecognizer,
    PhoneRecognizer,
)

from pii.invalid_recognizers import (
    INVALID_ENTITY_TYPES,
    INVALID_RULES,
    VALIDATED_RECOGNIZERS,
    make_invalid_recognizers,
)
from pii.mapping import PseudonymMap
from pii.recognizers import (
    AuAccountNumberRecognizer,
    AuBsbRecognizer,
    PayIdRecognizer,
)

# Entities replaced by default. Detected-but-kept by default: ORGANIZATION
# (merchant names carry analytical value in bank statements), DATE_TIME
# (transaction dates; DATE_OF_BIRTH is stripped separately).
DEFAULT_STRIP_ENTITIES = {
    "PERSON",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "AU_TFN",
    "AU_MEDICARE",
    "AU_ABN",
    "AU_ACN",
    "AU_BSB",
    "AU_BANK_ACCOUNT",
    "AU_PAYID",
    "AU_DRIVERS_LICENCE",
    "PASSPORT",
    "CREDIT_CARD",
    "LOCATION",
    "ADDRESS",
    "DATE_OF_BIRTH",
    "IBAN_CODE",
    "IP_ADDRESS",
    "URL",
}

NLP_CONFIG = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
}


@dataclass
class InvalidFinding:
    """A collected checksum-invalid identifier candidate. Near-PII —
    local-only, like the pseudonym map."""

    entity_type: str
    start: int
    end: int
    value: str
    rule: str  # the precise failed rule, for the report


class PiiPipeline:
    def __init__(
        self,
        use_ner: bool = True,
        threshold: float = 0.4,
        strip_entities: set[str] | None = None,
        invalid_identifiers: str = "likely",
        mask_invalid: bool = False,
    ):
        self.threshold = threshold
        self.strip_entities = (
            set(strip_entities) if strip_entities is not None
            else set(DEFAULT_STRIP_ENTITIES)
        )
        if mask_invalid:
            # masking is nothing more than stripping the invalid classes
            self.strip_entities |= INVALID_ENTITY_TYPES
        nlp_engine = NlpEngineProvider(nlp_configuration=NLP_CONFIG).create_engine()
        registry = RecognizerRegistry(supported_languages=["en"])
        registry.load_predefined_recognizers(nlp_engine=nlp_engine)
        # Default phone regions don't include AU.
        registry.remove_recognizer("PhoneRecognizer")
        registry.add_recognizer(
            PhoneRecognizer(supported_regions=("AU", "US", "GB"))
        )
        # The AU checksum recognizers aren't part of the default registry load.
        for recognizer_cls in (
            AuTfnRecognizer,
            AuMedicareRecognizer,
            AuAbnRecognizer,
            AuAcnRecognizer,
        ):
            registry.add_recognizer(recognizer_cls())
        registry.add_recognizer(AuBsbRecognizer())
        registry.add_recognizer(AuAccountNumberRecognizer())
        registry.add_recognizer(PayIdRecognizer())
        for recognizer in make_invalid_recognizers(invalid_identifiers):
            registry.add_recognizer(recognizer)
        if use_ner:
            from pii.gliner2_recognizer import Gliner2Recognizer

            registry.add_recognizer(Gliner2Recognizer())
        self.analyzer = AnalyzerEngine(
            nlp_engine=nlp_engine, registry=registry, supported_languages=["en"]
        )

    def analyze(self, text: str):
        """All detections above threshold, overlap-resolved, sorted by
        position — including entity types that strip() would keep."""
        results = self.analyzer.analyze(
            text=text, language="en", score_threshold=self.threshold
        )
        return _resolve_overlaps(results)

    def detect(self, text: str) -> tuple[list, list[InvalidFinding]]:
        """One analyzer pass -> (strip plan, checksum-invalid findings).

        The plan is what strip() replaces: strip-listed entities only,
        overlaps merged. Filter to strip-listed entities BEFORE overlap
        handling: a kept-type span (e.g. a bogus high-score DATE_TIME from
        spacy) must never shadow an overlapping PII span, or the PII
        leaks. Then MERGE overlapping spans rather than picking a winner —
        a small high-score span (BSB, 0.55) must not evict a wider
        covering span (account number, 0.52) and expose the rest of it.
        """
        results = self.analyzer.analyze(
            text=text, language="en", score_threshold=self.threshold
        )
        plan = _merge_overlaps(
            [r for r in results if r.entity_type in self.strip_entities]
        )
        return plan, _collect_invalid(results, text)

    def plan(self, text: str) -> list:
        """The spans strip() would replace."""
        return self.detect(text)[0]

    def strip(
        self, text: str, pmap: PseudonymMap
    ) -> tuple[str, list, list[InvalidFinding]]:
        """Replace detected PII with consistent placeholders.

        Returns (stripped_text, applied_detections, invalid_findings).
        """
        spans, invalid = self.detect(text)
        # Allocate placeholders in document order (readable numbering), then
        # splice right-to-left so earlier offsets stay valid.
        placeholders = [
            pmap.placeholder_for(r.entity_type, text[r.start : r.end])
            for r in spans
        ]
        out = text
        for r, placeholder in sorted(
            zip(spans, placeholders), key=lambda p: p[0].start, reverse=True
        ):
            out = out[: r.start] + placeholder + out[r.end :]
        return out, spans, invalid


def _resolve_overlaps(results):
    """Keep the higher-scored (then longer) span among overlapping ones.
    Used by analyze() for a readable debug view; strip() merges instead."""
    kept = []
    for r in sorted(results, key=lambda r: (-r.score, r.start - r.end)):
        if all(r.end <= k.start or r.start >= k.end for k in kept):
            kept.append(r)
    return sorted(kept, key=lambda r: r.start)


def _rank(r) -> tuple:
    """Merge ranking: any valid entity type outranks any invalid class
    regardless of score (recall-first — an invalid-class candidate must
    never claim the placeholder from a real detection), score breaks ties
    within a class."""
    return (r.entity_type not in INVALID_ENTITY_TYPES, r.score)


def _merge_overlaps(results):
    """Union overlapping spans into one; the merged span takes the
    highest-ranked member's entity type (see _rank). Recall-first:
    everything any span covered gets replaced."""
    merged = []
    for r in sorted(results, key=lambda r: (r.start, -r.end)):
        last = merged[-1] if merged else None
        if last is not None and r.start < last.end:
            last.end = max(last.end, r.end)
            if _rank(r) > _rank(last):
                last.score = r.score
                last.entity_type = r.entity_type
        else:
            merged.append(
                RecognizerResult(
                    entity_type=r.entity_type,
                    start=r.start,
                    end=r.end,
                    score=r.score,
                )
            )
    return merged


def _collect_invalid(results, text: str) -> list[InvalidFinding]:
    """Invalid-class detections worth reporting.

    Two filters:
    - a candidate COVERED by a validated detection (VALIDATED_RECOGNIZERS —
      checksummed identifiers, Luhn cards, libphonenumber phones; matched
      by recognizer name so an NER guess of the same entity type never
      suppresses) is a valid identifier of another class, not a mangled
      one: every valid TFN fails the ACN checksum, every bare mobile
      number matches the relaxed Medicare shape;
    - a candidate strictly contained in a longer invalid candidate is
      grouped-fragment noise ("715 451 689" inside "64 715 451 689").
      Identical spans are all kept — each records a distinct failed rule.
    """
    inv = [r for r in results if r.entity_type in INVALID_ENTITY_TYPES]
    if not inv:
        return []
    validated = [
        r
        for r in results
        if (r.recognition_metadata or {}).get(
            RecognizerResult.RECOGNIZER_NAME_KEY
        )
        in VALIDATED_RECOGNIZERS
    ]
    inv = [
        r
        for r in inv
        if not any(v.start <= r.start and r.end <= v.end for v in validated)
    ]
    kept = []
    for r in sorted(inv, key=lambda r: (r.start - r.end, r.start)):  # longest first
        contained = any(
            k.start <= r.start and r.end <= k.end
            and (k.start, k.end) != (r.start, r.end)
            for k in kept
        )
        if not contained:
            kept.append(r)
    return sorted(
        (
            InvalidFinding(
                entity_type=r.entity_type,
                start=r.start,
                end=r.end,
                value=text[r.start : r.end],
                rule=INVALID_RULES[r.entity_type],
            )
            for r in kept
        ),
        key=lambda f: (f.start, f.entity_type),
    )

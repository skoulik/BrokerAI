"""Layered PII detection + pseudonymization pipeline.

Layer 1: Presidio pattern/checksum recognizers — built-in AU_TFN, AU_ABN,
         AU_ACN, AU_MEDICARE, credit cards, emails, URLs, IPs — plus the
         custom AU recognizers in pii.recognizers (BSB, account, PayID) and
         an AU-region phone recognizer.
Layer 2: GLiNER zero-shot NER (names, addresses, DOB, person-vs-org).
Layer 3 (future): local-LLM audit pass via llama-server.

Detected spans are resolved for overlaps and replaced with consistent
placeholders from a PseudonymMap.
"""

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


class PiiPipeline:
    def __init__(
        self,
        use_ner: bool = True,
        threshold: float = 0.4,
        strip_entities: set[str] | None = None,
    ):
        self.threshold = threshold
        self.strip_entities = (
            set(strip_entities) if strip_entities is not None
            else set(DEFAULT_STRIP_ENTITIES)
        )
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
        if use_ner:
            from pii.gliner_recognizer import GlinerRecognizer

            registry.add_recognizer(GlinerRecognizer())
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

    def plan(self, text: str) -> list:
        """The spans strip() would replace: strip-listed entities only,
        overlaps merged.

        Filter to strip-listed entities BEFORE overlap handling: a
        kept-type span (e.g. a bogus high-score DATE_TIME from spacy) must
        never shadow an overlapping PII span, or the PII leaks. Then MERGE
        overlapping spans rather than picking a winner — a small
        high-score span (BSB, 0.55) must not evict a wider covering span
        (account number, 0.52) and expose the rest of it.
        """
        results = self.analyzer.analyze(
            text=text, language="en", score_threshold=self.threshold
        )
        return _merge_overlaps(
            [r for r in results if r.entity_type in self.strip_entities]
        )

    def strip(self, text: str, pmap: PseudonymMap) -> tuple[str, list]:
        """Replace detected PII with consistent placeholders.

        Returns (stripped_text, applied_detections).
        """
        spans = self.plan(text)
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
        return out, spans


def _resolve_overlaps(results):
    """Keep the higher-scored (then longer) span among overlapping ones.
    Used by analyze() for a readable debug view; strip() merges instead."""
    kept = []
    for r in sorted(results, key=lambda r: (-r.score, r.start - r.end)):
        if all(r.end <= k.start or r.start >= k.end for k in kept):
            kept.append(r)
    return sorted(kept, key=lambda r: r.start)


def _merge_overlaps(results):
    """Union overlapping spans into one; the merged span takes the
    highest-scored member's entity type. Recall-first: everything any span
    covered gets replaced."""
    merged = []
    for r in sorted(results, key=lambda r: (r.start, -r.end)):
        last = merged[-1] if merged else None
        if last is not None and r.start < last.end:
            last.end = max(last.end, r.end)
            if r.score > last.score:
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

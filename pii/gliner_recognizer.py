"""GLiNER zero-shot NER as a Presidio recognizer (detection layer 2).

Catches what patterns can't: names, addresses, dates of birth, and the
person-vs-organization distinction needed for bank transaction descriptions
(strip person names, keep merchants).

The model (~600 MB) is fetched from HF hub on first use into models/hf-cache.
GLiNER's effective window is short, so long inputs are analyzed in
overlapping character windows and results are de-duplicated.
"""

import re

from presidio_analyzer import EntityRecognizer, RecognizerResult

DEFAULT_MODEL = "urchade/gliner_multi_pii-v1"
CACHE_DIR = "models/hf-cache"

# GLiNER label -> Presidio entity type
LABELS = {
    "person": "PERSON",
    "organization": "ORGANIZATION",
    "address": "ADDRESS",
    "email": "EMAIL_ADDRESS",
    "phone number": "PHONE_NUMBER",
    "date of birth": "DATE_OF_BIRTH",
    "bank account number": "AU_BANK_ACCOUNT",
    "tax file number": "AU_TFN",
    "medicare number": "AU_MEDICARE",
    "driver licence number": "AU_DRIVERS_LICENCE",
    "passport number": "PASSPORT",
}

WINDOW_CHARS = 1500
OVERLAP_CHARS = 200

_ALLCAPS_TOKEN = re.compile(r"\b[A-Z]{2,}\b")


def _decap(text: str) -> str:
    """Title-case all-caps tokens (length-preserving). GLiNER's recall drops
    hard on ALL-CAPS bank-statement lines ('TRANSFER TO J SMITH ACC 12345678'
    misses entities the title-cased form finds), so windows are analyzed both
    raw and de-capitalized."""
    return _ALLCAPS_TOKEN.sub(lambda m: m.group(0).capitalize(), text)


class GlinerRecognizer(EntityRecognizer):
    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        threshold: float = 0.5,
        **kwargs,
    ):
        self.model_name = model_name
        self.threshold = threshold
        self._model = None
        super().__init__(
            supported_entities=sorted(set(LABELS.values())),
            name="GlinerRecognizer",
            **kwargs,
        )

    def load(self) -> None:
        pass  # lazy-loaded on first analyze; import cost is high

    def _ensure_model(self):
        if self._model is None:
            import torch
            from gliner import GLiNER

            self._model = GLiNER.from_pretrained(
                self.model_name, cache_dir=CACHE_DIR
            )
            if torch.cuda.is_available():
                self._model = self._model.to("cuda")
        return self._model

    def analyze(self, text, entities, nlp_artifacts=None):
        model = self._ensure_model()
        wanted = set(entities) if entities else None

        # Prediction units, unioned (recall-first):
        # - overlapping document windows: cross-line entities (multi-line
        #   addresses) and prose;
        # - individual lines: GLiNER reliably finds entities in short
        #   statement/transaction lines that it misses inside a full
        #   document ('TRANSFER TO J SMITH ...' scores 1.00 alone, nothing
        #   in context);
        # - a de-capitalized variant of each: ALL-CAPS text tanks recall.
        units: list[tuple[int, str]] = []
        for start in range(0, max(len(text), 1), WINDOW_CHARS - OVERLAP_CHARS):
            window = text[start : start + WINDOW_CHARS]
            if window.strip():
                units.append((start, window))
            if start + WINDOW_CHARS >= len(text):
                break
        offset = 0
        for line in text.splitlines(keepends=True):
            stripped = line.rstrip("\r\n")
            if len(stripped) < len(text) and stripped.strip():
                units.append((offset, stripped))
            offset += len(line)
        for unit_offset, unit_text in list(units):
            decapped = _decap(unit_text)
            if decapped != unit_text:
                units.append((unit_offset, decapped))

        results = []
        seen = set()
        predictions = model.batch_predict_entities(
            [u[1] for u in units], list(LABELS), threshold=self.threshold
        )
        for (unit_offset, _), ents in zip(units, predictions):
            for ent in ents:
                entity_type = LABELS.get(ent["label"])
                if entity_type is None:
                    continue
                if wanted is not None and entity_type not in wanted:
                    continue
                span = (
                    unit_offset + ent["start"],
                    unit_offset + ent["end"],
                    entity_type,
                )
                if span in seen:
                    continue
                seen.add(span)
                results.append(
                    RecognizerResult(
                        entity_type=entity_type,
                        start=span[0],
                        end=span[1],
                        score=ent["score"],
                    )
                )
        return results

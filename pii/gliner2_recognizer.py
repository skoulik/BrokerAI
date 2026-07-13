"""GLiNER2 zero-shot NER as a Presidio recognizer (detection layer 2),
using Fastino's PII-tuned GLiNER2 model. Replaced the original GLiNER (v1)
backend, removed 2026-07-13 (in git history).

Tuned from probing the same failure modes the GLiNER v1 recognizer worked
around, with different results:
- no ALL-CAPS recall penalty ('TRANSFER TO J SMITH ACC 12345678' scores the
  same as its title-cased form), so no de-capitalized variants;
- no context blindness (that line is still found buried mid-document), so
  no per-line prediction units;
- but no input truncation either: the mdeberta encoder attends over the
  whole text, and quadratic attention makes long inputs explode (20k chars
  = 15 s, 60k chars = CUDA OOM). Overlapping windows are therefore still
  required — for memory/speed, not recall — and can be wider than GLiNER's;
- formatted results carry ONE span per unique entity text even when the
  model detected several mentions, deduplicated case-insensitively
  ('Eric Smith' shadows 'ERIC SMITH'; format_results=False shows the
  duplicates, but has no spans at all), so every occurrence of a detected
  entity's text is located by case-insensitive search and given its own
  span;
- person spans often exclude an honorific ('Ms Eric Moore' -> 'Eric
  Moore'), leaving the title behind as a partial leak, so PERSON spans are
  extended left over an immediately preceding honorific;
- labels compete inside a schema (the count-based decoder finds 'New
  Kaylamouth NSW 2926' with an address-only schema but loses it when the
  other 12 labels are present, and even the three address flavors suppress
  each other when combined: 'Flat 66 7 Maddox Alleyway' scores 1.0 under
  'street address' alone, 0.49 next to its siblings) — so addresses get
  two extra single-purpose schema passes: a generic AU address label, and
  a street-line/locality-line split. AU address confidences run lower than
  the model's other labels, so these passes use a lower operating
  threshold and their scores are floored to the main threshold to survive
  the pipeline's global cutoff (same spirit as Presidio's fixed pattern
  scores). Adjacent ADDRESS spans separated only by comma/whitespace are
  coalesced (a two-span 'street, suburb' detection must not leak the
  comma).

GLiNER2 schemas attach a description to each label; descriptions carry the
AU-specific definitions instead of overloading the label string.
"""

import contextlib
import io
import os
import re

from presidio_analyzer import EntityRecognizer, RecognizerResult

DEFAULT_MODEL = "fastino/gliner2-privacy-filter-PII-multi"
CACHE_DIR = "models/hf-cache"

# GLiNER2 label -> (description, Presidio entity type). Same target entity
# set as the removed GLiNER v1 backend used, so eval scores stay comparable.
LABELS = {
    "person": (
        "Full or partial name of a person",
        "PERSON",
    ),
    "organization": (
        "Name of a company, bank, merchant or other organization",
        "ORGANIZATION",
    ),
    "address": (
        "Street or postal address, full or partial",
        "ADDRESS",
    ),
    "email": (
        "Email address",
        "EMAIL_ADDRESS",
    ),
    "phone number": (
        "Phone number in any format",
        "PHONE_NUMBER",
    ),
    "date of birth": (
        "A person's date of birth",
        "DATE_OF_BIRTH",
    ),
    "bank account number": (
        "Bank account number, 5-10 digits in Australian format, sometimes "
        "preceded by a 6-digit BSB code",
        "AU_BANK_ACCOUNT",
    ),
    "tax file number": (
        "Australian tax file number (TFN), 8 or 9 digits",
        "AU_TFN",
    ),
    "medicare number": (
        "Australian Medicare card number, 10 or 11 digits",
        "AU_MEDICARE",
    ),
    "driver licence number": (
        "Driver licence number",
        "AU_DRIVERS_LICENCE",
    ),
    "passport number": (
        "Passport number",
        "PASSPORT",
    ),
}

# Extra address-only schema passes, shielded from label competition —
# kept apart from each other too (see module docstring).
ADDRESS_LABELS_GENERIC = {
    "address": (
        "Residential or postal address or any part of one, e.g. "
        "'5 Jeremy Avenue, Richardland QLD 5537' or a suburb-state-postcode "
        "line like 'NEWTOWN NSW 2042'",
        "ADDRESS",
    ),
}
ADDRESS_LABELS_SPLIT = {
    "street address": (
        "Street line of an address, including unit, flat or suite numbers, "
        "e.g. 'Flat 66 7 Maddox Alleyway' or 'Suite 8 12 George Street'",
        "ADDRESS",
    ),
    "suburb state postcode": (
        "Locality line of an Australian address: suburb, state abbreviation "
        "and 4-digit postcode, e.g. 'NEWTOWN NSW 2042'",
        "ADDRESS",
    ),
}
ADDRESS_THRESHOLD = 0.3

WINDOW_CHARS = 3000
OVERLAP_CHARS = 300
BATCH_SIZE = 4

_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Prof)\.?\s+$", re.IGNORECASE)
_ADDRESS_GAP = re.compile(r"^,?\s*$")
ADDRESS_GAP_MAX = 4


class Gliner2Recognizer(EntityRecognizer):
    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        threshold: float = 0.4,
        **kwargs,
    ):
        self.model_name = model_name
        self.threshold = threshold
        self._model = None
        super().__init__(
            supported_entities=sorted({e for _, e in LABELS.values()}),
            name="Gliner2Recognizer",
            **kwargs,
        )

    def load(self) -> None:
        pass  # lazy-loaded on first analyze; import cost is high

    def _ensure_model(self):
        if self._model is None:
            os.environ.setdefault("HF_HUB_CACHE", os.path.abspath(CACHE_DIR))
            import torch
            from gliner2 import GLiNER2

            # The constructor prints an emoji banner, which raises
            # UnicodeEncodeError on cp1251 Windows consoles.
            with contextlib.redirect_stdout(io.StringIO()):
                self._model = GLiNER2.from_pretrained(
                    self.model_name,
                    map_location="cuda" if torch.cuda.is_available() else None,
                )
        return self._model

    def analyze(self, text, entities, nlp_artifacts=None):
        model = self._ensure_model()
        wanted = set(entities) if entities else None

        windows: list[tuple[int, str]] = []
        for start in range(0, max(len(text), 1), WINDOW_CHARS - OVERLAP_CHARS):
            window = text[start : start + WINDOW_CHARS]
            if window.strip():
                windows.append((start, window))
            if start + WINDOW_CHARS >= len(text):
                break

        passes = (
            (LABELS, self.threshold),
            (ADDRESS_LABELS_GENERIC, ADDRESS_THRESHOLD),
            (ADDRESS_LABELS_SPLIT, ADDRESS_THRESHOLD),
        )
        results = []
        seen = set()
        for labels, threshold in passes:
            predictions = model.batch_extract_entities(
                [w[1] for w in windows],
                {label: desc for label, (desc, _) in labels.items()},
                batch_size=BATCH_SIZE,
                threshold=threshold,
                include_confidence=True,
                include_spans=True,
            )
            for (window_offset, window_text), prediction in zip(
                windows, predictions
            ):
                for label, ents in prediction["entities"].items():
                    entity_type = labels[label][1]
                    if wanted is not None and entity_type not in wanted:
                        continue
                    for ent in ents:
                        for start, end in _occurrences(window_text, ent["text"]):
                            if entity_type == "PERSON":
                                m = _HONORIFIC.search(window_text, 0, start)
                                if m:
                                    start = m.start()
                            span = (
                                window_offset + start,
                                window_offset + end,
                                entity_type,
                            )
                            if span in seen:
                                continue
                            seen.add(span)
                            score = ent["confidence"]
                            if entity_type == "ADDRESS":
                                score = max(score, self.threshold)
                            results.append(
                                RecognizerResult(
                                    entity_type=entity_type,
                                    start=span[0],
                                    end=span[1],
                                    score=score,
                                )
                            )
        return _coalesce_addresses(results, text)


def _coalesce_addresses(results, text):
    """Join ADDRESS spans separated only by comma/whitespace into one span
    (highest member score)."""
    addresses = sorted(
        (r for r in results if r.entity_type == "ADDRESS"),
        key=lambda r: (r.start, r.end),
    )
    out = [r for r in results if r.entity_type != "ADDRESS"]
    for r in addresses:
        last = out[-1] if out and out[-1].entity_type == "ADDRESS" else None
        if (
            last is not None
            and r.start - last.end <= ADDRESS_GAP_MAX
            and _ADDRESS_GAP.match(text[max(last.end, 0) : max(r.start, 0)])
        ):
            last.end = max(last.end, r.end)
            last.score = max(last.score, r.score)
        else:
            out.append(r)
    return out


def _occurrences(text: str, needle: str):
    if not needle:
        return
    for m in re.finditer(re.escape(needle), text, re.IGNORECASE):
        yield m.start(), m.end()

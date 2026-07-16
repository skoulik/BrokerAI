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
- global attention has a second cost (2026-07-15 diagnosis, records in
  DONE.md): when the SAME person appears in one window under two word
  orders ('PAYID ... JOSEPH SCHAEFER' and 'OSKO ... SCHAEFER JOSEPH'), the
  canonical mention keeps its score and the reversed one collapses to
  sub-threshold fragments — reversed order alone is learned fine (0.94 in
  a junk blob without the canonical mention). Therefore text containing
  RECORD_SEPARATOR (U+241E — csv_mode's cell sentinel) is windowed per
  segment: cells are independent units whose spans get clamped per cell
  anyway, so cross-cell context is pure noise and isolating it removes
  the interference by construction;
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
  scores). Adjacent same-type spans separated only by comma/whitespace
  are coalesced for ADDRESS (a two-span 'street, suburb' detection must
  not leak the comma) and PERSON (2026-07-15: isolated statement lines
  emit reversed names as adjacent fragments — 'SCHAEFER' + 'JOSEPH RENT'
  — whose union misses only the joining space). Coalescing two genuinely
  distinct adjacent names into one span costs a pseudonym-consistency
  wart, never a leak.

GLiNER2 schemas attach a description to each label; descriptions carry the
AU-specific definitions instead of overloading the label string.

The model ships with max_width=8: spans are enumerated over 1..8 words, so
longer entities can never be emitted — the root cause of one-line AU
addresses ('Flat 66 7 Maddox Alleyway, New Kaylamouth NSW 2926', 9 words;
the tokenizer counts the comma as a word, so 10) coming out as fragments.
max_width is an enumeration parameter, not baked into weights (SpanMarkerV0
scores a span from its start/end tokens only), so it is lifted at inference
by overriding BOTH model.max_width and the span_rep layer's copy (used in a
.view(); overriding only the model attribute shape-errors). Experiment
2026-07-14 on the tier-1 corpus: the scorer generalizes past its training
width — full one-line addresses score 0.99 as single spans (fragments
0.29), NMS keeps the whole span, no precision change at width 10-12, +1.5%
layer-2 latency at 12; width 16 showed the first extra ORGANIZATION
over-strip, so stay below it.

Location pass (always on; the ablation flag was retired 2026-07-15): a
dedicated single-label LOCATION schema pass for bare place names in prose
("a teacher in Cairns") — contextual identifiers that are not addresses.
This is the production contextual-identifier net; it replaced the retired
SpacyRecognizer LOCATION detector (2026-07-15) rather than standing in for a
surviving spaCy role. Isolated from the main labels for the same
label-competition reason as the address passes. Precision guards live on the
pass: an exclusionary description and a LOCATION_MIN_CHARS floor (its false
positives were all short codes/acronyms — 'AU', 'NSW', 'NAB'). Chosen
head-to-head over spaCy LOCATION (11/11 vs 6/11 contextual towns): DONE.md,
2026-07-14.

Always-on digit floor on bank-account guesses (AU_BANK_ACCOUNT_MIN_DIGITS):
the model occasionally labels a stray digit fragment ('42') as a bank
account; a real AU account is 5-10 digits, so emissions carrying fewer than
5 digits in total are dropped. Counted on digits, not characters — the model
emits space-grouped accounts ('0007 3111 4') as ONE span, and internal
separators must not push a real account under the floor.
"""

import contextlib
import io
import os
import re

from presidio_analyzer import EntityRecognizer, RecognizerResult

from pii.core.constants import RECORD_SEPARATOR

DEFAULT_MODEL = "fastino/gliner2-privacy-filter-PII-multi"
CACHE_DIR = "models/hf-cache"
# Widest span (in words) the model may emit; see module docstring.
DEFAULT_MAX_WIDTH = 12

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

# Location pass (always on). Its own single-label schema — kept apart from the
# main labels so it neither suppresses PERSON/ORG nor is suppressed by them (label
# competition, see docstring). Purpose: cover bare place names in prose
# ("a teacher in Cairns") that are contextual identifiers but not addresses —
# the production contextual-identifier net that replaced the retired
# SpacyRecognizer LOCATION detector (2026-07-15). Chosen head-to-head over
# spaCy in the 2026-07-14 location-label experiment.
LOCATION_LABELS = {
    "location": (
        "A geographic place name on its own: a city, town, suburb or "
        "locality, e.g. 'Cairns', 'Wagga Wagga', 'Newtown' — NOT a full "
        "street address, NOT a state or country abbreviation, and NOT a "
        "company, bank, shop, brand or merchant name",
        "LOCATION",
    ),
}
LOCATION_THRESHOLD = 0.4
# Location false positives in the 2026-07-14 experiment were dominated by
# short ALL-CAPS tokens: the card-transaction country suffix 'AU', state
# codes ('NSW'/'VIC'/...) and bank/merchant acronyms ('NAB'). None is a real
# place name, and every AU place name in the corpus is >=4 chars, so a
# minimum-length floor removes the whole class at once (an explicit
# {AU, NSW, ...} stop-list was evaluated first but never shipped — every
# member is <=3 chars, so the floor subsumes it). Trade-off:
# the handful of genuine 3-letter suburbs (Kew, Ayr) are sacrificed — an
# acceptable loss for a contextual-identifier safety net; the planned AU
# place-name gazetteer (TODO.md) is the recovery path for those.
LOCATION_MIN_CHARS = 4

# Always-on floor on GLiNER2's AU_BANK_ACCOUNT *guesses* (a fragment like
# '42' otherwise strips two stray digits). Australian bare account numbers
# are 5-10 digits — matching the layer-1 AuAccountNumberRecognizer pattern
# (\d{5,10}) — so a real account can never fall below this; counted on digits
# only, robust to BSB prefixes and separators. Same spirit as LOCATION_MIN_
# CHARS but the meaningful unit here is digits, not characters (2026-07-14).
AU_BANK_ACCOUNT_MIN_DIGITS = 5

WINDOW_CHARS = 3000
OVERLAP_CHARS = 300
BATCH_SIZE = 4

_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Prof)\.?\s+$", re.IGNORECASE)
_COALESCE_GAP = re.compile(r"^,?\s*$")
COALESCE_GAP_MAX = 4


class Gliner2Recognizer(EntityRecognizer):
    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        threshold: float = 0.4,
        max_width: int = DEFAULT_MAX_WIDTH,
        **kwargs,
    ):
        self.model_name = model_name
        self.threshold = threshold
        self.max_width = max_width
        self._model = None
        entity_types = {e for _, e in LABELS.values()}
        entity_types |= {e for _, e in LOCATION_LABELS.values()}
        super().__init__(
            supported_entities=sorted(entity_types),
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
            self._model.max_width = self.max_width
            self._model.span_rep.span_rep_layer.max_width = self.max_width
        return self._model

    def analyze(self, text, entities, nlp_artifacts=None):
        model = self._ensure_model()
        wanted = set(entities) if entities else None

        # Segments split at RECORD_SEPARATOR never share a window (cell
        # isolation — see module docstring); ordinary text is one segment
        # and gets the plain overlapping-window treatment.
        windows: list[tuple[int, str]] = []
        offset = 0
        for segment in text.split(RECORD_SEPARATOR):
            for start in range(
                0, max(len(segment), 1), WINDOW_CHARS - OVERLAP_CHARS
            ):
                window = segment[start : start + WINDOW_CHARS]
                if window.strip():
                    windows.append((offset + start, window))
                if start + WINDOW_CHARS >= len(segment):
                    break
            offset += len(segment) + len(RECORD_SEPARATOR)

        passes = [
            (LABELS, self.threshold),
            (ADDRESS_LABELS_GENERIC, ADDRESS_THRESHOLD),
            (ADDRESS_LABELS_SPLIT, ADDRESS_THRESHOLD),
            (LOCATION_LABELS, LOCATION_THRESHOLD),
        ]
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
                        if (
                            entity_type == "LOCATION"
                            and len(ent["text"].strip()) < LOCATION_MIN_CHARS
                        ):
                            continue
                        if (
                            entity_type == "AU_BANK_ACCOUNT"
                            and sum(c.isdigit() for c in ent["text"])
                            < AU_BANK_ACCOUNT_MIN_DIGITS
                        ):
                            continue
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
        return _coalesce_adjacent(results, text)


# Entity types whose adjacent same-type spans merge (module docstring):
# fragmented multi-part addresses and fragmented person names.
COALESCE_TYPES = ("ADDRESS", "PERSON")


def _coalesce_adjacent(results, text):
    """Join same-type spans separated only by comma/whitespace into one
    span (highest member score), for the types in COALESCE_TYPES."""
    out = [r for r in results if r.entity_type not in COALESCE_TYPES]
    for etype in COALESCE_TYPES:
        merged: list = []
        for r in sorted(
            (r for r in results if r.entity_type == etype),
            key=lambda r: (r.start, r.end),
        ):
            last = merged[-1] if merged else None
            if (
                last is not None
                and r.start - last.end <= COALESCE_GAP_MAX
                and _COALESCE_GAP.match(
                    text[max(last.end, 0) : max(r.start, 0)]
                )
            ):
                last.end = max(last.end, r.end)
                last.score = max(last.score, r.score)
            else:
                merged.append(r)
        out.extend(merged)
    return out


def _occurrences(text: str, needle: str):
    if not needle:
        return
    for m in re.finditer(re.escape(needle), text, re.IGNORECASE):
        yield m.start(), m.end()

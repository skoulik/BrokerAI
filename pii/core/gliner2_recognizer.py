"""GLiNER2 zero-shot NER as a Presidio recognizer (detection layer 2),
using Fastino's PII-tuned GLiNER2 model. The design rationale and history —
the GLiNER v1 -> v2 switch, the spaCy/LOCATION decisions, the max_width
experiment and eval numbers — live in pii/core/ARCHITECTURE.md and DONE.md.
This docstring is the operational contract for editing NER behaviour.

Behaviour, and the reason the code is shaped this way:
- No ALL-CAPS recall penalty and no context blindness ('TRANSFER TO J SMITH
  ACC 12345678' scores the same title-cased, and is found buried
  mid-document), so no de-capitalized variants and no per-line prediction.
- Long inputs are still windowed, for MEMORY not recall: the mdeberta
  encoder attends over the whole text and quadratic attention makes long
  inputs explode (~20k chars = 15 s, ~60k = CUDA OOM). Windows can be wide.
- Text containing RECORD_SEPARATOR (U+241E — csv_mode's cell sentinel) is
  windowed PER SEGMENT. Global attention lets the same person under two
  word orders in one window interfere: the canonical mention keeps its
  score and the reversed one collapses to sub-threshold fragments (reversed
  order alone is learned fine). Cells are independent and their spans are
  clamped per cell anyway, so isolating them removes the interference by
  construction.
- Formatted results carry ONE span per unique entity text (deduplicated
  case-insensitively; 'Eric Smith' would otherwise shadow 'ERIC SMITH'), so
  every occurrence is then located by case-insensitive search and given its
  own span.
- PERSON spans are extended left over an immediately preceding honorific
  ('Ms Eric Moore' would otherwise leave 'Ms' behind as a partial leak).
- Labels compete inside a schema (count-based decoder): the same address
  scores ~1.0 under an address-only schema but ~0.5 among the other labels.
  So ADDRESS gets two extra single-purpose passes (a generic AU-address
  label, and a street-line/locality-line split) at a lower operating
  threshold, with scores floored to the main threshold to survive the
  pipeline cutoff (same spirit as Presidio's fixed pattern scores).
- Adjacent same-type spans separated only by comma/whitespace are coalesced
  for ADDRESS and PERSON (a two-span 'street, suburb' must not leak the
  comma; isolated statement lines emit reversed names as adjacent fragments
  'SCHAEFER' + 'JOSEPH RENT'). Merging two genuinely distinct adjacent names
  costs a pseudonym-consistency wart, never a leak. The shared-surname joint
  form ('Julie and Brian Summers') is coalesced across the ' and '/' & '
  connector as an expansion of the model's own PERSON detections —
  name-signal-gated by construction (prose 'X and Y Z' yields no PERSON). The
  left fragment must be a single token so two DISTINCT people ('Julie Summers
  and Brian Reid') stay separate. (JointNameRecognizer owns only the initials
  form GLiNER2 can't segment.)

GLiNER2 schemas attach a description to each label; descriptions carry the
AU-specific definitions instead of overloading the label string.

max_width (DEFAULT_MAX_WIDTH): the model ships with max_width=8, so spans
over 8 words can never be emitted — the root cause of one-line AU addresses
('Flat 66 7 Maddox Alleyway, New Kaylamouth NSW 2926', 9 words; the
tokenizer counts the comma as a word, so 10) fragmenting. It is an
enumeration parameter, not baked into weights (SpanMarkerV0 scores a span
from its start/end tokens only), so it is lifted at inference by overriding
BOTH model.max_width AND the span_rep layer's copy (used in a .view();
overriding only the model attribute shape-errors). Stay <= ~12 — width 16
starts showing wide-span false positives.

No standalone place-name detection: bare city/town names pass verbatim (the
ADDRESS passes still own full addresses and suburb-state-postcode lines).
See ARCHITECTURE.md for the decision.

Identifier post-validation (IDENTIFIER_VALIDATORS): layer-1's identifier
recognizers are checksum/format-validated, but the model's numeric-ID
guesses bypassed all validation — on real statements it labels bank receipt
references (a letter + 10 digits, 'W1045366576') semi-randomly as TFN,
driver licence or passport. Each numeric-ID guess now passes its class rule
before it may strip:
- AU_TFN: 9 digits + ATO mod-11 (pii.core.checksums). Legacy 8-digit TFNs
  pass structurally without arithmetic — no reliable public checksum
  variant, and layer-1's 9-digit pattern can't cover them, so demoting a
  real one would leak it (an 8-digit FP merely over-strips);
- AU_MEDICARE: 10-11 digits, first digit 2-6, mod-10 over the card number;
- AU_BANK_ACCOUNT: 5-16 digits (5-10 for the account, up to 6 more for a
  BSB prefix the model includes in one span). Counted on digits, not
  characters — space-grouped accounts ('0007 3111 4') come out as ONE span
  and internal separators must not push a real account under the floor. The
  cap also kills junk runs long enough for _merge_overlaps to drape a
  neighbouring CREDIT_CARD over;
- PASSPORT / AU_DRIVERS_LICENCE carry no public checksum, so the guards are
  structural: passports at most 9 digits (AU format is 1-2 letters +
  7 digits), licences at most 10 alphanumeric characters (no AU state
  issues longer ones — this also drops 'Australian credit licence NNNNNN'
  phrases mislabeled as licences).
A guess whose SHAPE is right but whose checksum fails is demoted to the
matching *_INVALID class and joins the shadow-recognizer findings (see
pii.core.invalid_recognizers) instead of silently vanishing — unless the
pipeline runs the 'ignore' tier (demote_invalid=False). Structurally
impossible guesses are plain-dropped. Masked last-4 disclosures ('card
ending 1234') fall under the digit floors by design, consistent with
layer-1: a last-4 fragment alone is not strip-worthy.
"""

import contextlib
import io
import os
import re

from presidio_analyzer import EntityRecognizer, RecognizerResult

from pii.core.checksums import digits, medicare_checksum, tfn_checksum
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

# No standalone place-name label: bare city/town names pass verbatim; the
# ADDRESS passes above own full addresses and suburb-state-postcode lines.
# See ARCHITECTURE.md for the decision.

# Digit bounds on GLiNER2's AU_BANK_ACCOUNT *guesses*: a fragment like '42'
# otherwise strips two stray digits, and an unbounded junk run ('...22
# digits...') otherwise claims a span that _merge_overlaps drapes a
# neighbouring CREDIT_CARD over.
# Australian bare account numbers are 5-10 digits — matching the layer-1
# AuAccountNumberRecognizer pattern (\d{5,10}) — and the model sometimes
# includes a 6-digit BSB prefix in the same span, hence the 16 cap. Counted
# on digits only, robust to separators.
AU_BANK_ACCOUNT_MIN_DIGITS = 5
AU_BANK_ACCOUNT_MAX_DIGITS = 16
# Structural caps for the two identifier classes without a public checksum
# (see module docstring): AU passports are 1-2 letters + 7 digits; no AU
# state issues a driver licence longer than 10 alphanumeric characters.
PASSPORT_MAX_DIGITS = 9
DRIVERS_LICENCE_MAX_ALNUM = 10


def _validate_tfn(text: str) -> str | None:
    d = digits(text)
    if len(d) == 8:  # legacy TFN: structural pass, no public checksum variant
        return "AU_TFN"
    if len(d) == 9:
        return "AU_TFN" if tfn_checksum(d) else "AU_TFN_INVALID"
    return None


def _validate_medicare(text: str) -> str | None:
    d = digits(text)
    if len(d) not in (10, 11) or d[0] not in "23456":
        return None
    return (
        "AU_MEDICARE" if medicare_checksum(d[:10]) else "AU_MEDICARE_INVALID"
    )


def _validate_account(text: str) -> str | None:
    n = sum(c.isdigit() for c in text)
    if AU_BANK_ACCOUNT_MIN_DIGITS <= n <= AU_BANK_ACCOUNT_MAX_DIGITS:
        return "AU_BANK_ACCOUNT"
    return None


def _validate_passport(text: str) -> str | None:
    if sum(c.isdigit() for c in text) <= PASSPORT_MAX_DIGITS:
        return "PASSPORT"
    return None


def _validate_licence(text: str) -> str | None:
    if sum(c.isalnum() for c in text) <= DRIVERS_LICENCE_MAX_ALNUM:
        return "AU_DRIVERS_LICENCE"
    return None


# Post-validation of the model's numeric-identifier guesses (module
# docstring; issue #10). Each validator returns the entity type to emit
# under — the class itself, a demotion to its *_INVALID shadow class
# (checksum-failed but shape-correct), or None to drop the guess.
IDENTIFIER_VALIDATORS = {
    "AU_TFN": _validate_tfn,
    "AU_MEDICARE": _validate_medicare,
    "AU_BANK_ACCOUNT": _validate_account,
    "PASSPORT": _validate_passport,
    "AU_DRIVERS_LICENCE": _validate_licence,
}

WINDOW_CHARS = 3000
OVERLAP_CHARS = 300
BATCH_SIZE = 4

_HONORIFIC = re.compile(r"\b(?:Mr|Mrs|Ms|Miss|Dr|Prof)\.?\s+$", re.IGNORECASE)
# Corporate-licence context guard on driver-licence guesses (issue #8c /
# review other-finding #1, 2026-07-22): a 5-6 digit run right after an
# AFSL / Australian Credit Licence label in a bank footer is a PUBLIC
# corporate identifier — layer-1's kept AU_AFSL/AU_CREDIT_LICENCE classes
# own it — but the model labels the bare number 'driver licence number'.
# Anchored at the guess's start ($ + endpos), so a genuine 'Licence no:
# NNNNNN' driver form (no afsl/credit/financial-services word) never
# matches.
_CORPORATE_LICENCE = re.compile(
    r"(?:afsl|acl|(?:australian\s+)?(?:credit|financial\s+services)\s+"
    r"licen[cs]e)\s*(?:no\.?|number|#)?\s*:?\s*$",
    re.IGNORECASE,
)
_COALESCE_GAP = re.compile(r"^,?\s*$")
COALESCE_GAP_MAX = 4
# Joint-name connector between two PERSON fragments ('Julie and Brian
# Summers' -> 'Julie' + 'Brian Summers'; 'E & J Moore'). Merging across it
# is how the full-name joint form is handled — as a span expansion of
# GLiNER2's own detections, gated by the model actually detecting a person
# on each side (so prose 'X and Y Z' can't trigger it). See _mergeable.
_JOINT_GAP = re.compile(r"^\s+(?:and|&)\s+$", re.IGNORECASE)


class Gliner2Recognizer(EntityRecognizer):
    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        threshold: float = 0.4,
        max_width: int = DEFAULT_MAX_WIDTH,
        demote_invalid: bool = True,
        **kwargs,
    ):
        self.model_name = model_name
        self.threshold = threshold
        self.max_width = max_width
        # Whether a shape-correct, checksum-failed identifier guess demotes
        # to its *_INVALID class (joining the shadow-recognizer findings) or
        # is dropped outright — False under the 'ignore' tier (module
        # docstring; wired from PiiPipeline's invalid_identifiers setting).
        self.demote_invalid = demote_invalid
        self._model = None
        entity_types = {e for _, e in LABELS.values()}
        entity_types |= {"AU_TFN_INVALID", "AU_MEDICARE_INVALID"}
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
                    if (
                        wanted is not None
                        and entity_type not in wanted
                        # a validated type may demote to a different
                        # (invalid) type — filter after validation instead
                        and entity_type not in IDENTIFIER_VALIDATORS
                    ):
                        continue
                    for ent in ents:
                        emit_type = entity_type
                        validator = IDENTIFIER_VALIDATORS.get(entity_type)
                        if validator is not None:
                            emit_type = validator(ent["text"])
                            if emit_type is None or (
                                emit_type != entity_type
                                and not self.demote_invalid
                            ):
                                continue
                        if wanted is not None and emit_type not in wanted:
                            continue
                        for surface in _search_forms(emit_type, ent["text"]):
                            for start, end in _occurrences(window_text, surface):
                                if emit_type == "PERSON":
                                    m = _HONORIFIC.search(window_text, 0, start)
                                    if m:
                                        start = m.start()
                                if (
                                    emit_type == "AU_DRIVERS_LICENCE"
                                    and _CORPORATE_LICENCE.search(
                                        window_text, max(0, start - 60), start
                                    )
                                ):
                                    continue
                                span = (
                                    window_offset + start,
                                    window_offset + end,
                                    emit_type,
                                )
                                if span in seen:
                                    continue
                                seen.add(span)
                                score = ent["confidence"]
                                if emit_type == "ADDRESS":
                                    score = max(score, self.threshold)
                                results.append(
                                    RecognizerResult(
                                        entity_type=emit_type,
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
            if last is not None and _mergeable(last, r, text, etype):
                last.end = max(last.end, r.end)
                last.score = max(last.score, r.score)
            else:
                merged.append(r)
        out.extend(merged)
    return out


def _mergeable(last, r, text, etype: str) -> bool:
    """Whether span `r` coalesces into the preceding same-type span `last`.

    Two cases (text between them is the gap):
    - comma/whitespace gap — fragmented multi-part addresses and names;
    - PERSON only: a joint connector (' and ' / ' & ') where `last` is a
      single token — a couple sharing a surname ('Julie' + ' and ' + 'Brian
      Summers'). Restricting `last` to one token keeps two DISTINCT people
      ('Julie Summers' + ' and ' + 'Brian Reid') as separate placeholders.
      This is the full-name joint form, handled as an expansion of the
      model's own PERSON detections rather than a lexical pattern (issue #4)."""
    gap = text[max(last.end, 0) : max(r.start, 0)]
    if r.start - last.end <= COALESCE_GAP_MAX and _COALESCE_GAP.match(gap):
        return True
    if (
        etype == "PERSON"
        and _JOINT_GAP.match(gap)
        and len(text[last.start : last.end].split()) == 1
    ):
        return True
    return False


def _search_forms(entity_type: str, text: str) -> list[str]:
    """Surface strings to locate for a detected entity: the detected text,
    plus — for a two-token PERSON — its reversed (surname-first) order.

    GLiNER2's global attention collapses a reversed mention when the
    canonical order also sits in the window (module docstring): it emits the
    canonical name at full score and the reversed one as a sub-threshold
    fragment (surname only), so the given name leaks. Re-finding the reversed
    order from the CONFIDENT canonical detection recovers it — 'OLGA KULIK'
    detected also marks 'KULIK OLGA' wherever it appears, at the canonical's
    score. Two tokens only: reversing 3+ tokens (particle surnames, middle
    names) is ambiguous and false-positive-prone, and a reversed bigram
    matching non-name text needs both tokens adjacent in reverse — rare."""
    forms = [text]
    if entity_type == "PERSON":
        tokens = text.split()
        if len(tokens) == 2:
            forms.append(f"{tokens[1]} {tokens[0]}")
    return forms


def _occurrences(text: str, needle: str):
    if not needle:
        return
    for m in re.finditer(re.escape(needle), text, re.IGNORECASE):
        yield m.start(), m.end()

"""Layer 1 — pattern/checksum detection — and the pseudonymization pipeline.

`PiiPipeline` is layer 1: the pattern/checksum rules in
pii.core.recognizers, run by pii.core.engine. AU_TFN / AU_MEDICARE /
AU_ABN / AU_ACN and payment cards each come from ONE rule that emits the
valid class or its `*_INVALID` shadow from a single checksum call; plus
BSB, account numbers, PayID, joint-account initials, ATF trustee clauses,
email, IBAN and AU-region phones. URL/IP detection is deliberately absent
(not relevant to financial documents), as is anything US-specific.

The SEMANTIC detector is layer 0 and lives outside this module — a local
LLM reading the page image (pii.core.vlm) or the document text
(pii.core.text_llm). It emits COARSE classes on purpose, because it is
measurably unreliable at typing identifiers, and `merge_detections` folds
a layer-1 pass over the same string on top to refine, validate and extend
them. The front-ends (pii.core.text_mode, pii.core.image_mode) own that
composition; a front-end never strips a document without a layer-0
detector.

There is no layer 2. GLiNER2 was retired 2026-08-09 after layer 0 beat it
on every semantic class and seed (ARCHITECTURE "Layer 0"), and Presidio
and spaCy went the same day — the engine is ours (pii.core.engine).

Detected spans are resolved for overlaps and replaced with consistent
placeholders from a PseudonymMap.

Checksum-invalid identifier candidates (a value shaped like a TFN whose
mod-11 arithmetic fails — a typo, bad OCR, or forgery) are collected by
the same rules that detect the valid classes, controlled by the
`invalid_identifiers` tier, and returned by detect()/strip() as
InvalidFinding records. They are only *masked* when mask_invalid=True
adds the invalid classes to strip_entities — note layer 0 strips such a
value anyway under IDENTIFIER_GENERIC, which is a known open item (TODO).
The findings are near-PII (a typo'd TFN is a real TFN minus a digit) —
treat any log of them as a local-only artifact, like the pseudonym map.
"""

from dataclasses import dataclass

from pii.core.detection import Detection
from pii.core.engine import Analyzer
from pii.core.mapping import PseudonymMap
from pii.core.org_policy import is_private_entity
from pii.core.recognizers import (
    INVALID_ENTITY_TYPES,
    INVALID_RULES,
    VALIDATED_RULES,
    build_rules,
)

# Entities replaced by default. Detected-but-kept by default: ORGANIZATION
# (merchant/institution names carry analytical value in bank statements) —
# EXCEPT account-holder private entities (PTY LTD / TRUST / ... not on the
# institution keep-list), stripped per org_policy (see detect()). DATE_TIME
# (transaction dates; DATE_OF_BIRTH is stripped separately) is kept.
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
    "ADDRESS",
    "DATE_OF_BIRTH",
    "IBAN_CODE",
    # Layer-0 (VLM) detections that no deterministic recognizer has claimed.
    # The VLM emits one coarse PII_IDENTIFIER class on purpose — layer 1 is what
    # refines a digit run into TFN/Medicare/ABN/ACN/BSB/account/card — so
    # anything it cannot classify must still strip, under a generic placeholder.
    # Distinct from the *_INVALID classes: those matched a pattern and FAILED
    # its checksum, which is a different signal from matching no pattern at all.
    "IDENTIFIER_GENERIC",
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
        self.analyzer = Analyzer(build_rules(invalid_identifiers))

    def analyze(self, text: str):
        """All detections above threshold, overlap-resolved, sorted by
        position — including entity types that strip() would keep."""
        return _resolve_overlaps(self.analyzer.analyze(text, self.threshold))

    def detect(self, text: str) -> tuple[list, list[InvalidFinding]]:
        """One analyzer pass -> (strip plan, checksum-invalid findings).

        The plan is what strip() replaces: strip-listed entities only,
        overlaps merged. Filter to strip-listed entities BEFORE overlap
        handling: a kept-type span must never shadow an overlapping PII
        span, or the PII leaks. Then MERGE overlapping spans rather than picking a winner —
        a small high-score span (BSB, 0.55) must not evict a wider
        covering span (account number, 0.52) and expose the rest of it.
        """
        results = self.analyzer.analyze(text, self.threshold)
        plan = _merge_overlaps(
            [r for r in results if self._in_strip_plan(r, text)]
        )
        return plan, _collect_invalid(results, text)

    def merge_detections(
        self, detected: list, text: str
    ) -> tuple[list, list[InvalidFinding]]:
        """Fold layer-0 detections and a layer-1 pass over the same text into
        one strip plan.

        `detected` are layer-0 (VLM) spans already located in `text`, so both
        span sets share one offset space — which is only true because
        production geometry is OCR (`--geometry ocr`), and is what makes this
        a merge rather than a reconciliation.

        Layer 1 does three jobs here, and all three fall out of running the
        ordinary analyzer pass and merging the two sets:

        - REFINE. The VLM emits one coarse IDENTIFIER_GENERIC class on
          purpose; an overlapping layer-1 span carries the precise,
          checksum-validated type and OUTRANKS it (see _rank), so the merged
          span strips as AU_TFN_1 / AU_BSB_1 / CREDIT_CARD_1, not ID_1.
        - VALIDATE. The *_INVALID shadows are a signal a VLM structurally
          cannot produce — it can read a TFN but not verify its mod-11
          arithmetic — so they can only come from this pass.
        - UNION. Whatever layer 1 finds that the model missed is added, which
          is a deterministic recall floor under a stochastic detector.

        Layer-0 spans are put through the strip plan first, so the kept-
        ORGANIZATION policy applies to them exactly as it does to layer 1: the
        prompt deliberately carries no institutional carve-outs (an
        unauditable per-page keep decision is the wrong place for one), which
        means the model reports merchant and bank names by design and this is
        where they are kept.

        Where the two disagree on a specific class the higher score wins,
        which is layer 0 (it detects at 1.0) — deliberate, it is the better
        semantic detector. The failure that protects against (layer 1 typing
        the AFSL number 237502 as a phone) costs an over-strip, not a leak.
        """
        layer1, invalid = self.detect(text)
        kept = [r for r in detected if self._in_strip_plan(r, text)]
        return _merge_overlaps(kept + list(layer1)), invalid

    def strips_value(self, entity_type: str, value: str) -> bool:
        """Whether a bare (type, value) pair would be stripped.

        The offset-free form of `_in_strip_plan`, for detections that never
        got a span — layer-0 findings painted from the model's own box
        (locator tier 3). Without it the kept-ORGANIZATION policy would not
        reach them and a boxed merchant logo would be painted over.
        """
        return self._in_strip_plan(
            Detection(
                entity_type=entity_type, start=0, end=len(value), score=1.0
            ),
            value,
        )

    def _in_strip_plan(self, r, text: str) -> bool:
        """Whether a detected span is stripped.

        Straight strip_entities membership, except ORGANIZATION: kept by
        default (merchant/institution analytical value), but the account
        holder's own private entities — a legal-form marker and not a known
        institution (org_policy.is_private_entity) — are identifying PII and
        stripped (keeping the ORG_n placeholder). --strip-orgs (ORGANIZATION
        added to strip_entities) still forces all orgs.
        """
        if (
            r.entity_type == "ORGANIZATION"
            and "ORGANIZATION" not in self.strip_entities
        ):
            return is_private_entity(text[r.start : r.end])
        return r.entity_type in self.strip_entities

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
        return apply_plan(text, spans, pmap), spans, invalid


def apply_plan(text: str, spans, pmap: PseudonymMap) -> str:
    """Replace each planned span with its placeholder.

    Split out of `PiiPipeline.strip` so the layer-0 text path
    (`pii.core.text_mode`) applies a plan exactly the way the layered path
    does — the plan's provenance must not change how it is spliced.

    Placeholders are allocated in document order (readable numbering), then
    spliced right-to-left so earlier offsets stay valid.
    """
    placeholders = [
        pmap.placeholder_for(r.entity_type, text[r.start : r.end])
        for r in spans
    ]
    out = text
    for r, placeholder in sorted(
        zip(spans, placeholders), key=lambda p: p[0].start, reverse=True
    ):
        out = out[: r.start] + placeholder + out[r.end :]
    return out


def _resolve_overlaps(results):
    """Keep the higher-scored (then longer) span among overlapping ones.
    Used by analyze() for a readable debug view; strip() merges instead."""
    kept = []
    for r in sorted(results, key=lambda r: (-r.score, r.start - r.end)):
        if all(r.end <= k.start or r.start >= k.end for k in kept):
            kept.append(r)
    return sorted(kept, key=lambda r: r.start)


def _rank(r) -> tuple:
    """Merge ranking: which member's entity type LABELS a merged span. Three
    tiers, score breaking ties within a tier. Ranking decides the label only —
    every member's extent is unioned regardless, recall-first.

    2. A specific class — layer 1's pattern/checksum classes and layer 0's
       semantic ones alike.
    1. IDENTIFIER_GENERIC: layer 0 saw an identifier and deliberately did not
       type it (it drifts CREDIT_CARD <-> AU_BANK_ACCOUNT on the same value
       between runs), so ANY specific class overlapping it wins the
       placeholder. This is the refinement in `merge_detections`.
    0. The *_INVALID shadows: "matched a pattern, failed its checksum" must
       never claim the placeholder from a real detection.
    """
    if r.entity_type in INVALID_ENTITY_TYPES:
        tier = 0
    elif r.entity_type == "IDENTIFIER_GENERIC":
        tier = 1
    else:
        tier = 2
    return (tier, r.score)


def _merge_overlaps(results):
    """Union overlapping spans into one; the merged span takes the
    highest-ranked member's entity type (see _rank) — except AU_BSB, which
    may label a union only if the BSB member itself covers the whole
    extent (issue #8b, 2026-07-22). A BSB is a routing code, the least
    identifying thing a union can contain, yet its context word promotes
    it past account scores ('BSB Cash Account Number 014-936 111873883':
    BSB 0.6+0.35 context = 0.95 beat the whole-run account guess at 0.93),
    so score alone would hide an account number under a BSB_n placeholder.
    A standalone BSB (union == its own span) keeps its label even when a
    grouped-account pattern double-covers the same digits. Recall-first:
    everything any span covered gets replaced."""
    unions: list[list] = []
    end = None
    for r in sorted(results, key=lambda r: (r.start, -r.end)):
        if unions and r.start < end:
            unions[-1].append(r)
            end = max(end, r.end)
        else:
            unions.append([r])
            end = r.end
    merged = []
    for members in unions:
        start = min(m.start for m in members)
        end = max(m.end for m in members)
        candidates = [
            m
            for m in members
            if m.entity_type != "AU_BSB"
            or (m.start == start and m.end == end)
        ] or members
        best = max(candidates, key=_rank)
        merged.append(
            Detection(
                entity_type=best.entity_type,
                start=start,
                end=end,
                score=best.score,
            )
        )
    return merged


def _collect_invalid(results, text: str) -> list[InvalidFinding]:
    """Invalid-class detections worth reporting.

    Two filters:
    - a candidate COVERED by a validated detection (VALIDATED_RECOGNIZERS —
      checksummed identifiers, Luhn cards, libphonenumber phones; matched
      by recognizer name, the discipline that kept an unvalidated NER guess
      of the same entity type from suppressing) is a valid identifier of another class, not a mangled
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
        if r.recognizer in VALIDATED_RULES
        and r.entity_type not in INVALID_ENTITY_TYPES
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

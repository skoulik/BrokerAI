"""Text stripping: detect -> locate -> splice placeholders.

The text counterpart of `image_mode`. A `TextDetector` (layer 0) reads the
document text and names the values; `locator.locate_in_text` places each one at
every occurrence, and layer 1 then refines, validates and extends them
(`PiiPipeline.merge_detections`), exactly as it does for the vision path.

**The detector is required, not optional.** Running layer 1 alone is the
patterns-only regime retired 2026-07-15 as unsafe (its name leaks), so no entry
point here lets a document be stripped without a semantic detector. Layer 1 on
its own is still reachable as `PiiPipeline.detect` — as a *layer*, which is
what `merge_detections` consumes — never as a way to strip a document.

There is no geometry leg at all, which is what makes this module so much
smaller than `image_mode`: the model quotes from the very string it was given,
so a located value needs no reconciliation and an unlocated one means the model
reformatted or invented it. Both paths end in `pipeline.apply_plan`, so a plan's
provenance never changes how it is applied.

A value the model reports but which is not in the text is surfaced, never
dropped — same invariant as the image path, and for the same reason: a
detection we cannot place is a detection we cannot redact.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

from presidio_analyzer import RecognizerResult

from pii.core.locator import locate_in_text
from pii.core.mapping import PseudonymMap
from pii.core.pipeline import InvalidFinding, PiiPipeline, apply_plan


@dataclass
class TextStripResult:
    text: str  # the pseudonymized text
    spans: list  # applied detections; offsets into the ORIGINAL text
    invalid: list[InvalidFinding]
    # Layer-0 findings that could not be located in the text. These are
    # UNREDACTED detections; a count that reaches the caller is the point,
    # because warnings alone are deduplicated by Python's default filter.
    unlocated: list = field(default_factory=list)


def detect_text(
    text: str, pipeline: PiiPipeline, detector
) -> tuple[list, list[InvalidFinding], list]:
    """One detection pass -> (strip plan, invalid findings, unlocated)."""
    placed = locate_in_text(detector.detect(text), text)
    detected = [
        RecognizerResult(
            entity_type=placement.finding.entity_type,
            start=start,
            end=end,
            # Layer 0 detects at 1.0; where it and layer 1 disagree on a
            # specific class the higher score wins, which is deliberate — it
            # is the better semantic detector. See merge_detections.
            score=1.0,
        )
        for placement in placed.located
        for start, end in placement.spans
    ]
    if placed.unlocated:
        warnings.warn(
            f"{len(placed.unlocated)} detected value(s) were not found in the "
            f"text and are NOT redacted (the model reformatted or invented "
            f"them): {[p.finding.text for p in placed.unlocated]!r}",
            RuntimeWarning,
            stacklevel=2,
        )
    spans, invalid = pipeline.merge_detections(detected, text)
    return spans, invalid, [p.finding for p in placed.unlocated]


def strip_text(
    text: str, pipeline: PiiPipeline, pmap: PseudonymMap, detector
) -> TextStripResult:
    """Replace detected PII in `text` with consistent placeholders."""
    spans, invalid, unlocated = detect_text(text, pipeline, detector)
    return TextStripResult(
        text=apply_plan(text, spans, pmap),
        spans=spans,
        invalid=invalid,
        unlocated=unlocated,
    )

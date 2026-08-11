"""Image stripping: detect -> locate in the OCR text -> paint placeholders.

Painting is pseudonymization, not blank redaction: each box is filled
with the page background color and the span's placeholder (PERSON_1) is
drawn into it, so the stripped image stays analyzable by a cloud model
and its answers can be rehydrated. A span crossing lines paints one box
per line, each carrying the placeholder — self-describing over compact.

The drawing toolkit itself (`Segment` / `paint_segments` / fill / frame)
lives in `pii.core.paint`, shared with the OCR-debug overlay; the names used
by callers and the eval harness are re-exported here for backward compat.

Detection is layer 0: a `VlmDetector` reads the page image and names the
values (`strip_from_vlm`); `pii.core.locator` places each one in the OCR text
and layer 1 refines, validates and extends them. The detector is REQUIRED —
running layer 1 alone over OCR text is the patterns-only regime retired
2026-07-15 as unsafe, and the layered path was deleted with GLiNER2 on
2026-08-09.

Painting geometry comes from OCR word boxes (the VLM's own
boxes are measured unsafe — see `pii.core.vlm`), so detection never decides
pixels and painting never sees raw analyzer results. The one exception is
the locator's tier-3 residue: values with no OCR text at all (a logo, a
barcode) where the model's padded box is the only geometry in existence.
Those are counted separately on the result, never mixed into the clean count.

The RecognizerInput in the returned ImageStripResult contains the recognized
plaintext INCLUDING the PII — like the pseudonym map, it is a local-only
artifact.
"""

import warnings
from dataclasses import dataclass, field

from PIL import Image
from pii.core.detection import Detection

from pii.core.linearization import RecognizerInput, linearize
from pii.core.locator import locate_findings
from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box, get_ocr_page
from pii.core.paint import Segment, paint_segments
from pii.core.pipeline import InvalidFinding, PiiPipeline
from pii.core.vlm import (
    DEFAULT_GEOMETRY,
    DEFAULT_PAD,
    GEOMETRIES,
    VlmFinding,
)

# The drawing toolkit moved to pii.core.paint (2026-07-24); re-exported so
# existing imports keep working — tests use `_grow` / `_FRAME_COLOR`, the eval
# harness uses `Segment` / `paint_segments`.
from pii.core.paint import _FRAME_COLOR, _grow  # noqa: F401


@dataclass
class ImageStripResult:
    image: Image.Image  # redacted RGB copy
    # Recognized text + word boxes — near-PII, local-only. A page-level
    # RecognizerInput, or None when the VLM supplied geometry directly and OCR
    # never ran — callers that report span text must handle that.
    ocr: object | None
    spans: list  # applied detections; offsets into ocr.text
    invalid: list[InvalidFinding]
    # What was actually painted. Equals one entry per span on the OCR paths,
    # but it is the ONLY record on the VLM-geometry path, which has no text to
    # take offsets into and therefore no spans.
    segments: list = field(default_factory=list)
    # Layer-0 findings painted from the MODEL's box because no OCR text
    # matched them (locator tier 3) — a distinct confidence class: stochastic
    # geometry, and no text means layer 1 never saw the value, so it carries
    # no checksum and no *_INVALID shadow. Counted, not merely warned about.
    box_geometry: list = field(default_factory=list)
    # Layer-0 findings with neither text nor usable geometry. These are
    # UNREDACTED detections; a count that reaches the caller is the point.
    unlocated: list = field(default_factory=list)


def strip_image(
    image: Image.Image,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    lang: str = "eng",
    ocr_backend: str = "paddle",
    *,
    detector,
    geometry: str = DEFAULT_GEOMETRY,
    pad: int = DEFAULT_PAD,
) -> ImageStripResult:
    """Detect the PII on the page and replace it with painted placeholders.

    `detector` (a `pii.core.vlm.VlmDetector`) is layer 0: the model reads the
    page image and names the values, and layer 1 then refines and extends
    them. `geometry` chooses how those values are placed on the page:
    "hybrid" (production) adds a second model pass for boxes and uses them to
    constrain the search, "ocr" searches the whole page string unconstrained,
    "vlm" paints the model's own boxes and skips OCR altogether. See
    `pii.core.vlm` and `pii.core.locator` for the rationale."""
    ocr_engine = None
    if geometry != "vlm":
        engine = get_ocr_page(ocr_backend)
        ocr_engine = lambda im: engine(im, lang=lang)  # noqa: E731
    return strip_rendered_page(
        image, pipeline, pmap, ocr_engine=ocr_engine, detector=detector,
        geometry=geometry, pad=pad,
    )


def strip_rendered_page(
    image: Image.Image,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    ocr_engine=None,
    *,
    detector,
    geometry: str = DEFAULT_GEOMETRY,
    pad: int = DEFAULT_PAD,
) -> ImageStripResult:
    """Strip one already-rendered page against an ALREADY-RESOLVED OCR engine
    (`image -> OcrPage`, or None only when `geometry="vlm"` and OCR never
    runs).

    The geometry dispatch lives here, in one place, so `strip_pdf` can resolve
    the engine once per document instead of once per page — and so both entry
    points share exactly one decision about what runs."""
    if geometry not in GEOMETRIES:
        raise ValueError(f"unknown geometry: {geometry!r}")
    findings = detector.detect(image)
    if geometry == "hybrid":
        # Pass 2: the boxes are a search constraint for the locator, not
        # paint geometry. Kept a separate call from detect() so pass 1
        # stays byte-identical to the measured recall baseline.
        findings = detector.localize(image, findings)
    ocr = None if geometry == "vlm" else linearize(ocr_engine(image))
    return strip_from_vlm(image, findings, pipeline, pmap, ocr=ocr, pad=pad)


def strip_from_vlm(
    image: Image.Image,
    findings: list[VlmFinding],
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    ocr: RecognizerInput | None = None,
    pad: int = DEFAULT_PAD,
) -> ImageStripResult:
    """Strip against layer-0 findings — the VLM detector seam.

    Geometry comes from one of two places, decided by whether `ocr` was
    supplied:

    - `ocr` given  → `locator.locate_findings` places every value, using any
      box the model gave as a search constraint (see `pii.core.locator` for
      why a box too unreliable to paint is reliable enough to disambiguate).
      Matched values paint through `painted_boxes_for_span` — OCR word boxes
      are exact — and because the OCR text is there, layer 1 runs on it too
      (`merge_detections`): it refines IDENTIFIER_GENERIC into the precise
      checksummed class, restores the `*_INVALID` shadows the VLM cannot
      produce, and adds what the model missed. Values that match nothing fall
      back to the model's own padded box, and values with neither text nor
      geometry warn loudly and are counted, because a detection we cannot
      paint is a detection we cannot redact.
    - `ocr` None   → the model's own `bbox_2d` is painted wholesale and OCR
      never runs, so there is no text for layer 1 to refine against either.
      Measured unsafe (see `pii.core.vlm`): 16% of boxes clip by >20 px,
      stochastically, and the tail includes real account numbers.

    Returns spans only on the OCR path — the VLM-geometry path has no text to
    take offsets into, so `ImageStripResult.ocr` is None there."""
    if ocr is None:
        return _paint_vlm_boxes(image, findings, pmap, pad)

    placed = locate_findings(findings, ocr, image.size)
    detected = [
        Detection(
            entity_type=p.finding.entity_type,
            start=p.start,
            end=p.end,
            score=1.0,
        )
        for p in placed.located
    ]
    # Tier 3 is subject to the same strip policy as everything else — the
    # prompt carries no institutional carve-outs by design, so the model
    # reports (and boxes) merchant logos, and the kept-ORGANIZATION rule is
    # what leaves them alone.
    box_only = [
        p
        for p in placed.box_only
        if pipeline.strips_value(p.finding.entity_type, p.finding.text)
    ]
    if box_only:
        warnings.warn(
            f"{len(box_only)} VLM finding(s) matched no OCR text and were "
            f"painted from the MODEL's own box — stochastic geometry, and no "
            f"layer-1 refinement or checksum was possible: "
            f"{[p.finding.text for p in box_only]!r}",
            RuntimeWarning,
            stacklevel=2,
        )
    if placed.unlocated:
        warnings.warn(
            f"{len(placed.unlocated)} VLM finding(s) could not be located in "
            f"the OCR text and had no usable box — these are unredacted "
            f"detections: {[p.finding.text for p in placed.unlocated]!r}",
            RuntimeWarning,
            stacklevel=2,
        )
    spans, invalid = pipeline.merge_detections(detected, ocr.text)
    # Placeholders for tier 3 are allocated after the plan's, so document
    # order still numbers everything that has offsets; a box-only finding has
    # none to sort by. Consistency is by value, not by order, so a value
    # appearing in both places keeps one placeholder.
    extra = [
        Segment(
            label=pmap.placeholder_for(p.finding.entity_type, p.finding.text),
            boxes=[p.box],
        )
        for p in box_only
    ]
    return _paint_plan(
        image, ocr, spans, invalid, pmap,
        extra=extra,
        box_geometry=[p.finding for p in box_only],
        unlocated=[p.finding for p in placed.unlocated],
    )


def _paint_vlm_boxes(image, findings, pmap, pad) -> ImageStripResult:
    """Paint the model's own boxes. No OCR, so no text and no offsets."""
    width, height = image.size
    segments = []
    for finding in findings:
        if finding.box is None:
            continue
        x1, y1, x2, y2 = finding.box
        left = max(int(x1 / 1000 * width) - pad, 0)
        top = max(int(y1 / 1000 * height) - pad, 0)
        right = min(int(x2 / 1000 * width) + pad, width)
        bottom = min(int(y2 / 1000 * height) + pad, height)
        if right <= left or bottom <= top:
            continue
        segments.append(
            Segment(
                label=pmap.placeholder_for(finding.entity_type, finding.text),
                boxes=[Box(left, top, right - left, bottom - top)],
            )
        )
    return ImageStripResult(
        image=paint_segments(image, segments), ocr=None, spans=[], invalid=[],
        segments=segments,
    )


def _paint_plan(
    image, ocr, spans, invalid, pmap, extra=(), box_geometry=(), unlocated=()
) -> ImageStripResult:
    """Allocate a placeholder per span and paint it over the span's pixels.
    Spans arrive in document order, which is the numbering order. `extra`
    carries pre-built segments that have no offsets to sort by (locator tier
    3) and are painted alongside."""
    segments = [
        Segment(
            label=pmap.placeholder_for(r.entity_type, ocr.text[r.start : r.end]),
            boxes=ocr.painted_boxes_for_span(r.start, r.end),
        )
        for r in spans
    ] + list(extra)
    out = paint_segments(image, segments)
    return ImageStripResult(
        image=out, ocr=ocr, spans=spans, invalid=invalid, segments=segments,
        box_geometry=list(box_geometry), unlocated=list(unlocated),
    )

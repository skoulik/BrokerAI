"""Image stripping: OCR -> text pipeline -> paint placeholders on pixels.

The image path reuses the WHOLE text pipeline (all detection layers,
overlap merging, invalid-identifier collection) by running it on the
OCR-assembled text, then mapping each merged span back to pixel boxes
(pii.core.ocr.OcrResult.painted_boxes_for_span) and painting on the ORIGINAL
image — detection never sees pixels, painting never sees raw analyzer results.

Painting is pseudonymization, not blank redaction: each box is filled
with the page background color and the span's placeholder (PERSON_1) is
drawn into it, so the stripped image stays analyzable by a cloud model
and its answers can be rehydrated. A span crossing lines paints one box
per line, each carrying the placeholder — self-describing over compact.

The drawing toolkit itself (`Segment` / `paint_segments` / fill / frame)
lives in `pii.core.paint`, shared with the OCR-debug overlay; the names used
by callers and the eval harness are re-exported here for backward compat.

Two feeds reach the pipeline, selected by `feed`:

- "page" — the whole page as one string (the historical assembly), either
  from the flat `OcrResult` path (`strip_from_ocr`) or from an `OcrPage`
  linearized whole (`strip_from_page`);
- "blocks" — one recognizer call per layout block (`strip_from_page`), so
  nothing detects across a block boundary. The per-block results are
  rebased into one page-level view before painting, so everything
  downstream of detection is identical either way.

The OcrResult/RecognizerInput in the returned ImageStripResult contains the
recognized plaintext INCLUDING the PII — like the pseudonym map, it is a
local-only artifact.
"""

import warnings
from dataclasses import dataclass, field, replace

from PIL import Image
from presidio_analyzer import RecognizerResult

from pii.core.linearization import linearize, linearize_blocks, rebase
from pii.core.mapping import PseudonymMap
from pii.core.ocr import OCR_BACKENDS, Box, OcrResult, get_ocr, get_ocr_page
from pii.core.ocr_page import OcrPage
from pii.core.paint import Segment, paint_segments
from pii.core.pipeline import InvalidFinding, PiiPipeline
from pii.core.vlm import DEFAULT_PAD, VlmFinding, locate

# The drawing toolkit moved to pii.core.paint (2026-07-24); re-exported so
# existing imports keep working — tests use `_grow` / `_FRAME_COLOR`, the eval
# harness uses `Segment` / `paint_segments`.
from pii.core.paint import _FRAME_COLOR, _grow  # noqa: F401


@dataclass
class ImageStripResult:
    image: Image.Image  # redacted RGB copy
    # Recognized text + word boxes — near-PII, local-only. An OcrResult on
    # the flat path, a page-level RecognizerInput on the OcrPage path; both
    # answer .text and .painted_boxes_for_span, which is all callers use.
    # None when the VLM supplied geometry directly and OCR never ran — callers
    # that report span text must handle that.
    ocr: object | None
    spans: list  # applied detections; offsets into ocr.text
    invalid: list[InvalidFinding]
    # What was actually painted. Equals one entry per span on the OCR paths,
    # but it is the ONLY record on the VLM-geometry path, which has no text to
    # take offsets into and therefore no spans.
    segments: list = field(default_factory=list)


def strip_image(
    image: Image.Image,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    lang: str = "eng",
    ocr_backend: str = "doclayout:v3",
    feed: str = "blocks",
    detector=None,
    geometry: str = "ocr",
    pad: int = DEFAULT_PAD,
) -> ImageStripResult:
    """OCR the image and replace detected PII with painted placeholders.

    Defaults to layout perception + the per-block feed (one recognizer call
    per layout block). `feed="page"` on a flat backend name keeps the
    historical `OcrResult` path; any other combination goes through the
    `OcrPage` perception layer.

    `detector` (a `pii.core.vlm.VlmDetector`) replaces layers 1+2 entirely —
    the model reads the page image and produces the plan itself. `geometry`
    then chooses where the boxes come from: "ocr" locates each value in the
    OCR text (exact word boxes; OCR still runs), "vlm" uses the model's own
    boxes and skips OCR altogether. See `pii.core.vlm` for why "ocr" is the
    safe default."""
    if detector is not None:
        findings = detector.detect(image)
        if geometry == "vlm":
            return strip_from_vlm(image, findings, pmap, ocr=None, pad=pad)
        if geometry != "ocr":
            raise ValueError(f"unknown geometry: {geometry!r}")
        # Whole-page linearization: the VLM detects across the entire page, so
        # per-block offsets would have nothing to rebase against.
        ocr = linearize(get_ocr_page(ocr_backend)(image, lang=lang))
        return strip_from_vlm(image, findings, pmap, ocr=ocr, pad=pad)

    # OCR_BACKENDS are the flat (OcrResult) engine names; the OcrPage
    # backends are a superset, so only this combination stays on the old path.
    if feed == "page" and ocr_backend in OCR_BACKENDS:
        ocr = get_ocr(ocr_backend)(image, lang=lang)
        return strip_from_ocr(image, ocr, pipeline, pmap)
    page = get_ocr_page(ocr_backend)(image, lang=lang)
    return strip_from_page(image, page, pipeline, pmap, feed=feed)


def strip_from_ocr(
    image: Image.Image,
    ocr: OcrResult,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
) -> ImageStripResult:
    """Strip against an existing OCR result (separate seam so the OCR
    engine bake-off and the PDF page loop can reuse the painting path)."""
    spans, invalid = pipeline.detect(ocr.text)
    return _paint_plan(image, ocr, spans, invalid, pmap)


def strip_from_page(
    image: Image.Image,
    page: OcrPage,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    feed: str = "blocks",
) -> ImageStripResult:
    """Strip against an OcrPage — the perception-layer seam.

    `feed="blocks"` runs the pipeline once per layout block and rebases
    every result into the page-level view; `feed="page"` linearizes the
    whole page and runs it once (the historical assembly over the new
    perception layer). Detection is the only thing that differs: both end
    up with one page text, one document-ordered plan and one painting pass.

    Blocks are processed in reading order and spans within a block in
    document order, so placeholder numbering stays document order."""
    if feed == "blocks":
        parts = linearize_blocks(page)
    elif feed == "page":
        parts = (linearize(page),)
    else:
        raise ValueError(f"unknown feed: {feed!r}")
    # A block with no recognized characters gets no analyzer call — it is a
    # skipped model invocation, not a skipped line (blank parts still take
    # their place in the rebase, so offsets and page text stay exact).
    detected = [
        pipeline.detect(part.text) if part.text.strip() else ([], [])
        for part in parts
    ]
    ocr, offsets = rebase(parts)
    spans = [
        RecognizerResult(
            entity_type=r.entity_type,
            start=r.start + offset,
            end=r.end + offset,
            score=r.score,
        )
        for offset, (part_spans, _) in zip(offsets, detected)
        for r in part_spans
    ]
    invalid = [
        replace(f, start=f.start + offset, end=f.end + offset)
        for offset, (_, part_invalid) in zip(offsets, detected)
        for f in part_invalid
    ]
    return _paint_plan(image, ocr, spans, invalid, pmap)


def strip_from_vlm(
    image: Image.Image,
    findings: list[VlmFinding],
    pmap: PseudonymMap,
    ocr: OcrResult | None = None,
    pad: int = DEFAULT_PAD,
) -> ImageStripResult:
    """Strip against VLM findings — the alternative-detector seam.

    Geometry comes from one of two places, and which one is decided by whether
    `ocr` was supplied:

    - `ocr` given  → each value is located in the OCR text and painted through
      `painted_boxes_for_span`. OCR word boxes are exact, so this is the safe
      path. A value that cannot be located is a detection we cannot paint —
      i.e. a leak — so it warns loudly rather than disappearing.
    - `ocr` None   → the model's own `bbox_2d` is used and OCR never runs.
      Measured unsafe (see `pii.core.vlm`): 16% of boxes clip by >20 px,
      stochastically, and the tail includes real account numbers.

    Returns spans only on the OCR path — the VLM-geometry path has no text to
    take offsets into, so `ImageStripResult.ocr` is None there."""
    if ocr is None:
        return _paint_vlm_boxes(image, findings, pmap, pad)

    spans, taken, unlocated = [], [], []
    for finding in findings:
        found = locate(ocr.text, finding.text, taken)
        if found is None:
            unlocated.append(finding)
            continue
        start, end = found
        taken.append((start, end))
        spans.append(
            RecognizerResult(
                entity_type=finding.entity_type, start=start, end=end, score=1.0
            )
        )
    if unlocated:
        warnings.warn(
            f"{len(unlocated)} VLM finding(s) could not be located in the OCR "
            f"text and were NOT painted — these are unredacted detections: "
            f"{[f.text for f in unlocated]!r}",
            RuntimeWarning,
            stacklevel=2,
        )
    spans.sort(key=lambda r: (r.start, r.end))
    return _paint_plan(image, ocr, spans, [], pmap)


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


def _paint_plan(image, ocr, spans, invalid, pmap) -> ImageStripResult:
    """Allocate a placeholder per span and paint it over the span's pixels.
    Spans arrive in document order, which is the numbering order."""
    segments = [
        Segment(
            label=pmap.placeholder_for(r.entity_type, ocr.text[r.start : r.end]),
            boxes=ocr.painted_boxes_for_span(r.start, r.end),
        )
        for r in spans
    ]
    out = paint_segments(image, segments)
    return ImageStripResult(
        image=out, ocr=ocr, spans=spans, invalid=invalid, segments=segments
    )

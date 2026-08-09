"""Image stripping: detect -> locate in the OCR text -> paint placeholders.

Painting is pseudonymization, not blank redaction: each box is filled
with the page background color and the span's placeholder (PERSON_1) is
drawn into it, so the stripped image stays analyzable by a cloud model
and its answers can be rehydrated. A span crossing lines paints one box
per line, each carrying the placeholder — self-describing over compact.

The drawing toolkit itself (`Segment` / `paint_segments` / fill / frame)
lives in `pii.core.paint`, shared with the OCR-debug overlay; the names used
by callers and the eval harness are re-exported here for backward compat.

Two detectors reach the painting path, and they differ only in what produces
the plan — everything from `_paint_plan` down is shared:

- `detector=None` (`strip_from_page`) — the layered path: OCR the page,
  linearize it, run the whole text pipeline on that string.
- a `VlmDetector` (`strip_from_vlm`) — layer 0 reads the page image and names
  the values; each is located in the OCR text and refined by layer 1.

Painting geometry comes from OCR word boxes in both cases (the VLM's own
boxes are measured unsafe — see `pii.core.vlm`), so detection never decides
pixels and painting never sees raw analyzer results.

The RecognizerInput in the returned ImageStripResult contains the recognized
plaintext INCLUDING the PII — like the pseudonym map, it is a local-only
artifact.
"""

import warnings
from dataclasses import dataclass, field

from PIL import Image
from presidio_analyzer import RecognizerResult

from pii.core.linearization import RecognizerInput, linearize
from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box, get_ocr_page
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


def strip_image(
    image: Image.Image,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    lang: str = "eng",
    ocr_backend: str = "paddle",
    detector=None,
    geometry: str = "ocr",
    pad: int = DEFAULT_PAD,
) -> ImageStripResult:
    """Detect the PII on the page and replace it with painted placeholders.

    `detector` (a `pii.core.vlm.VlmDetector`) makes layer 0 the detector: the
    model reads the page image and names the values, and layer 1 then refines
    and extends them. `geometry` chooses where the boxes come from: "ocr"
    locates each value in the OCR text (exact word boxes; OCR still runs),
    "vlm" uses the model's own boxes and skips OCR altogether. See
    `pii.core.vlm` for why "ocr" is the production path.

    With `detector=None` the layered path runs instead: OCR the page,
    linearize it, and feed the whole page string to the text pipeline."""
    ocr_engine = None
    if _needs_ocr(detector, geometry):
        engine = get_ocr_page(ocr_backend)
        ocr_engine = lambda im: engine(im, lang=lang)  # noqa: E731
    return strip_rendered_page(
        image, pipeline, pmap, ocr_engine=ocr_engine, detector=detector,
        geometry=geometry, pad=pad,
    )


def _needs_ocr(detector, geometry: str) -> bool:
    """Whether OCR has to run at all. Only one configuration skips it —
    a VLM detector painting its own boxes."""
    return detector is None or geometry != "vlm"


def strip_rendered_page(
    image: Image.Image,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    ocr_engine=None,
    detector=None,
    geometry: str = "ocr",
    pad: int = DEFAULT_PAD,
) -> ImageStripResult:
    """Strip one already-rendered page against an ALREADY-RESOLVED OCR engine
    (`image -> OcrPage`, or None only when `geometry="vlm"` and OCR never
    runs).

    The detector/geometry dispatch lives here, in one place, so `strip_pdf`
    can resolve the engine once per document instead of once per page — and
    so both entry points share exactly one decision about what runs."""
    if detector is not None:
        if geometry not in ("ocr", "vlm"):
            raise ValueError(f"unknown geometry: {geometry!r}")
        findings = detector.detect(image)
        ocr = None if geometry == "vlm" else linearize(ocr_engine(image))
        return strip_from_vlm(image, findings, pipeline, pmap, ocr=ocr, pad=pad)
    return strip_from_page(image, ocr_engine(image), pipeline, pmap)


def strip_from_page(
    image: Image.Image,
    page: OcrPage,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
) -> ImageStripResult:
    """Strip against an OcrPage through the layered detector — a separate
    seam so the PDF page loop and the eval harness reuse the painting path
    without re-running OCR."""
    ocr = linearize(page)
    spans, invalid = pipeline.detect(ocr.text)
    return _paint_plan(image, ocr, spans, invalid, pmap)


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

    - `ocr` given  → each value is located in the OCR text and painted through
      `painted_boxes_for_span`. OCR word boxes are exact, so this is the safe
      path — and because the OCR text is there, layer 1 runs on it too
      (`merge_detections`): it refines IDENTIFIER_GENERIC into the precise
      checksummed class, restores the `*_INVALID` shadows the VLM cannot
      produce, and adds what the model missed. A value that cannot be located
      is a detection we cannot paint — i.e. a leak — so it warns loudly
      rather than disappearing.
    - `ocr` None   → the model's own `bbox_2d` is used and OCR never runs, so
      there is no text for layer 1 to refine against either. Measured unsafe
      (see `pii.core.vlm`): 16% of boxes clip by >20 px, stochastically, and
      the tail includes real account numbers.

    Returns spans only on the OCR path — the VLM-geometry path has no text to
    take offsets into, so `ImageStripResult.ocr` is None there."""
    if ocr is None:
        return _paint_vlm_boxes(image, findings, pmap, pad)

    detected, taken, unlocated = [], [], []
    for finding in findings:
        found = locate(ocr.text, finding.text, taken)
        if found is None:
            unlocated.append(finding)
            continue
        start, end = found
        taken.append((start, end))
        detected.append(
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
    spans, invalid = pipeline.merge_detections(detected, ocr.text)
    return _paint_plan(image, ocr, spans, invalid, pmap)


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

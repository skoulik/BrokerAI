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

The OcrResult in the returned ImageStripResult contains the recognized
plaintext INCLUDING the PII — like the pseudonym map, it is a local-only
artifact.
"""

from dataclasses import dataclass

from PIL import Image

from pii.core.mapping import PseudonymMap
from pii.core.ocr import OcrResult, get_ocr
from pii.core.paint import Segment, paint_segments
from pii.core.pipeline import InvalidFinding, PiiPipeline

# The drawing toolkit moved to pii.core.paint (2026-07-24); re-exported so
# existing imports keep working — tests use `_grow` / `_FRAME_COLOR`, the eval
# harness uses `Segment` / `paint_segments`.
from pii.core.paint import _FRAME_COLOR, _grow  # noqa: F401


@dataclass
class ImageStripResult:
    image: Image.Image  # redacted RGB copy
    ocr: OcrResult  # recognized text + word boxes — near-PII, local-only
    spans: list  # applied detections; offsets into ocr.text
    invalid: list[InvalidFinding]


def strip_image(
    image: Image.Image,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    lang: str = "eng",
    ocr_backend: str = "paddle",
) -> ImageStripResult:
    """OCR the image and replace detected PII with painted placeholders."""
    ocr = get_ocr(ocr_backend)(image, lang=lang)
    return strip_from_ocr(image, ocr, pipeline, pmap)


def strip_from_ocr(
    image: Image.Image,
    ocr: OcrResult,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
) -> ImageStripResult:
    """Strip against an existing OCR result (separate seam so the OCR
    engine bake-off and the PDF page loop can reuse the painting path)."""
    spans, invalid = pipeline.detect(ocr.text)
    segments = [
        Segment(
            label=pmap.placeholder_for(r.entity_type, ocr.text[r.start : r.end]),
            boxes=ocr.painted_boxes_for_span(r.start, r.end),
        )
        for r in spans  # detect() returns document order == numbering order
    ]
    out = paint_segments(image, segments)
    return ImageStripResult(image=out, ocr=ocr, spans=spans, invalid=invalid)

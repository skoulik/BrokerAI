"""Image stripping: OCR -> text pipeline -> paint placeholders on pixels.

The image path reuses the WHOLE text pipeline (all detection layers,
overlap merging, invalid-identifier collection) by running it on the
OCR-assembled text, then mapping each merged span back to pixel boxes
(pii.core.ocr.OcrResult.boxes_for_span) and painting on the ORIGINAL image —
detection never sees pixels, painting never sees raw analyzer results.

Painting is pseudonymization, not blank redaction: each box is filled
with the page background color and the span's placeholder (PERSON_1) is
drawn into it, so the stripped image stays analyzable by a cloud model
and its answers can be rehydrated. A span crossing lines paints one box
per line, each carrying the placeholder — self-describing over compact.

The OcrResult in the returned ImageStripResult contains the recognized
plaintext INCLUDING the PII — like the pseudonym map, it is a local-only
artifact.
"""

from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box, OcrResult, _background_color, get_ocr
from pii.core.pipeline import InvalidFinding, PiiPipeline

# Painted boxes are grown by this many pixels per side: word boxes are
# glyph-tight and antialiased edges would survive as a readable fringe.
_MARGIN = 2

_MIN_FONT = 8


@dataclass
class ImageStripResult:
    image: Image.Image  # redacted RGB copy
    ocr: OcrResult  # recognized text + word boxes — near-PII, local-only
    spans: list  # applied detections; offsets into ocr.text
    invalid: list[InvalidFinding]


@dataclass(frozen=True)
class Segment:
    """One painted placeholder: the label and the pixel boxes it covers
    (one box per text line for a line-crossing span). The seam between
    detection and painting — the pipeline produces segments from merged
    spans, and the eval harness produces them straight from ground-truth
    markup, so both paint through the identical code path."""

    label: str
    boxes: list[Box]


def paint_segments(
    image: Image.Image,
    segments: list[Segment],
    margin: int = _MARGIN,
    style: str = "fill",
) -> Image.Image:
    """Paint every segment onto a copy of the image. The input image is
    not mutated.

    style="fill" (production): each box is filled with the page
    background color and the label drawn into it — the content is gone.
    style="frame" (review): each box gets an outline rectangle with the
    label on a chip above it — the content stays readable underneath.
    The ground-truth renderer uses this to make markup auditable."""
    if style not in ("fill", "frame"):
        raise ValueError(f"unknown paint style: {style!r}")
    out = image.convert("RGB")
    fill = _background_color(out)
    ink = (0, 0, 0) if _luminance(fill) > 127 else (255, 255, 255)
    for seg in segments:
        for box in seg.boxes:
            grown = _grow(box, margin, out)
            if style == "fill":
                _paint(out, grown, seg.label, fill, ink)
            else:
                _frame(out, grown, seg.label)
    return out


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
            boxes=ocr.boxes_for_span(r.start, r.end),
        )
        for r in spans  # detect() returns document order == numbering order
    ]
    out = paint_segments(image, segments)
    return ImageStripResult(image=out, ocr=ocr, spans=spans, invalid=invalid)


def _grow(box: Box, margin: int, image: Image.Image) -> Box:
    left = max(box.left - margin, 0)
    top = max(box.top - margin, 0)
    return Box(
        left=left,
        top=top,
        width=min(box.right + margin, image.width) - left,
        height=min(box.bottom + margin, image.height) - top,
    )


def _paint(image, box: Box, label: str, fill, ink) -> None:
    """Fill the box and draw the label into it, shrinking the font to fit
    the width. Drawn on a box-sized layer, so an oversized label clips at
    the box edge instead of overpainting neighboring text."""
    layer = Image.new("RGB", (box.width, box.height), fill)
    draw = ImageDraw.Draw(layer)
    size = max(int(box.height * 0.8), _MIN_FONT)
    font = _font(size)
    while size > _MIN_FONT and draw.textlength(label, font=font) > box.width - 2:
        size -= 1
        font = _font(size)
    draw.text((1, box.height // 2), label, font=font, fill=ink, anchor="lm")
    image.paste(layer, (box.left, box.top))


_FRAME_COLOR = (220, 30, 30)


def _frame(image, box: Box, label: str) -> None:
    """Outline the box and write the label on a chip above it (inside
    the top edge when there is no room above)."""
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (box.left, box.top, box.right - 1, box.bottom - 1),
        outline=_FRAME_COLOR,
        width=3,
    )
    size = min(max(int(box.height * 0.45), 14), 30)
    font = _font(size)
    chip_w = int(draw.textlength(label, font=font)) + 6
    chip_h = size + 4
    top = box.top - chip_h if box.top >= chip_h else box.top
    left = max(min(box.left, image.width - chip_w), 0)
    draw.rectangle((left, top, left + chip_w, top + chip_h), fill=_FRAME_COLOR)
    draw.text(
        (left + 3, top + chip_h // 2),
        label,
        font=font,
        fill=(255, 255, 255),
        anchor="lm",
    )


@lru_cache(maxsize=None)
def _font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default(size)


def _luminance(color) -> float:
    r, g, b = color[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b

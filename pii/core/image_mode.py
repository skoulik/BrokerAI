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
from pii.core.ocr import Box, OcrResult, _background_color, ocr_image
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


def strip_image(
    image: Image.Image,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    lang: str = "eng",
) -> ImageStripResult:
    """OCR the image and replace detected PII with painted placeholders."""
    return strip_from_ocr(image, ocr_image(image, lang=lang), pipeline, pmap)


def strip_from_ocr(
    image: Image.Image,
    ocr: OcrResult,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
) -> ImageStripResult:
    """Strip against an existing OCR result (separate seam so the OCR
    engine bake-off and the PDF page loop can reuse the painting path)."""
    spans, invalid = pipeline.detect(ocr.text)
    out = image.convert("RGB")
    fill = _background_color(out)
    ink = (0, 0, 0) if _luminance(fill) > 127 else (255, 255, 255)
    for r in spans:  # detect() returns document order == numbering order
        placeholder = pmap.placeholder_for(r.entity_type, ocr.text[r.start : r.end])
        for box in ocr.boxes_for_span(r.start, r.end):
            _paint(out, _grow(box, _MARGIN, out), placeholder, fill, ink)
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


@lru_cache(maxsize=None)
def _font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default(size)


def _luminance(color) -> float:
    r, g, b = color[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b

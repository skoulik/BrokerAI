"""Placeholder / annotation drawing on page rasters — the shared drawing
toolkit.

Extracted from image_mode (2026-07-24) so both the strip painter (paint-over
placeholders) and the OCR-debug overlay (rectangles) reuse one implementation
without the debug path pulling in the analysis stack — image_mode imports the
detection pipeline, this module imports only Pillow + the neutral geometry.

A `Segment` is a label plus the pixel boxes it covers; `paint_segments`
renders a list of them in one of two styles:

- ``style="fill"`` (production strip): fill each box with the page background
  and draw the label into it — the content is gone (pseudonymization).
- ``style="frame"`` (review / overlay): outline each box and, when the label
  is non-empty, write it on a chip above — the content stays readable. `color`
  and `width` parameterize the outline (the debug overlay uses them to
  distinguish lines / detected blocks / synthetic blocks).
"""

import warnings
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

from pii.core.ocr import Box, _background_color

# Painted boxes are grown by this many pixels per side: word boxes are
# glyph-tight and antialiased edges would survive as a readable fringe.
_MARGIN = 2
_MIN_FONT = 8
_FRAME_COLOR = (220, 30, 30)


@dataclass(frozen=True)
class Segment:
    """One painted placeholder: the label and the pixel boxes it covers (one
    box per text line for a line-crossing span). The seam between detection
    and painting — the pipeline produces segments from merged spans, and the
    eval harness produces them straight from ground-truth markup, so both
    paint through the identical code path."""

    label: str
    boxes: list[Box]


def paint_segments(
    image: Image.Image,
    segments: list[Segment],
    margin: int = _MARGIN,
    style: str = "fill",
    color=_FRAME_COLOR,
    width: int = 3,
) -> Image.Image:
    """Paint every segment onto a copy of the image. The input image is not
    mutated.

    style="fill" (production): each box is filled with the page background
    color and the label drawn into it — the content is gone.
    style="frame" (review): each box gets an outline rectangle (`color`,
    `width`) with the label on a chip above it — the content stays readable
    underneath. The ground-truth renderer and the OCR-debug overlay use this."""
    if style not in ("fill", "frame"):
        raise ValueError(f"unknown paint style: {style!r}")
    out = image.convert("RGB")
    fill = _background_color(out)
    ink = (0, 0, 0) if _luminance(fill) > 127 else (255, 255, 255)
    for seg in segments:
        for box in seg.boxes:
            grown = _grow(box, margin, out)
            if grown.width <= 0 or grown.height <= 0:
                # A degenerate box paints nothing; skip it rather than let
                # Image.new reject a negative dimension and abort the whole
                # page. It must NOT pass unnoticed: an unpainted box means PII
                # pixels may have survived, so warn with the geometry.
                warnings.warn(
                    f"skipping degenerate paint box for {seg.label!r}: "
                    f"raw={box} grown={grown} on {out.width}x{out.height} "
                    "image — PII pixels for this span may survive; check "
                    "OcrResult.painted_boxes_for_span geometry",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            if style == "fill":
                _paint(out, grown, seg.label, fill, ink)
            else:
                _frame(out, grown, seg.label, color, width)
    return out


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


def _frame(image, box: Box, label: str, color=_FRAME_COLOR, width: int = 3) -> None:
    """Outline the box and, when `label` is non-empty, write it on a chip
    above (inside the top edge when there is no room above)."""
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (box.left, box.top, box.right - 1, box.bottom - 1),
        outline=color,
        width=width,
    )
    if not label:
        return
    size = min(max(int(box.height * 0.45), 14), 30)
    font = _font(size)
    chip_w = int(draw.textlength(label, font=font)) + 6
    chip_h = size + 4
    top = box.top - chip_h if box.top >= chip_h else box.top
    left = max(min(box.left, image.width - chip_w), 0)
    draw.rectangle((left, top, left + chip_w, top + chip_h), fill=color)
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

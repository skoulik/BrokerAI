"""Placeholder / annotation drawing on page rasters — the shared drawing
toolkit.

Extracted from image_mode (2026-07-24) so both the strip painter (paint-over
placeholders) and the OCR-debug overlay (rectangles) reuse one implementation
without the debug path pulling in the analysis stack — image_mode imports the
detection pipeline, this module imports only Pillow + the neutral geometry.

A `Segment` is a label plus the pixel boxes it covers — and, where the input
could tell us, the face that text was set in and the rotation it was printed
at, so a filled placeholder reads like the line it replaces instead of like an
annotation. `paint_segments` renders a list of them in one of two styles:

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
from pii.core.ocr_page import FontSpec

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
    paint through the identical code path.

    `font` is the face the replaced text was set in, where the input told us
    (`pii.core.text_layer` traceback of a PDF's own text layer). None — every
    non-PDF input, and any word the text layer did not cover — falls back to
    the painter's default face and its box-height sizing.

    `rotation` is how the replaced text was PRINTED (`pii.core.ocr.ROTATIONS`),
    so a placeholder over a page-edge stripe is drawn along the stripe. Painted
    upright into a 29x475 box it would shrink to the minimum size and clip
    anyway: the fill covers the pixels either way, but the output stops being
    self-describing, and a placeholder nobody can read cannot be rehydrated by
    hand."""

    label: str
    boxes: list[Box]
    font: FontSpec | None = None
    rotation: int = 0


def paint_segments(
    image: Image.Image,
    segments: list[Segment],
    margin: int = _MARGIN,
    style: str = "fill",
    color=_FRAME_COLOR,
    width: int = 3,
    chip: str = "above",
) -> Image.Image:
    """Paint every segment onto a copy of the image. The input image is not
    mutated.

    style="fill" (production): each box is filled with the page background
    color and the label drawn into it — the content is gone.
    style="frame" (review): each box gets an outline rectangle (`color`,
    `width`) with the label on a chip above it — the content stays readable
    underneath. The ground-truth renderer and the debug overlay use this.
    `chip="none"` draws the outline only, for annotations that would be
    illegible labelled: the debug overlay outlines every OCR word on the page,
    and a chip on each would bury the page under its own labels."""
    if style not in ("fill", "frame"):
        raise ValueError(f"unknown paint style: {style!r}")
    if chip not in ("above", "none"):
        raise ValueError(f"unknown chip setting: {chip!r}")
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
                    "RecognizerInput.painted_boxes_for_span geometry",
                    RuntimeWarning,
                    stacklevel=2,
                )
                continue
            if style == "fill":
                _paint(out, grown, seg.label, fill, ink, seg.font, seg.rotation)
            else:
                _frame(out, grown, seg.label, color, width, chip)
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


def _paint(image, box: Box, label: str, fill, ink, spec=None, rotation=0) -> None:
    """Fill the box and draw the label into it, shrinking the font to fit
    the width. Drawn on a box-sized layer, so an oversized label clips at
    the box edge instead of overpainting neighboring text.

    `spec` (a `FontSpec`) makes the placeholder read like the text it replaces
    — bold where the original was bold, monospaced where it was monospaced, and
    at the document's own point size rather than a guess from the box. The box
    is grown and includes the region's ink margin, so `height * 0.8` overstates
    the real face; the true size is used where it is known and still capped to
    the box.

    `rotation` replaces text that was printed sideways. The label is drawn
    upright on a layer of the box's own dimensions SWAPPED — so all the fitting
    above still reads "along the line" and "across the line" without a second
    version of it — and the layer is then turned by the rotation, which lands it
    exactly on the box."""
    width, height = (
        (box.height, box.width) if rotation else (box.width, box.height)
    )
    layer = Image.new("RGB", (width, height), fill)
    draw = ImageDraw.Draw(layer)
    if spec is not None and spec.size:
        size = max(min(int(round(spec.size)), height), _MIN_FONT)
    else:
        size = max(int(height * 0.8), _MIN_FONT)
    font = _face(spec, size)
    while size > _MIN_FONT and draw.textlength(label, font=font) > width - 2:
        size -= 1
        font = _face(spec, size)
    draw.text((1, height // 2), label, font=font, fill=ink, anchor="lm")
    if rotation:
        layer = layer.transpose(
            Image.ROTATE_90 if rotation == 90 else Image.ROTATE_270
        )
    image.paste(layer, (box.left, box.top))


def _frame(
    image, box: Box, label: str, color=_FRAME_COLOR, width: int = 3,
    chip: str = "above",
) -> None:
    """Outline the box and, when `label` is non-empty and `chip` asks for one,
    write it on a chip above (inside the top edge when there is no room
    above)."""
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (box.left, box.top, box.right - 1, box.bottom - 1),
        outline=color,
        width=width,
    )
    if not label or chip == "none":
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


# Family -> (regular, bold, italic, bold-italic) file names, in the naming
# every one of these families actually ships with. Pillow resolves a bare file
# name through the platform font directories.
_FAMILY_FILES = {
    "arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "times": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "courier": ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"),
    "calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "verdana": ("verdana.ttf", "verdanab.ttf", "verdanai.ttf", "verdanaz.ttf"),
    "tahoma": ("tahoma.ttf", "tahomabd.ttf", "tahoma.ttf", "tahomabd.ttf"),
    "georgia": ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    "consolas": ("consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf"),
    # Last-resort family, present where the ones above are not.
    "dejavu-sans": (
        "DejaVuSans.ttf", "DejaVuSans-Bold.ttf",
        "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf",
    ),
    "dejavu-serif": (
        "DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf",
        "DejaVuSerif-Italic.ttf", "DejaVuSerif-BoldItalic.ttf",
    ),
    "dejavu-mono": (
        "DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf",
        "DejaVuSansMono-Oblique.ttf", "DejaVuSansMono-BoldOblique.ttf",
    ),
}

# Document font name -> the family we render it in. The base-14 names and the
# handful of families that are actually installed; everything else falls
# through to the serif/mono/sans class, which is what the spec is for.
_FAMILY_ALIASES = {
    "helvetica": "arial", "helveticaneue": "arial", "arial": "arial",
    "arialnarrow": "arial", "liberationsans": "arial",
    "times": "times", "timesnewroman": "times", "liberationserif": "times",
    "courier": "courier", "couriernew": "courier",
    "calibri": "calibri", "verdana": "verdana", "tahoma": "tahoma",
    "georgia": "georgia", "consolas": "consolas",
}

# Fallback order per class: the class family first, then its DejaVu twin.
_CLASS_FAMILIES = {
    "mono": ("courier", "dejavu-mono"),
    "serif": ("times", "dejavu-serif"),
    "sans": ("arial", "dejavu-sans"),
}


def _face(spec, size: int):
    """Resolve a `FontSpec` to a drawable face at `size`, falling back to the
    default face when the document named nothing we have.

    The DOCUMENT's own embedded font is deliberately not used, although
    pymupdf can extract it. Measured: 8 of the 11 fonts on one reference page
    are Identity-H CID subsets, through which Pillow renders `PERSON_1` as
    zero-height nothing — a filled box with an invisible label, which is a
    silently unreadable output rather than a cosmetic miss."""
    if spec is None:
        return _font(size)
    return _font_file(
        _family(spec), spec.bold, spec.italic, size
    ) or _font(size)


def _family(spec) -> str:
    """The family to render a spec in: what the document named if we have it,
    otherwise its class."""
    named = _FAMILY_ALIASES.get(_normalized(spec.name))
    if named:
        return named
    return "mono" if spec.mono else "serif" if spec.serif else "sans"


def _normalized(name: str) -> str:
    """A document font name reduced to a family key: `Arial-BoldMT` -> `arial`,
    `TimesNewRomanPSMT` -> `timesnewroman`."""
    base = "".join(ch for ch in name.lower() if ch.isalpha())
    for style in (
        "bolditalic", "boldoblique", "bold", "italic", "oblique", "light",
        "regular", "roman", "book", "medium", "black", "heavy", "semibold",
    ):
        base = base.replace(style, "")
    for suffix in ("psmt", "ps", "mt", "pro", "std", "lt"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


@lru_cache(maxsize=None)
def _font_file(family: str, bold: bool, italic: bool, size: int):
    """First loadable face for a family (or a class) at this style and size."""
    index = (1 if bold else 0) + (2 if italic else 0)
    families = _CLASS_FAMILIES.get(family, (family,))
    for name in families:
        files = _FAMILY_FILES.get(name)
        if not files:
            continue
        # The exact style first, then the plain face of the same family: a
        # missing italic is better served by its own family upright than by
        # another family slanted.
        for candidate in (files[index], files[0]):
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return None


@lru_cache(maxsize=None)
def _font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default(size)


def _luminance(color) -> float:
    r, g, b = color[:3]
    return 0.299 * r + 0.587 * g + 0.114 * b

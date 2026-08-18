"""OCR adapter seam + the shared pixel-geometry toolkit.

PaddleOCR is the OCR engine (`ocr_paddle.py`); this module owns `Box`, the
line-banding and word-box normalization every paddle-shaped result goes
through, and the `get_ocr_page` seam resolving a backend name to an
`image -> OcrPage` callable.

OCR supplies GEOMETRY, not detection: layer 0 (`vlm.py`) reads the page image
and names the values, and each value is then located in the OCR text so it
can be painted with exact word boxes. The perception objects live in
`ocr_page.py`; character offsets are born in `linearization.py` and never
here — re-deriving an offset from word lengths is the silent-leak class found
in the presidio-image-redactor review (DONE.md).

Line structure is preserved (words joined by spaces, lines by newlines)
rather than flat-joining the page: statement rows only make sense as lines,
and `_rows` banding is what keeps a label and its value on one of them.

**A rotated line is a line, on its own axis.** Page-edge stripes are printed at
90 degrees to the body text; `reading_extent` / `cross_extent` / `_oriented_box`
below are the one place that knows what "along the line" and "across the line"
mean for such a line, so every consumer measures a gap, grows a paint run or
buckets a text word on the line's own axes instead of assuming x. Rotation is
decided by GEOMETRY (`is_rotated`) and the direction by recognition (the
adapter); see core/ARCHITECTURE.md.

Retired backends live in git history: tesseract (2026-07-17), surya
(2026-07-17), and the PP-StructureV3 / PP-DocLayoutV3 layout backends
(2026-08-09, with the rest of the segmenter). Records in DONE.md and
reports/.
"""

import re
from collections import Counter
from typing import NamedTuple

from PIL import Image

# The engine seam: every backend is an `(image, lang=...) -> OcrPage`
# callable. The entries select a PaddleOCR model tier; bare "paddle" is
# DEFAULT_TIER (v6_medium, the round-1 fidelity winner). Lines come from the
# same pinned PP-OCR tier either way, so the choice moves recognition
# quality, never the shape of the result.
OCR_PAGE_BACKENDS = ("paddle", "paddle:v5_server", "paddle:v6_medium")


def get_ocr_page(backend: str = "paddle"):
    """Resolve a backend name to an `(image, lang=...) -> OcrPage` callable.

    In-process, always, on either paddle wheel. The GPU wheel used to be
    routed through a worker subprocess because paddle-GPU and torch cannot
    share a Windows process and the pipeline held torch — but no part of the
    strip path imports torch since Presidio and spaCy went (2026-08-09), so
    the subprocess had nothing left to isolate and was retired with them.
    `ocr_paddle._engine` still refuses to run if torch IS present, which is
    what turns a future re-introduction into an error instead of a crash.

    Imports are deferred so the engine loads only when used."""
    family, _, selector = backend.partition(":")
    if family != "paddle":
        raise ValueError(f"unknown OCR page backend: {backend!r}")
    from functools import partial

    from pii.core.ocr_paddle import DEFAULT_TIER, MODEL_TIERS, ocr_page_paddle

    tier = selector or DEFAULT_TIER
    if tier not in MODEL_TIERS:
        raise ValueError(f"unknown paddle model tier: {selector!r}")
    return partial(ocr_page_paddle, tier=tier)


class Box(NamedTuple):
    """Axis-aligned pixel rectangle in original-image coordinates."""

    left: int
    top: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


def _union(boxes: list[Box]) -> Box:
    left = min(b.left for b in boxes)
    top = min(b.top for b in boxes)
    return Box(
        left=left,
        top=top,
        width=max(b.right for b in boxes) - left,
        height=max(b.bottom for b in boxes) - top,
    )


# --- Rotated lines: the axes of a line printed at 90 degrees to the page ---

# `rotation` is always DEGREES COUNTER-CLOCKWISE THE TEXT IS ROTATED FROM
# UPRIGHT, so rotating a crop of it back by that much reads it. Only the two
# quarter turns occur in print: 90 is the left margin reading bottom-to-top,
# 270 the right margin reading top-to-bottom (both measured in the reference
# corpus). 180 is not handled — nothing prints upside down.
ROTATIONS = (90, 270)

# A detection region at least this many times taller than it is wide is a
# rotated LINE, not an upright one. Geometry decides this and recognition never
# does: the same rule then holds for a scan with no text layer, and for a page
# whose stripe reads badly. Measured over the 1879 detection regions of the
# 27-page reference corpus (2026-08-18), the two populations do not touch — the
# tallest upright region is 1.5:1 (a lone glyph, and paddle's own crop-rotation
# threshold), the shortest rotated one 10.5:1. The gate sits at the recall-first
# end of that empty gap: no upright text region is twice as tall as it is wide.
ROTATED_MIN_RATIO = 2.0


def is_rotated(box: Box) -> bool:
    """Whether a detection region's shape says it holds a rotated line."""
    return box.height >= ROTATED_MIN_RATIO * max(box.width, 1)


def reading_extent(box: Box, rotation: int = 0) -> tuple[int, int]:
    """`box`'s extent ALONG the line, oriented so it grows in reading order.

    Upright it is (left, right). Rotated it is the vertical extent, negated for
    a bottom-to-top line so that "later in the line" is always the larger
    number and every consumer can compare, subtract and take midpoints without
    a direction branch of its own. `_oriented_box` maps the pair back."""
    if rotation == 90:  # reads bottom-to-top
        return -box.bottom, -box.top
    if rotation == 270:  # reads top-to-bottom
        return box.top, box.bottom
    return box.left, box.right


def cross_extent(box: Box, rotation: int = 0) -> tuple[int, int]:
    """`box`'s extent ACROSS the line — its thickness — oriented so it grows
    AWAY from the tops of the glyphs.

    Upright that is (top, bottom): downward, away from the ascenders. Rotated
    the glyph tops point sideways — to the left of a bottom-to-top line, to the
    right of a top-to-bottom one — so the axis is x, negated for the latter.
    Keeping the orientation lets "above the line" stay one comparison in
    `pii.core.layout` whatever the line's rotation."""
    if rotation == 90:
        return box.left, box.right
    if rotation == 270:
        return -box.right, -box.left
    return box.top, box.bottom


def _oriented_box(
    rotation: int, start: int, end: int, near: int, far: int
) -> Box:
    """The inverse of `reading_extent`/`cross_extent`: the box spanning
    `start..end` along a line and `near..far` across it."""
    if rotation == 90:
        return Box(left=near, top=-end, width=far - near, height=end - start)
    if rotation == 270:
        return Box(left=-far, top=start, width=far - near, height=end - start)
    return Box(left=start, top=near, width=end - start, height=far - near)


# --- Engine-neutral normalization helpers (moved here from ocr_paddle.py
# 2026-07-17 when the Surya adapter arrived: every line-oriented engine
# needs the same line->word machinery). ---


def _to_box(quad) -> Box:
    """Axis-aligned Box from either [x1, y1, x2, y2] or a 4-point poly."""
    flat = [list(p) for p in quad] if hasattr(quad[0], "__len__") else None
    if flat:
        xs = [int(p[0]) for p in flat]
        ys = [int(p[1]) for p in flat]
    else:
        xs = [int(quad[0]), int(quad[2])]
        ys = [int(quad[1]), int(quad[3])]
    left, top = min(xs), min(ys)
    return Box(
        left=left,
        top=top,
        width=max(max(xs) - left, 1),
        height=max(max(ys) - top, 1),
    )


def _interpolate(text: str, box: Box, rotation: int = 0):
    """Fallback word boxes: split the line box proportionally by char
    position. Approximate for proportional fonts; the paint layer's
    per-line box union and growth margin absorb the error.

    The split runs along the line's own reading axis and in its own direction,
    so the first word of a bottom-to-top stripe lands at the BOTTOM of it.
    Splitting by x regardless would hand every word of a rotated line the whole
    stripe."""
    start, stop = reading_extent(box, rotation)
    near, far = cross_extent(box, rotation)
    scale = (stop - start) / max(len(text), 1)
    out = []
    for m in re.finditer(r"\S+", text):
        at = start + round(m.start() * scale)
        to = max(start + round(m.end() * scale), at + 1)
        out.append((m.group(), _oriented_box(rotation, at, to, near, far)))
    return out


def _rows(regions):
    """Band regions into visual rows by y-center; one assembled line per
    row, words ordered left-to-right across the row's regions.

    A region joins the current row only if it also does NOT horizontally
    overlap a region already in it: two regions sharing an x-column are
    vertically STACKED lines (a label/value block), not one row. Without this
    a tall neighbour between two stacked lines — a logo — bridges them by
    y-center and their words interleave (the BPAY block, issue #6). Side-by-
    side columns (different x, same y) don't overlap, so multi-column
    statement rows are unaffected.

    **A ROTATED region is never banded — with anything, in either direction.**
    A page-edge stripe is 275-865 px tall, so a y-center band around it reaches
    a third of the page and swallows every horizontal line it crosses: on the
    reference statement the enquiries phone and the stripe assembled as one
    line `13 13 14 XPRCAP0022-2309300323` inside one 1488x275 rectangle, which
    is a paint box, a label neighbourhood and a `contiguous` answer all at once
    (2026-08-18). Each rotated region becomes its own line, and two of them are
    not joined to each other either: joining stripes is speculative, and a
    wrong join is a context error with nothing to gain.

    Rotated rows are banded APART and merged back by y-center, rather than
    skipped in the one pass, so a stripe crossing the middle of the page cannot
    split a horizontal row in two by landing between its regions.

    Words are ordered left-to-right across an upright row; a rotated row keeps
    the order the recognizer read it in, which for a bottom-to-top line runs up
    the page and a sort by `left` would reverse."""
    regions = sorted(regions, key=lambda r: r[0].top + r[0].height / 2)
    rows = []
    origins: list[float] = []
    centers: list[float] = []
    heights: list[float] = []
    row_boxes: list[list[Box]] = []
    rotated = []
    for box, words, *rest in regions:
        c = box.top + box.height / 2
        if rest and rest[0]:
            rotated.append((c, list(words)))
            continue
        if (
            rows
            and abs(c - centers[-1]) < 0.5 * max(box.height, heights[-1], 1)
            and not any(_x_overlap(box, rb) for rb in row_boxes[-1])
        ):
            rows[-1].extend(words)
            centers[-1] += (c - centers[-1]) / 2
            heights[-1] = max(heights[-1], float(box.height))
            row_boxes[-1].append(box)
        else:
            rows.append(list(words))
            origins.append(c)
            centers.append(c)
            heights.append(float(box.height))
            row_boxes.append([box])
    for row in rows:
        row.sort(key=lambda item: item[1].left)
    # Ordered by where each row STARTED, not by its running center: the upright
    # rows are created in ascending order and a stable sort on the origin keeps
    # them exactly there, whatever the banding averaged them to.
    merged = list(zip(origins, rows)) + rotated
    merged.sort(key=lambda item: item[0])
    return [row for _, row in merged]


def _x_overlap(a: Box, b: Box) -> bool:
    """True if two region boxes share enough horizontal extent to be
    vertically stacked lines rather than side-by-side columns."""
    return min(a.right, b.right) - max(a.left, b.left) > 0.3 * min(
        a.width, b.width
    )


def _background_color(image: Image.Image):
    """Most common pixel along the image border — the page background,
    used as the fill color when painting placeholders (image_mode.py)."""
    counts: Counter = Counter()
    for crop_box in (
        (0, 0, image.width, 1),
        (0, image.height - 1, image.width, image.height),
        (0, 0, 1, image.height),
        (image.width - 1, 0, image.width, image.height),
    ):
        crop = image.crop(crop_box)
        for count, color in crop.getcolors(maxcolors=crop.width * crop.height):
            counts[color] += count
    return counts.most_common(1)[0][0]

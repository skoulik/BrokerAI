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
    """Resolve a backend name to an `(image, lang=...) -> OcrPage` callable,
    worker vs in-process by paddle wheel (see ocr_paddle's DLL rules).
    Imports are deferred so the engine loads only when used."""
    family, _, selector = backend.partition(":")
    if family != "paddle":
        raise ValueError(f"unknown OCR page backend: {backend!r}")
    from pii.core.ocr_paddle import DEFAULT_TIER, MODEL_TIERS, _gpu_wheel

    tier = selector or DEFAULT_TIER
    if tier not in MODEL_TIERS:
        raise ValueError(f"unknown paddle model tier: {selector!r}")
    if _gpu_wheel():
        from pii.core.ocr_worker import worker_page

        return lambda image, lang="eng": worker_page(tier, image)
    from functools import partial

    from pii.core.ocr_paddle import ocr_page_paddle

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


def _interpolate(text: str, box: Box):
    """Fallback word boxes: split the line box proportionally by char
    position. Approximate for proportional fonts; the paint layer's
    per-line box union and growth margin absorb the error."""
    scale = box.width / max(len(text), 1)
    out = []
    for m in re.finditer(r"\S+", text):
        left = box.left + round(m.start() * scale)
        right = box.left + round(m.end() * scale)
        out.append(
            (m.group(),
             Box(left, box.top, max(right - left, 1), box.height))
        )
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
    statement rows are unaffected."""
    regions = sorted(regions, key=lambda r: r[0].top + r[0].height / 2)
    rows = []
    centers: list[float] = []
    heights: list[float] = []
    row_boxes: list[list[Box]] = []
    for box, words in regions:
        c = box.top + box.height / 2
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
            centers.append(c)
            heights.append(float(box.height))
            row_boxes.append([box])
    for row in rows:
        row.sort(key=lambda item: item[1].left)
    return rows


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

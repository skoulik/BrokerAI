"""OCR adapter: image -> assembled text + word bounding boxes.

Engine-neutral interchange: whatever the engine, a page becomes a list of
words carrying (text, bbox, conf, line) and an OcrResult whose assembled
text records each word's character interval AT ASSEMBLY TIME. Mapping a
detected PII span back to pixel boxes is then pure interval intersection —
never re-derived from word lengths, which is the silent-leak class found in
the presidio-image-redactor review (DONE.md).

Assembly preserves line structure (words joined by spaces, lines by
newlines) rather than flat-joining the whole page: the GLiNER2 recognizer
runs per-line passes, and statement rows only make sense as lines.

PaddleOCR is the OCR engine (`ocr_paddle.py`); this module owns the
neutral interchange (`Box`/`OcrWord`/`OcrResult`/`assemble`) that every
backend normalizes into, plus the `get_ocr` seam. Tesseract was the first
backend and was retired 2026-07-17 after the fidelity bake-off (records in
DONE.md); its operational profile survives there as history.
"""

import re
from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple

from PIL import Image

# The engine seam: every backend is an image -> OcrResult callable
# normalizing into the interchange below (ARCHITECTURE.md). The paddle
# entries select a model tier ("paddle" = the default tier). Retired
# backends live in git history: tesseract (2026-07-17), surya
# (2026-07-17, one revert away — see reports/ round-2 bake-off).
OCR_BACKENDS = ("paddle", "paddle:v5_server", "paddle:v6_medium")


def get_ocr(backend: str = "paddle"):
    """Resolve a backend name to an `(image, lang=...) -> OcrResult`
    callable. Imports are deferred so unused engines cost nothing."""
    if backend.split(":", 1)[0] == "paddle":
        from pii.core.ocr_paddle import DEFAULT_TIER, MODEL_TIERS, make_paddle_ocr

        tier = backend.partition(":")[2] or DEFAULT_TIER
        if tier not in MODEL_TIERS:
            raise ValueError(f"unknown paddle model tier: {tier!r}")
        return make_paddle_ocr(tier)
    raise ValueError(f"unknown OCR backend: {backend!r}")


# Backends producing the OcrPage perception (get_ocr_page): "ppstructure"
# (PP-StructureV3, typed blocks + reading order) and the paddle line-only
# tiers (synthetic per-line blocks).
OCR_PAGE_BACKENDS = (
    "ppstructure", "paddle", "paddle:v5_server", "paddle:v6_medium",
)


def get_ocr_page(backend: str = "ppstructure"):
    """Resolve a backend to an `(image, lang=...) -> OcrPage` callable, worker
    vs in-process by wheel (mirrors get_ocr). "ppstructure" -> PP-StructureV3
    (typed blocks + reading order); the "paddle" family -> line-only
    perception (one synthetic block per line). Imports are deferred so the
    engine loads only when used."""
    family = backend.split(":", 1)[0]
    if family not in ("paddle", "ppstructure"):
        raise ValueError(f"unknown OCR page backend: {backend!r}")
    from pii.core.ocr_paddle import DEFAULT_TIER, MODEL_TIERS, _gpu_wheel

    tier = backend.partition(":")[2] or DEFAULT_TIER
    if family == "paddle" and tier not in MODEL_TIERS:
        raise ValueError(f"unknown paddle model tier: {tier!r}")
    if _gpu_wheel():
        from pii.core.ocr_worker import worker_page

        spec = "structure" if family == "ppstructure" else f"page:{tier}"
        return lambda image, lang="eng": worker_page(spec, image)
    if family == "ppstructure":
        from pii.core.ocr_ppstructure import ppstructure_page

        return lambda image, lang="eng": ppstructure_page(image)
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


@dataclass(frozen=True)
class OcrWord:
    text: str
    box: Box
    conf: float  # engine confidence, 0-100
    line: int  # index into the assembled text's lines
    char_start: int  # interval in OcrResult.text, recorded at assembly
    char_end: int
    # Paddle's detection line/region box the word came from. It CONTAINS
    # the glyph ink, whereas `box` (a per-word fragment box) is inset from
    # the glyphs by several px at each outer edge — so painting grows the
    # word box out to this (see painted_boxes_for_span). Defaults to `box`
    # for callers that supply no region geometry (glyph-tight assumption).
    region_box: Box | None = None


@dataclass(frozen=True)
class OcrResult:
    text: str
    words: list[OcrWord]

    def boxes_for_span(self, start: int, end: int) -> list[Box]:
        """Pixel boxes covering a character span of `text`.

        Interval intersection (`max(start, w.start) < min(end, w.end)`):
        a word partially covered by the span — entity boundary mid-word at
        either end — still yields its box, recall-first. Word boxes on the
        same line are unioned into one rectangle so the inter-word gaps of
        a multi-word entity don't survive as readable pixels.
        """
        by_line: dict[int, list[Box]] = {}
        for w in self.words:
            if max(start, w.char_start) < min(end, w.char_end):
                by_line.setdefault(w.line, []).append(w.box)
        return [_union(boxes) for _, boxes in sorted(by_line.items())]

    def painted_boxes_for_span(self, start: int, end: int) -> list[Box]:
        """Boxes for painting a span — like boxes_for_span, but each line's
        run is grown out to paddle's line/region box so no glyph fringe
        survives.

        The word boxes returned by boxes_for_span come from paddle's
        per-word fragment boxes, which are inset from the glyph ink by
        several px at each outer edge (the line/region box, `region_box`,
        contains the ink — measured 2026-07-21). A small fixed paint margin
        can't cover that, so for each line the run touches we take the union
        of the run words' region boxes and then pull the outer edges back to
        the MIDPOINT of the gap toward any neighbouring word not in the span
        — recovering the run's own inset without overpainting a kept
        neighbour. Never narrower than boxes_for_span (both midpoint and
        region edge lie outside the word-union edge). Falls back to the word
        box where no region geometry was supplied (region_box is None)."""
        by_line: dict[int, list[OcrWord]] = {}
        for w in self.words:
            if max(start, w.char_start) < min(end, w.char_end):
                by_line.setdefault(w.line, []).append(w)
        out = []
        for line_idx, run in sorted(by_line.items()):
            u_left = min(w.box.left for w in run)
            u_right = max(w.box.right for w in run)
            regions = [w.region_box or w.box for w in run]
            # Union the region box with the word extent. A region box is
            # meant to CONTAIN its words, but paddle occasionally emits one
            # that doesn't (measured on a footer line where the run's words
            # sit past the region's right edge — "ServletRetrieve (6).pdf").
            # Taking min/max against u_left/u_right keeps the documented
            # invariant "region edge lies outside the word-union edge", so a
            # stale region box can never pull an edge PAST the words and the
            # neighbour clamps below can't produce right < left (negative
            # width -> Image.new ValueError).
            left = min(min(r.left for r in regions), u_left)
            right = max(max(r.right for r in regions), u_right)
            top = min(r.top for r in regions)
            bottom = max(r.bottom for r in regions)
            # Clamp the region extension back toward any same-line word not
            # part of the run, so we grow into whitespace, not a neighbour.
            for w in self.words:
                if w.line != line_idx or max(start, w.char_start) < min(
                    end, w.char_end
                ):
                    continue
                if w.box.right <= u_left:
                    left = max(left, (w.box.right + u_left) // 2)
                elif w.box.left >= u_right:
                    right = min(right, (w.box.left + u_right) // 2)
            out.append(Box(left=left, top=top, width=right - left, height=bottom - top))
        return out


def assemble(lines: list[list[tuple]]) -> OcrResult:
    """Build the assembled text from per-line word tuples, recording each
    word's character interval as it is written.

    A word tuple is (text, box, conf) or (text, box, conf, region_box);
    when the region box is omitted it defaults to the word box (glyph-tight
    assumption — see OcrWord.region_box)."""
    words = []
    parts = []
    pos = 0
    for line_idx, line in enumerate(lines):
        if line_idx:
            parts.append("\n")
            pos += 1
        for word_idx, item in enumerate(line):
            text, box, conf = item[0], item[1], item[2]
            region_box = item[3] if len(item) > 3 else box
            if word_idx:
                parts.append(" ")
                pos += 1
            words.append(
                OcrWord(
                    text=text,
                    box=box,
                    conf=conf,
                    line=line_idx,
                    char_start=pos,
                    char_end=pos + len(text),
                    region_box=region_box,
                )
            )
            parts.append(text)
            pos += len(text)
    return OcrResult(text="".join(parts), words=words)


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

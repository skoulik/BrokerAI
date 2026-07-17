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


def assemble(lines: list[list[tuple[str, Box, float]]]) -> OcrResult:
    """Build the assembled text from per-line (text, box, conf) words,
    recording each word's character interval as it is written."""
    words = []
    parts = []
    pos = 0
    for line_idx, line in enumerate(lines):
        if line_idx:
            parts.append("\n")
            pos += 1
        for word_idx, (text, box, conf) in enumerate(line):
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
    row, words ordered left-to-right across the row's regions."""
    regions = sorted(regions, key=lambda r: r[0].top + r[0].height / 2)
    rows = []
    centers: list[float] = []
    heights: list[float] = []
    for box, words in regions:
        c = box.top + box.height / 2
        if rows and abs(c - centers[-1]) < 0.5 * max(
            box.height, heights[-1], 1
        ):
            rows[-1].extend(words)
            centers[-1] += (c - centers[-1]) / 2
            heights[-1] = max(heights[-1], float(box.height))
        else:
            rows.append(list(words))
            centers.append(c)
            heights.append(float(box.height))
    for row in rows:
        row.sort(key=lambda item: item[1].left)
    return rows


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

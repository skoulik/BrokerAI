"""OCR adapter: image -> assembled text + word bounding boxes.

Engine-neutral interchange: whatever the engine (Tesseract today; Paddle/VLM
candidates later normalize into the same shape), a page becomes a list of
words carrying (text, bbox, conf, line) and an OcrResult whose assembled
text records each word's character interval AT ASSEMBLY TIME. Mapping a
detected PII span back to pixel boxes is then pure interval intersection —
never re-derived from word lengths, which is the silent-leak class found in
the presidio-image-redactor review (DONE.md).

Assembly preserves line structure (words joined by spaces, lines by
newlines) rather than flat-joining the whole page: the GLiNER2 recognizer
runs per-line passes, and statement rows only make sense as lines.

Tesseract specifics kept out of the neutral layer:
- `image_to_data` DICT rows with conf == -1 are structural (page/block/
  para/line), not words; empty/whitespace text rows are artifacts. Both
  are dropped BEFORE assembly.
- Tesseract misreads text flush against image edges, so the image is
  padded with a border of the background color before OCR and the pad is
  subtracted from the returned boxes (tightly-cropped statement
  screenshots are a primary input).
- DPI is irrelevant here (established 2026-07-16, ARCHITECTURE.md
  "Tesseract operational profile"): the padded `Image.new` drops PIL
  metadata and pytesseract's temp-file re-save writes none anyway — and
  the DPI hint is a recognition no-op on the LSTM path. Glyph pixel size
  (x-height) is the only size variable that matters.
- `conf` is word-level, int-truncated by pytesseract's DICT parsing, and
  its LSTM calibration is undocumented — do not threshold on it without
  measured numbers (the ocr-report sweep records conf-vs-error data).
"""

import shutil
from collections import Counter
from dataclasses import dataclass
from typing import NamedTuple

from PIL import Image

# Fallback when tesseract isn't on PATH (the winget install location).
_TESSERACT_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# The engine seam: every backend is an image -> OcrResult callable
# normalizing into the interchange below (ARCHITECTURE.md). The paddle
# entries select a model tier ("paddle" = the default tier).
OCR_BACKENDS = ("tesseract", "paddle", "paddle:v5_server", "paddle:v6_medium")


def get_ocr(backend: str = "tesseract"):
    """Resolve a backend name to an `(image, lang=...) -> OcrResult`
    callable. Imports are deferred so unused engines cost nothing."""
    if backend == "tesseract":
        return ocr_image
    if backend.split(":", 1)[0] == "paddle":
        from functools import partial

        from pii.core.ocr_paddle import (
            DEFAULT_TIER,
            MODEL_TIERS,
            ocr_image_paddle,
        )

        tier = backend.partition(":")[2] or DEFAULT_TIER
        if tier not in MODEL_TIERS:
            raise ValueError(f"unknown paddle model tier: {tier!r}")
        return partial(ocr_image_paddle, tier=tier)
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


def ocr_image(
    image: Image.Image,
    lang: str = "eng",
    edge_pad: int = 25,
    config: str = "",
) -> OcrResult:
    """OCR a PIL image with Tesseract into an OcrResult.

    The (padded) image here feeds OCR only; returned boxes are in the
    ORIGINAL image's coordinates, so painting happens on original pixels.
    """
    import pytesseract

    _ensure_tesseract(pytesseract)
    if edge_pad:
        padded = Image.new(
            image.mode,
            (image.width + 2 * edge_pad, image.height + 2 * edge_pad),
            _background_color(image),
        )
        padded.paste(image, (edge_pad, edge_pad))
    else:
        padded = image
    data = pytesseract.image_to_data(
        padded, lang=lang, config=config, output_type=pytesseract.Output.DICT
    )
    return assemble(_lines_from_tesseract(data, offset=edge_pad))


def _lines_from_tesseract(
    data: dict, offset: int = 0
) -> list[list[tuple[str, Box, float]]]:
    """Group `image_to_data` DICT rows into lines of (text, box, conf),
    dropping structural rows (conf == -1) and empty-text artifacts.
    `offset` is subtracted from coordinates (edge padding), clamped >= 0.
    """
    lines: list[list[tuple[str, Box, float]]] = []
    current_key = None
    for i, text in enumerate(data["text"]):
        conf = float(data["conf"][i])
        if conf < 0 or not text.strip():
            continue
        box = Box(
            left=max(data["left"][i] - offset, 0),
            top=max(data["top"][i] - offset, 0),
            width=data["width"][i],
            height=data["height"][i],
        )
        key = (
            data["page_num"][i],
            data["block_num"][i],
            data["par_num"][i],
            data["line_num"][i],
        )
        if key != current_key:
            lines.append([])
            current_key = key
        lines[-1].append((text, box, conf))
    return lines


def _union(boxes: list[Box]) -> Box:
    left = min(b.left for b in boxes)
    top = min(b.top for b in boxes)
    return Box(
        left=left,
        top=top,
        width=max(b.right for b in boxes) - left,
        height=max(b.bottom for b in boxes) - top,
    )


def _background_color(image: Image.Image):
    """Most common pixel along the image border — the pad must blend with
    the page background (white border on a dark screenshot would add
    edges for Tesseract to misread)."""
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


def _ensure_tesseract(pytesseract) -> None:
    if shutil.which("tesseract") is None:
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_DEFAULT

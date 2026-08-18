"""Linearization: OcrPage -> RecognizerInput.

The recognizer runs on ONE flat string. This layer produces that string
from an OcrPage, plus a SOURCE MAP recording, per emitted character range,
the OCR geometry it came from — so a detected span maps back to pixel boxes
by interval intersection, never re-derived from lengths (the silent-leak
class from the presidio-image-redactor review).

Character offsets are born HERE — not on the perception objects. An offset
is a property of the (page, assembly) pair, not of a line, which is why the
source map and not `OcrLine` carries it.

There is one assembly: every line of the page in page order, words joined
by spaces, lines by newlines, so the recognizer sees the whole page at once.
The per-block feed (`linearize_blocks` / `rebase`) was retired 2026-08-09
together with the layout backends that produced the blocks; record in
DONE.md.
"""

from dataclasses import dataclass

from pii.core.ocr import Box, _union
from pii.core.ocr_page import FontSpec, OcrPage


@dataclass(frozen=True)
class PlacedWord:
    """A word placed at a character interval in the linearized text, with the
    geometry needed to map a span back to pixels. `region_box` is the
    detection-line box the run grows out to when painting (always set here —
    resolved from the word's region, glyph-tight box otherwise).

    `font` and `source` are carried straight off the `OcrWord` (see
    `pii.core.ocr_page`): the face a placeholder is drawn in, and what the
    reading is owed to. Both are render/diagnostic only."""

    text: str
    box: Box
    region_box: Box
    line: int
    char_start: int
    char_end: int
    font: FontSpec | None = None
    source: str = "ocr"


@dataclass(frozen=True)
class RecognizerInput:
    """The recognizer's view of a page: the assembled `text` plus the source
    map (`words`) that turns a character span back into pixel boxes. Holds
    the recognized plaintext INCLUDING the PII — a local-only artifact like
    the pseudonym map."""

    text: str
    words: tuple[PlacedWord, ...]

    def boxes_for_span(self, start: int, end: int) -> list[Box]:
        """Pixel boxes covering a character span of `text`.

        Interval intersection (`max(start, w.start) < min(end, w.end)`): a
        word partially covered by the span — an entity boundary mid-word at
        either end — still yields its box, recall-first. Word boxes on the
        same line are unioned into one rectangle so the inter-word gaps of a
        multi-word entity don't survive as readable pixels."""
        by_line: dict[int, list[Box]] = {}
        for w in self.words:
            if max(start, w.char_start) < min(end, w.char_end):
                by_line.setdefault(w.line, []).append(w.box)
        return [_union(boxes) for _, boxes in sorted(by_line.items())]

    def font_for_span(self, start: int, end: int) -> FontSpec | None:
        """The face a placeholder covering this span should be drawn in: the
        one carried by most of the CHARACTERS the span covers.

        By characters rather than by words so a span that reaches one word of a
        heading and six of the body takes the body's face. One face per span,
        not per painted box: a span crossing lines in two different faces is
        rare, and each box re-fits its own size anyway. None whenever the page
        has no font traceback at all, which is every non-PDF input — the
        painter then falls back to its own default."""
        weights: dict[FontSpec, int] = {}
        for w in self.words:
            covered = min(end, w.char_end) - max(start, w.char_start)
            if covered > 0 and w.font is not None:
                weights[w.font] = weights.get(w.font, 0) + covered
        if not weights:
            return None
        return max(weights.items(), key=lambda item: item[1])[0]

    def painted_boxes_for_span(self, start: int, end: int) -> list[Box]:
        """Boxes for painting a span — like boxes_for_span, but each line's
        run is grown out to the detection-line box so no glyph fringe
        survives.

        Engine word boxes are inset from the glyph ink (the region box
        contains the ink). A small fixed paint margin can't cover that, so
        for each line the run touches we take the union of the run words'
        region boxes and then pull the outer edges back to the MIDPOINT of
        the gap toward any neighbouring word not in the span — recovering the
        run's own inset without overpainting a kept neighbour. Never narrower
        than boxes_for_span. The region box is unioned with the word extent
        so a stale region that stops short of its words can't invert the box
        (negative width -> Image.new ValueError)."""
        by_line: dict[int, list[PlacedWord]] = {}
        for w in self.words:
            if max(start, w.char_start) < min(end, w.char_end):
                by_line.setdefault(w.line, []).append(w)
        out = []
        for line_idx, run in sorted(by_line.items()):
            u_left = min(w.box.left for w in run)
            u_right = max(w.box.right for w in run)
            regions = [w.region_box for w in run]
            left = min(min(r.left for r in regions), u_left)
            right = max(max(r.right for r in regions), u_right)
            top = min(r.top for r in regions)
            bottom = max(r.bottom for r in regions)
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


def linearize(page: OcrPage) -> RecognizerInput:
    """Assemble a whole OcrPage into one RecognizerInput.

    Lines in page order; words joined by spaces, lines by newlines. Each
    word's character interval is recorded into the source map AS IT IS
    WRITTEN, never re-derived from lengths — that re-derivation is the
    silent-leak class the source map exists to prevent."""
    words = []
    parts = []
    pos = 0
    for line_idx, line in enumerate(page.lines):
        if line_idx:
            parts.append("\n")
            pos += 1
        for word_idx, w in enumerate(line.words):
            if word_idx:
                parts.append(" ")
                pos += 1
            words.append(
                PlacedWord(
                    text=w.text,
                    box=w.box,
                    region_box=w.region,
                    line=line_idx,
                    char_start=pos,
                    char_end=pos + len(w.text),
                    font=w.font,
                    source=w.source,
                )
            )
            parts.append(w.text)
            pos += len(w.text)
    return RecognizerInput(text="".join(parts), words=tuple(words))

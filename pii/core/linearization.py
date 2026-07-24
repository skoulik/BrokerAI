"""Linearization: OcrPage -> RecognizerInput.

The recognizer runs on ONE flat string. This layer produces that string
from an OcrPage, plus a SOURCE MAP recording, per emitted character range,
the OCR geometry it came from — so a detected span maps back to pixel boxes
by interval intersection, never re-derived from lengths (the silent-leak
class from the presidio-image-redactor review).

Character offsets are born HERE, per linearization — not on the perception
objects. Multiple trial linearizations of one page each get their own
source map over the same geometry; an offset is a property of the
(page, assembly) pair, not of a line.

v1 (`linearize`) reproduces the historical assembly exactly: lines in page
order, words joined by spaces, lines by newlines. Smarter trial
linearizations (reading-order variants, per-block passes, column merges)
grow behind this seam without touching perception or painting.
"""

from dataclasses import dataclass

from pii.core.ocr import Box, _union
from pii.core.ocr_page import OcrPage


@dataclass(frozen=True)
class PlacedWord:
    """A word placed at a character interval in the linearized text, with the
    geometry needed to map a span back to pixels. `region_box` is the
    detection-line box the run grows out to when painting (always set here —
    resolved from the word's region, glyph-tight box otherwise)."""

    text: str
    box: Box
    region_box: Box
    line: int
    char_start: int
    char_end: int


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
    """Assemble an OcrPage into a RecognizerInput (v1: today's assembly).

    Lines in page order; words joined by spaces, lines by newlines; each
    word's character interval recorded into the source map as it is written.
    Byte-identical to the retired `pii.core.ocr.assemble` — only the input
    (an OcrPage instead of raw word-tuple rows) and the offset's home
    (the source map instead of the OCR object) have changed."""
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
                )
            )
            parts.append(w.text)
            pos += len(w.text)
    return RecognizerInput(text="".join(parts), words=tuple(words))

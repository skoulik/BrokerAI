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

Two assemblies live here:

- `linearize` (v1) reproduces the historical assembly exactly: every line
  of the page in page order, words joined by spaces, lines by newlines —
  ONE input, so the recognizer sees the whole page at once.
- `linearize_blocks` cuts the same lines at block boundaries: one
  `RecognizerInput` per block, offsets local to that block. The caller runs
  the recognizer once per block, so a block is FULLY isolated — no pattern,
  no context word and no NER attention window reaches across a block
  boundary. `rebase` folds the per-block inputs (and the spans detected in
  them) back into one page-level view with global offsets, so painting and
  reporting are unchanged by the choice of feed.

Both produce the same characters in the same order (`rebase` of the blocks
is byte-identical to `linearize`); only the unit the recognizer is fed
changes. Smarter trial linearizations (reading-order variants, column
merges, overlapping windows) grow behind this seam without touching
perception or painting.
"""

from dataclasses import dataclass, replace

from pii.core.ocr import Box, _union
from pii.core.ocr_page import OcrLine, OcrPage


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
    """The recognizer's view of a page — or of ONE BLOCK of it: the assembled
    `text` plus the source map (`words`) that turns a character span back into
    pixel boxes. Holds the recognized plaintext INCLUDING the PII — a
    local-only artifact like the pseudonym map.

    `block_id` names the block this input covers (`linearize_blocks`), or is
    None for a whole-page assembly. `PlacedWord.line` stays PAGE-global in
    both, so a block input maps spans to boxes exactly as the page input
    would."""

    text: str
    words: tuple[PlacedWord, ...]
    block_id: int | None = None

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


def _assemble(
    numbered: list[tuple[int, OcrLine]], block_id: int | None = None
) -> RecognizerInput:
    """Assemble numbered `(page_line_index, line)` pairs into one input:
    words joined by spaces, lines by newlines, each word's character interval
    recorded into the source map as it is written (never re-derived from
    lengths). Offsets start at 0 — they are local to THIS input."""
    words = []
    parts = []
    pos = 0
    for n, (line_idx, line) in enumerate(numbered):
        if n:
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
    return RecognizerInput(
        text="".join(parts), words=tuple(words), block_id=block_id
    )


def linearize(page: OcrPage) -> RecognizerInput:
    """Assemble a whole OcrPage into one RecognizerInput (v1: today's
    assembly).

    Lines in page order; words joined by spaces, lines by newlines.
    Byte-identical to the retired `pii.core.ocr.assemble` — only the input
    (an OcrPage instead of raw word-tuple rows) and the offset's home
    (the source map instead of the OCR object) have changed."""
    return _assemble(list(enumerate(page.lines)))


def linearize_blocks(page: OcrPage) -> tuple[RecognizerInput, ...]:
    """Cut the page into one RecognizerInput per block — the per-block feed.

    Blocks are emitted in the order their FIRST line appears in
    `page.lines` (which a layout backend has already sorted by the block
    reading order), lines within a block in page order — so the characters
    and their order are exactly `linearize`'s, only cut into units. A block
    with no lines contributes nothing.

    Deliberately dumb: every block is a unit, whatever its `kind` or
    `origin`. On a line-only backend that means one unit per line (every
    block is synthetic), and an orphan line under a layout backend is its
    own one-line unit — measure before adding grouping heuristics.

    The caller feeds each input to the recognizer separately, which is what
    makes the isolation total: layer-1 patterns cannot match across a block
    boundary, Presidio's context enhancer cannot promote across one, and
    GLiNER2 never shares an attention window across one. The cost is
    per-block analyzer calls and the loss of any cross-block context
    (a label in one block promoting a value in the next)."""
    groups: dict[int, list[tuple[int, OcrLine]]] = {}
    for line_idx, line in enumerate(page.lines):
        groups.setdefault(line.block_id, []).append((line_idx, line))
    return tuple(
        _assemble(numbered, block_id=block_id)
        for block_id, numbered in groups.items()
    )


def rebase(
    inputs: tuple[RecognizerInput, ...] | list[RecognizerInput],
) -> tuple[RecognizerInput, tuple[int, ...]]:
    """Concatenate per-block inputs into one page-level input, and report
    each part's character offset in it.

    Returns `(page_input, offsets)`: parts joined by a newline — the same
    separator `_assemble` puts between lines — so the result is byte-
    identical to `linearize(page)` for the inputs `linearize_blocks(page)`
    produced. Add `offsets[i]` to a span detected in `inputs[i]` and it
    addresses the page input; that is how per-block detection results
    become one page-wide plan with document-order offsets, leaving painting
    and reporting indifferent to which feed produced them."""
    words: list[PlacedWord] = []
    parts: list[str] = []
    offsets: list[int] = []
    pos = 0
    for i, ri in enumerate(inputs):
        if i:
            parts.append("\n")
            pos += 1
        offsets.append(pos)
        words.extend(
            replace(w, char_start=w.char_start + pos, char_end=w.char_end + pos)
            for w in ri.words
        )
        parts.append(ri.text)
        pos += len(ri.text)
    return RecognizerInput(text="".join(parts), words=tuple(words)), tuple(offsets)

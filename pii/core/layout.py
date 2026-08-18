"""Geometric label attachment: which words are NEAR a candidate on the page.

The engine knows only `Layout` (a protocol over character offsets); this module
is the implementation that has pixels. It exists because the retiring
`WindowLayout` — 60 characters back in the assembled string — models nothing
about the page: on one statement it reached across a line break to a label that
already had an owner, and across a *column* to a word `_rows` had interleaved
onto the same assembled line (2026-08-14, record in DONE.md).

**The neighbourhood IS the context string** (Sergei's formulation): collect the
words that sit near the candidate, concatenate them in reading order, and hand
that to the engine as the text to search for labels. Two bands, kept separate
rather than flattened into one:

- `left`  — the last `word_floor` words of the candidate's own line. A word
  count rather than a distance, because the dominant statement layout puts a
  label at the left margin and its value right-aligned across the line, and no
  distance that reaches such a label excludes the neighbouring column (measured
  — see the note on the constants below).
- `above` — the detection REGIONS overhead, within `V_ABOVE` line heights,
  limited to those whose x-range overlaps the candidate's. Regions rather than
  individual words, so a two-word label reaches a one-word value beneath it
  instead of contributing only whichever word happens to be directly overhead.

They must stay separate because reading order flattens the page. On

    unrelated text1     ABN:
    unrelated text2     12345

one flat concatenation reads `unrelated text1 · ABN: · unrelated text2 · 12345`,
so the text between the label and the value would appear to be `unrelated
text2` and a strict attachment would fail — even though nothing at all sits
between them. As two bands, the `above` band holds only `ABN:`, because
`unrelated text1` does not overlap the value's column.

**Distances are measured in line heights, not pixels**, so the same constants
hold at any DPI and any font size; the height comes from the candidate's own
line, so a value in small print gets a proportionally small neighbourhood.
"""

from __future__ import annotations

from pii.core.engine import Context, ContextWord
from pii.core.linearization import PlacedWord, RecognizerInput

# How far above a candidate a label may sit, in line heights. Two rather than
# one so a label that WRAPS ("Australian Financial / Services Licence") still
# assembles whole in the band.
V_ABOVE = 2.0
# How far outside the candidate's x-range a REGION may sit and still count as
# overhead. Half a line height absorbs column jitter without admitting the
# neighbouring column.
X_TOLERANCE = 0.5

# The widest gap that may sit INSIDE one value, in line heights. A printed
# space measures about 0.8 of one; the column jump that made `From 1 January
# 2022 to 30 June 2022` and an enquiries phone into the account number
# `2022 133 174` measured 34. Three is generous enough for a wide fixed-column
# field and nowhere near a column boundary, which is why the constant is not
# delicate.
MAX_INTERNAL_GAP = 3.0

# There is deliberately no horizontal distance limit: the left band is the last
# `word_floor` words on the line and nothing else. Measured on the specimen page
# (300 dpi, 34 px lines, 2396 px wide), a distance limit cannot separate the
# cases anyway — the label `Statement Enquiries` sits 462 px from its value and
# the false promoter `cheque` sits 748 px from its own, so any threshold that
# keeps the first admits nearly all of the second. The word count separates
# them cleanly instead: `cheque` is nine words back, and no rule's floor reaches
# that far. Residual, accepted and untested-for because it needs a specific
# layout: a two-column line whose LEFT column ends in a label word, beside a
# right-column value with fewer than `word_floor` words before it.


class PageLayout:
    """`Layout` over an OCR'd page (`RecognizerInput`)."""

    def __init__(self, source: RecognizerInput) -> None:
        self.source = source
        self._by_line: dict[int, list[PlacedWord]] = {}
        # OCR detection REGIONS — the runs of words the engine found as one
        # printed fragment. They are the unit overhead: if any part of a region
        # is above the candidate, the whole run is one label candidate, which
        # is what lets a two-word label reach a one-word value under it.
        self._regions: dict[tuple[int, tuple], list[PlacedWord]] = {}
        for word in source.words:
            self._by_line.setdefault(word.line, []).append(word)
            self._regions.setdefault((word.line, tuple(word.region_box)), []).append(word)
        for line in self._by_line.values():
            line.sort(key=lambda w: w.char_start)
        for region in self._regions.values():
            region.sort(key=lambda w: w.char_start)

    def contexts(self, start: int, end: int, word_floor: int) -> list[Context]:
        anchor = self._anchor(start, end)
        if anchor is None:
            # No geometry for this span — it is not from this page's OCR. Say
            # nothing rather than guess: a caller mixing sources gets no
            # attachment, never a wrong one.
            return []
        line, left, right, top, height = anchor
        box = (left, top, right, top + height)
        out = []
        band = self._left_band(line, start, word_floor)
        if band:
            out.append(_assemble("left", band, box, height))
        band = self._above_band(left, right, top, height)
        if band:
            out.append(_assemble("above", band, box, height))
        return out

    def contiguous(self, start: int, end: int) -> bool:
        """Whether a span could be ONE value, or spans a column boundary.

        The separator classes in `recognizers.py` bound how much whitespace may
        sit between the groups of an identifier, and on this path they cannot
        work: `linearize` joins every word with a single space, so a column gap
        and a word space are the same character. The geometry still knows, and
        this is where it is asked.
        """
        words = sorted(
            (w for w in self.source.words if w.char_start < end and start < w.char_end),
            key=lambda w: w.char_start,
        )
        if len(words) < 2:
            return True
        height = max(w.box.height for w in words) or 1
        for a, b in zip(words, words[1:]):
            if a.line != b.line:
                return False
            if b.box.left - (a.box.left + a.box.width) > MAX_INTERNAL_GAP * height:
                return False
        return True

    def _anchor(self, start: int, end: int):
        """The candidate's own line and box — its FIRST line if it wraps, since
        a label introduces a value where the value begins."""
        covering = [
            w for w in self.source.words
            if w.char_start < end and start < w.char_end
        ]
        if not covering:
            return None
        line = min(w.line for w in covering)
        boxes = [w.box for w in covering if w.line == line]
        left = min(b.left for b in boxes)
        right = max(b.left + b.width for b in boxes)
        top = min(b.top for b in boxes)
        height = max(
            max(b.height for b in boxes),
            # A one-line value of small type still sits on a line; use the
            # line's own height when it is taller, so the band scales with the
            # page rather than with a single short glyph run.
            max((w.box.height for w in self._by_line.get(line, ())), default=0),
        )
        return line, left, right, top, height

    def _left_band(self, line, start, word_floor):
        if word_floor <= 0:
            return []
        before = [w for w in self._by_line.get(line, ()) if w.char_end <= start]
        return before[-word_floor:]

    def _above_band(self, left, right, top, height):
        reach = V_ABOVE * height
        slack = X_TOLERANCE * height
        band: list[PlacedWord] = []
        for (_, region_box), words in self._regions.items():
            r_left, r_top, r_width, r_height = region_box
            if r_top + r_height > top:
                continue
            if top - (r_top + r_height) > reach:
                continue
            if r_left + r_width < left - slack or r_left > right + slack:
                continue
            band.extend(words)
        band.sort(key=lambda w: (w.line, w.char_start))
        return band


def _assemble(relation, band: list[PlacedWord], box, height: float) -> Context:
    """Words -> one string in reading order, plus the map back to their spans
    and how far each sits from the candidate.

    Joined with single spaces regardless of what separated them on the page:
    the gap between two words is not evidence, and a run of OCR whitespace
    would only make the strict test harder to satisfy for no reason. The
    distance is kept per word rather than per band, because the engine picks
    the NEAREST label and a band holds several.
    """
    words, parts, at = [], [], 0
    for word in band:
        words.append(
            ContextWord(
                word.text,
                word.char_start,
                word.char_end,
                at,
                _distance(word.box, box, height),
            )
        )
        parts.append(word.text)
        at += len(word.text) + 1
    return Context(relation, " ".join(parts), words=tuple(words))


def _distance(word, box, height: float) -> float:
    """Edge-to-edge distance between a word and the candidate, in line heights.

    Edge to edge rather than centre to centre so the measure means the same
    thing in both directions: for a label on the same line it is the whitespace
    between them, for one overhead it is the leading. Centres would make a long
    label read as far away merely for being long.
    """
    wx0, wy0 = word.left, word.top
    wx1, wy1 = word.left + word.width, word.top + word.height
    bx0, by0, bx1, by1 = box
    dx = max(0, bx0 - wx1, wx0 - bx1)
    dy = max(0, by0 - wy1, wy0 - by1)
    return ((dx * dx + dy * dy) ** 0.5) / height if height else 0.0

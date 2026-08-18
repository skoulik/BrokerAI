"""Geometric label attachment: which words are NEAR a candidate on the page.

The engine knows only `Layout` (a protocol over character offsets); this module
is the implementation that has pixels. It exists because the character window
it replaced — 60 characters back in the assembled string, retired 2026-08-18 —
modelled nothing about the page: on one statement it reached across a line
break to a label that already had an owner, and across a *column* to a word
`_rows` had interleaved onto the same assembled line (2026-08-14, record in
DONE.md).

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

**A rotated line has its own axes**, and both bands are computed on them
(`ocr.reading_extent` / `ocr.cross_extent`): "along the line" is vertical for a
page-edge stripe, and "above" it is sideways. A region of a DIFFERENT rotation
is never in a band — a label and its value are printed the same way up, and
without that rule a full-width footer would sit "above" every stripe it crosses
and lend it a label.
"""

from __future__ import annotations

from pii.core.engine import Context, ContextWord
from pii.core.linearization import PlacedWord, RecognizerInput
from pii.core.ocr import Box, _oriented_box, cross_extent, reading_extent

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
            self._regions.setdefault(
                (word.line, tuple(word.region_box)), []
            ).append(word)
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
        line, rotation, box, height = anchor
        out = []
        band = self._left_band(line, start, word_floor)
        if band:
            out.append(_assemble("left", band, box, height))
        band = self._above_band(rotation, box, height)
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
        height = max(_thickness(w.box, w.rotation) for w in words) or 1
        for a, b in zip(words, words[1:]):
            if a.line != b.line:
                return False
            gap = (
                reading_extent(b.box, b.rotation)[0]
                - reading_extent(a.box, a.rotation)[1]
            )
            if gap > MAX_INTERNAL_GAP * height:
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
        on_line = [w for w in covering if w.line == line]
        rotation = on_line[0].rotation
        extents = [reading_extent(w.box, rotation) for w in on_line]
        at = min(e[0] for e in extents)
        to = max(e[1] for e in extents)
        near = min(cross_extent(w.box, rotation)[0] for w in on_line)
        height = max(
            max(_thickness(w.box, rotation) for w in on_line),
            # A one-line value of small type still sits on a line; use the
            # line's own height when it is taller, so the band scales with the
            # page rather than with a single short glyph run.
            max(
                (_thickness(w.box, rotation) for w in self._by_line.get(line, ())),
                default=0,
            ),
        )
        box = _oriented_box(rotation, at, to, near, near + height)
        return line, rotation, box, height

    def _left_band(self, line, start, word_floor):
        if word_floor <= 0:
            return []
        before = [w for w in self._by_line.get(line, ()) if w.char_end <= start]
        return before[-word_floor:]

    def _above_band(self, rotation: int, box: Box, height):
        """The regions overhead, in the candidate's OWN frame: nearer the tops
        of its glyphs, within `V_ABOVE` line heights, and overlapping the run
        of the line it occupies.

        A region printed at a different rotation is never overhead, whatever
        its geometry says. A label and its value are set the same way up, and a
        page-wide footer crossing a page-edge stripe would otherwise be `above`
        every stripe on the page and lend it a label."""
        reach = V_ABOVE * height
        slack = X_TOLERANCE * height
        at, to = reading_extent(box, rotation)
        near, _ = cross_extent(box, rotation)
        band: list[PlacedWord] = []
        for (_, region_box), words in self._regions.items():
            if words[0].rotation != rotation:
                continue
            region = Box(*region_box)
            r_at, r_to = reading_extent(region, rotation)
            _, r_far = cross_extent(region, rotation)
            if r_far > near or near - r_far > reach:
                continue
            if r_to < at - slack or r_at > to + slack:
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


def _distance(word: Box, box: Box, height: float) -> float:
    """Edge-to-edge distance between a word and the candidate, in line heights.

    Edge to edge rather than centre to centre so the measure means the same
    thing in both directions: for a label on the same line it is the whitespace
    between them, for one overhead it is the leading. Centres would make a long
    label read as far away merely for being long.

    Rotation-free by construction: a distance between two rectangles is the
    same number whichever way their text runs.
    """
    dx = max(0, box.left - word.right, word.left - box.right)
    dy = max(0, box.top - word.bottom, word.top - box.bottom)
    return ((dx * dx + dy * dy) ** 0.5) / height if height else 0.0


def _thickness(box: Box, rotation: int) -> int:
    """A box's extent ACROSS its line — the "line height" of the constants
    above, on the axis the line's own rotation puts it."""
    near, far = cross_extent(box, rotation)
    return far - near

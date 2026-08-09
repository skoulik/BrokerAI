"""OCR perception: an engine-neutral description of a page as the OCR engine
saw it — lines of words, with geometry but NO linearization.

It deliberately carries no character offsets. Where a line lands in an
assembled string is a *linearization* decision, owned by
pii.core.linearization, not a property of perception — baking an offset onto
a line would tie it to one assembly.

The layout hierarchy that used to live here (OcrBlock, typed blocks, reading
order, the line->block linkage) was retired 2026-08-09 along with the layout
backends: layer 0 reads page structure natively, so nothing consumed it. The
line ordering that remains is `_rows` visual banding (pii.core.ocr), which is
what keeps a label and its value on one assembled line. Record in DONE.md.
"""

from dataclasses import dataclass

from pii.core.ocr import Box, _union


@dataclass(frozen=True)
class OcrWord:
    """Word geometry within a line. Rich attributes (font, per-word conf,
    box_source) are deferred — for now a word is recognized text + its pixel
    box, plus `region_box`: the detection-line box the word came from. The
    paint layer grows a run out to `region_box` because engine word boxes
    are inset from the glyph ink. Per-word (not per-line): a visual row can
    aggregate words from several detection regions, each with its own region
    box. None means glyph-tight — `region` then falls back to `box`."""

    text: str
    box: Box
    region_box: Box | None = None

    @property
    def region(self) -> Box:
        return self.region_box if self.region_box is not None else self.box


@dataclass(frozen=True)
class OcrLine:
    """One line of recognized text. `box` is the line's bounding box — see
    `_line_box`: it CONTAINS the glyph ink, so it is the union of the word
    boxes with their region boxes, not the word boxes alone. `conf` is the
    native line confidence (0-100) or None if the engine doesn't score lines.
    `font` is None from any OCR engine (filled only by PDF-traceback,
    diagnostics-only)."""

    text: str
    box: Box
    words: tuple[OcrWord, ...]
    conf: float | None = None
    polygon: tuple | None = None  # reserved (skew); None for now
    font: object | None = None


@dataclass(frozen=True)
class OcrFrame:
    """The page's coordinate/provenance frame. Geometry (every box) is in
    pixels of this raster; `dpi`/`source`/`page` make those pixels
    interpretable and portable (normalized or PDF-point coords derive from
    them). `backend`/`tier` record which engine produced the page."""

    width: int
    height: int
    page: int  # page id — 1-based for PDFs, 1 for a lone image
    dpi: int | None = None
    source: str | None = None
    backend: str | None = None
    tier: str | None = None


@dataclass(frozen=True)
class OcrPage:
    frame: OcrFrame
    lines: tuple[OcrLine, ...]


def _line_box(words: list[OcrWord]) -> Box:
    """A line's bounding box: the union of its words' boxes AND their region
    boxes. Every line goes through here so the same words always produce the
    same rectangle.

    Region boxes, not word boxes alone, because an engine word box is inset
    from the glyph ink while the detection region box contains it — a line box
    built from word boxes alone slices the first and last glyph (measured
    2026-07-27: 50 of 53 lines on a real statement page lost up to 8px of ink
    at 200 dpi). Union rather than the region box alone because paddle
    occasionally emits a region that does NOT contain its own words (the
    ea9e056 footer case); unioning keeps the box from ever ending up narrower
    than the words it holds, the same defence `painted_boxes_for_span` applies
    when growing a paint run.

    A word with no region geometry reports its own box as `region`, so a
    glyph-tight backend is unaffected."""
    return _union([w.box for w in words] + [w.region for w in words])


def build_page(rows, frame: OcrFrame) -> OcrPage:
    """Build an OcrPage from assembled visual rows.

    `rows` is the output of pii.core.ocr._rows: a list of lines, each a list
    of word items (text, box, conf) or (text, box, conf, region_box). Each
    non-empty row becomes one OcrLine, in row order — which is the order
    `pii.core.linearization.linearize` then assembles them in.

    Every row carrying words becomes a line, unconditionally: a dropped line
    is unredacted PII.
    """
    lines = []
    for row in rows:
        if not row:
            continue
        words = tuple(
            OcrWord(
                text=item[0],
                box=item[1],
                region_box=item[3] if len(item) > 3 else None,
            )
            for item in row
        )
        lines.append(
            OcrLine(
                text=" ".join(w.text for w in words),
                box=_line_box(words),
                words=words,
                conf=row[0][2] if len(row[0]) > 2 else None,
            )
        )
    return OcrPage(frame=frame, lines=tuple(lines))

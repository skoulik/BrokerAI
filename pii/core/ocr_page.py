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

# What an `OcrWord.source` may say — the vocabulary lives here, beside the
# field, because `pii.core.text_layer` (which sets it) and
# `pii.core.debug_overlay` (which colours by it) must not disagree about the
# spellings, and neither may be imported from here.
SOURCE_OCR = "ocr"  # nothing corroborated this reading
SOURCE_AGREED = "agreed"  # the document's text layer confirmed it
SOURCE_TEXT = "text"  # the document's text layer replaced it


@dataclass(frozen=True)
class FontSpec:
    """A face, as a document describes it — the fact, not the file.

    No OCR engine supplies this; it is filled by traceback from a PDF's own
    text layer (`pii.core.text_layer`), which also documents why `serif` is
    derived from the font NAME and not from the PDF's own serifed flag.

    `size` is in PIXELS OF THE PAGE RASTER, like every other measurement on
    these objects — converting at extraction is what keeps dpi out of
    `pii.core.paint`, which resolves a spec to an actual face at draw time.

    Render-only: a placeholder is drawn in the face it replaces. A font must
    never reach a detection decision — we deliberately distrust the text layer,
    and its idea of the typeface is the least load-bearing thing it carries.
    """

    name: str = ""
    size: float = 0.0
    bold: bool = False
    italic: bool = False
    mono: bool = False
    serif: bool = False


@dataclass(frozen=True)
class OcrWord:
    """Word geometry within a line: recognized text + its pixel box, plus
    `region_box`, the detection-line box the word came from. The paint layer
    grows a run out to `region_box` because engine word boxes are inset from
    the glyph ink. Per-word (not per-line): a visual row can aggregate words
    from several detection regions, each with its own region box. None means
    glyph-tight — `region` then falls back to `box`.

    `font` and `source` are filled only by PDF text-layer traceback
    (`pii.core.text_layer`) and are `None` / `"ocr"` from any OCR engine.
    `source` says what the word's TEXT is owed to — `ocr` nothing corroborated
    it, `agreed` the text layer confirmed it, `text` the text layer replaced it
    — and is what `pii.core.debug_overlay` colours the perception layer by.
    Per-word rather than per-line because a text layer routinely covers only
    part of a page.

    `rotation` is its line's (see `OcrLine`), copied here because every
    consumer of the source map holds words, not lines.
    """

    text: str
    box: Box
    region_box: Box | None = None
    font: FontSpec | None = None
    source: str = SOURCE_OCR
    rotation: int = 0

    @property
    def region(self) -> Box:
        return self.region_box if self.region_box is not None else self.box


@dataclass(frozen=True)
class OcrLine:
    """One line of recognized text. `box` is the line's bounding box — see
    `_line_box`: it CONTAINS the glyph ink, so it is the union of the word
    boxes with their region boxes, not the word boxes alone. `conf` is the
    native line confidence (0-100) or None if the engine doesn't score lines.
    `font` is the face most of the line's words carry — None from any OCR
    engine, filled only by PDF text-layer traceback
    (`pii.core.text_layer`).

    `rotation` is how the line is PRINTED: degrees counter-clockwise its text
    is turned from upright (`pii.core.ocr.ROTATIONS` — 90 reads bottom-to-top,
    270 top-to-bottom, 0 is ordinary text). A line has ONE rotation because a
    rotated region is never banded with anything else, which is what makes
    "along this line" and "across this line" answerable per line at all."""

    text: str
    box: Box
    words: tuple[OcrWord, ...]
    conf: float | None = None
    polygon: tuple | None = None  # reserved (skew); None for now
    font: FontSpec | None = None
    rotation: int = 0


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
    of word items (text, box, conf), optionally with a region box and then a
    rotation appended. Each non-empty row becomes one OcrLine, in row order —
    which is the order `pii.core.linearization.linearize` then assembles them
    in.

    Every row carrying words becomes a line, unconditionally: a dropped line
    is unredacted PII.

    The line's rotation is its first word's: a rotated region is never banded
    with another region, so every word of a row shares one.
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
                rotation=item[4] if len(item) > 4 else 0,
            )
            for item in row
        )
        lines.append(
            OcrLine(
                text=" ".join(w.text for w in words),
                box=_line_box(words),
                words=words,
                conf=row[0][2] if len(row[0]) > 2 else None,
                rotation=words[0].rotation,
            )
        )
    return OcrPage(frame=frame, lines=tuple(lines))

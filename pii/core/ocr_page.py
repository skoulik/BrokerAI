"""OCR perception hierarchy: an engine-neutral description of a page as the
OCR/layout engine saw it — blocks of lines of words, with geometry but NO
linearization.

It deliberately carries no character offsets. Where a line lands in an
assembled string is a *linearization* decision — one of many possible ways
to concatenate the page — owned by pii.core.linearization, not a property
of perception. Baking an offset onto a line would tie it to one assembly;
we intend to try several (combine lines different ways), so the offset lives
in the linearization's source map instead.

Every OCR backend normalizes into these types. A layout-capable backend
(PP-StructureV3) fills real typed blocks + reading order; a line-only
backend synthesizes one block per line (origin="synthetic"), so a line
ALWAYS has a block and its page is reachable transitively
(page.block_of(line)). block_id is therefore total — never None.
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
    """One line of recognized text. `box` is the line's bounding box (union
    of its word boxes). `block_id` indexes into OcrPage.blocks (total —
    every line has a block). `conf` is the native line confidence (0-100) or
    None if the engine doesn't score lines. `font` is None from any OCR
    engine (filled only by PDF-traceback, diagnostics-only)."""

    text: str
    box: Box
    words: tuple[OcrWord, ...]
    block_id: int
    conf: float | None = None
    polygon: tuple | None = None  # reserved (skew); None for now
    font: object | None = None


@dataclass(frozen=True)
class OcrBlock:
    """A layout region grouping lines. `kind` is the layout type
    (text/title/table/footer/…), "unassigned" for a synthetic block.
    `origin` is "detected" (a layout model found it) or "synthetic" (we
    fabricated it around a line with no detected block). `reading_order` is
    the block's position in the page's reading order. `page_id` mirrors the
    frame's page so a detached block still resolves its coordinate frame."""

    id: int
    kind: str
    origin: str  # "detected" | "synthetic"
    box: Box
    reading_order: int
    page_id: int
    conf: float | None = None
    polygon: tuple | None = None
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
    blocks: tuple[OcrBlock, ...]
    lines: tuple[OcrLine, ...]

    def block_of(self, line: OcrLine) -> OcrBlock:
        """Resolve a line's block (block_id indexes blocks by id==position)."""
        return self.blocks[line.block_id]


def build_page(rows, frame: OcrFrame) -> OcrPage:
    """Build an OcrPage for a line-only backend from assembled visual rows.

    `rows` is the output of pii.core.ocr._rows: a list of lines, each a list
    of word items (text, box, conf) or (text, box, conf, region_box). Each
    non-empty row becomes one OcrLine wrapped in its OWN synthetic block
    (origin="synthetic", kind="unassigned"), reading order = row order — so
    line ordering matches today's assembly exactly and block_id is total.
    """
    rows = [row for row in rows if row]
    lines = []
    blocks = []
    for i, row in enumerate(rows):
        words = tuple(
            OcrWord(
                text=item[0],
                box=item[1],
                region_box=item[3] if len(item) > 3 else None,
            )
            for item in row
        )
        line_box = _union([w.box for w in words])
        lines.append(
            OcrLine(
                text=" ".join(w.text for w in words),
                box=line_box,
                words=words,
                block_id=i,
                conf=row[0][2] if len(row[0]) > 2 else None,
            )
        )
        blocks.append(
            OcrBlock(
                id=i,
                kind="unassigned",
                origin="synthetic",
                box=line_box,
                reading_order=i,
                page_id=frame.page,
            )
        )
    return OcrPage(frame=frame, blocks=tuple(blocks), lines=tuple(lines))

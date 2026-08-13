"""Diagnostics: draw what a strip run saw, layer by layer, onto its own page.

Four independently selectable layers — one per STAGE of the pipeline — each
annotating the SAME pixels the run processed, and each written to ITS OWN file
(`pii strip ... --debug=ocr,layer-0,locate,layer-1` produces four; see
`DebugSpec` for why they are not combined into one):

- ``ocr``      — the perception layer: every word box, and the assembled line
  boxes numbered in assembly order, so `_rows` banding is visible. This is the
  geometry painting can use at all; a value missing from these lines can only
  be redacted from the model's own box.
- ``layer-0``  — what the semantic detector itself produced, and nothing else:
  its class, drawn on its own `bbox_2d`. A run whose model was never asked for
  boxes (`--geometry ocr`) draws NOTHING here, which is the honest picture —
  layer 0 contributed no geometry to it.
- ``locate``   — what `pii.core.locator` then did with each finding: the span
  it resolved to, chipped with the TIER that resolved it (exact / squash /
  fuzzy inside the box / box = the model's own padded geometry, no OCR text
  matched / dup = already covered by a wider finding). A finding nothing could
  place draws nothing here, so a layer-0 box with no `locate` box over it is
  an unredacted detection — the one thing an operator must not miss.
- ``layer-1``  — the merged strip plan: the boxes actually painted, labelled
  with the class after refinement and with where the span came from — `L0` the
  model found it here, `DOC` the document lent it (another page, or another
  occurrence of a value named once), `L1` a pattern/checksum caught what the
  model missed. Values that were detected and then EXEMPTED by the keep list
  are drawn here too, in a muted outline chipped `skipped`: they are not
  painted, and "found, then skipped" appeared on no layer at all until this
  was added — the state an operator is looking at when a value they expected
  redacted is still readable.

The layer-0 / locate split is load-bearing rather than tidiness (Sergei,
2026-08-11): layer 0 is the VLM alone with its rough boxes, and which tier
placed a value is a decision made AFTER it, from the OCR text with that box as
a search constraint. Drawing the two apart is what makes the invariant visible
— the model's box and the word boxes actually painted are two different
rectangles, because a box too unreliable to paint is still reliable enough to
disambiguate.

Why one overlay per RUN rather than a standalone command: every artifact drawn
here is a by-product of a strip that already paid for the model passes (minutes
per page). A separate command would pay twice AND would show its own re-run
rather than the run that produced the output — which is how the OCR-only
`debug ocr` this replaces went stale (retired 2026-08-11).

**The overlay is drawn on the UNREDACTED page.** That is the point — you are
looking at the original text under the boxes — and it makes the artifact
near-PII of the strongest kind, exactly like the pseudonym map: local-only,
never shared.

Model-free and analysis-free: this imports the geometry types and the shared
paint toolkit, nothing that loads a model, so a GUI can render a page without
the strip stack.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image

from pii.core.linearization import RecognizerInput
from pii.core.ocr import Box, _union
from pii.core.paint import Segment, paint_segments

# The selectable layers, in DRAWING order — pipeline order, so the overlay
# stacks the way the run ran: perception underneath, then what the model said,
# then where it was placed, then what was painted on top. `parse_layers`
# returns this order whatever order the operator typed.
DEBUG_LAYERS = ("ocr", "layer-0", "locate", "layer-1")

_WORD_COLOR = (90, 90, 90)  # thin grey — word boxes
_LINE_COLOR = (30, 120, 220)  # blue — assembled line boxes, numbered
_LAYER0_COLOR = (190, 40, 190)  # magenta — the model's own view
_LOCATE_COLOR = (235, 140, 0)  # orange — where the locator put it
_LAYER1_COLOR = (220, 30, 30)  # red — the strip painter's own frame color
_SKIPPED_COLOR = (120, 130, 140)  # slate — detected, then kept by policy

# Placement.kind -> what the locate chip says. There is deliberately no entry
# for `None`: a finding nothing could place has no honest geometry, so it draws
# NOTHING and is read off the layer-0 box left bare underneath.
_TIERS = {
    "exact": "exact",
    "squash": "squash",
    "fuzzy": "fuzzy",
    "box": "box",
    "redundant": "dup",
}


@dataclass(frozen=True)
class DebugSpec:
    """A request for overlays: which layers, and the base path they go to.

    **One artifact per layer** (`path` is a base — the layer name is inserted
    before its extension), not one page carrying all of them. Four layers on
    one dense statement page is unreadable (Sergei, 2026-08-11): the boxes are
    dense, the chips collide, and the layers most worth comparing — the model's
    box against the pixels actually painted — are exactly the ones that overlap.
    Separate files diff cleanly page by page in any viewer, and asking for three
    layers costs three files rather than one unusable one.

    Passed into a strip entry point rather than applied afterwards, because the
    pixels to annotate are the ones the run held — on a PDF they live in the
    page cache and are gone by the time a caller sees the result. Re-rendering
    to annotate would reintroduce the very assumption the cache exists to kill
    (the model's boxes are in the coordinate space of the exact raster it saw).
    """

    layers: tuple[str, ...]
    path: str | Path

    def paths(self) -> list[tuple[str, str]]:
        """`(layer, path)` for every requested layer, in drawing order."""
        base = Path(self.path)
        return [
            (layer, str(base.with_suffix(f".{layer}{base.suffix}")))
            for layer in self.layers
        ]


@dataclass(frozen=True)
class PageDebug:
    """One page's drawable record, assembled from a strip result.

    Everything here is already computed by the run; this only gathers it. `ocr`
    is None on the `--geometry vlm` path, where OCR never ran — the ocr and
    layer-1 layers then have nothing to draw and are silently empty rather than
    an error, because "this regime has no OCR text" is what the overlay should
    show."""

    ocr: RecognizerInput | None = None
    placements: tuple = ()  # pii.core.locator.Placement — layer 0
    spans: tuple = ()  # the merged strip plan — what was painted
    borrowed: tuple = ()  # spans this page owes to the rest of the document
    skipped: tuple = ()  # detected, then exempted by the keep list


def parse_layers(spec: str) -> tuple[str, ...]:
    """Parse a `--debug` value ("ocr,layer-1", "all") into canonical layers.

    Raises ValueError with the valid names — a typo'd layer must not silently
    produce an overlay missing the layer the operator asked for."""
    text = spec.strip()
    if text == "all":
        return DEBUG_LAYERS
    names = [part.strip() for part in text.split(",") if part.strip()]
    if not names:
        raise ValueError(
            f"no debug layers given; choose from {', '.join(DEBUG_LAYERS)} "
            f"(or 'all')"
        )
    unknown = [name for name in names if name not in DEBUG_LAYERS]
    if unknown:
        raise ValueError(
            f"unknown debug layer(s): {', '.join(unknown)}; choose from "
            f"{', '.join(DEBUG_LAYERS)} (or 'all')"
        )
    return tuple(layer for layer in DEBUG_LAYERS if layer in set(names))


def page_debug(result) -> PageDebug:
    """Gather an `ImageStripResult` into the record `draw_layers` draws."""
    return PageDebug(
        ocr=result.ocr,
        placements=tuple(result.placements),
        spans=tuple(result.spans),
        borrowed=tuple(result.borrowed),
        skipped=tuple(getattr(result, "skipped", ())),
    )


def draw_layers(
    image: Image.Image, debug: PageDebug, layers: Sequence[str]
) -> Image.Image:
    """Annotate a copy of `image` with each requested layer. Input untouched."""
    unknown = [layer for layer in layers if layer not in DEBUG_LAYERS]
    if unknown:
        raise ValueError(f"unknown debug layer(s): {', '.join(unknown)}")
    wanted = set(layers)
    out = image.convert("RGB")
    if "ocr" in wanted and debug.ocr is not None:
        out = paint_segments(
            out, _word_segments(debug.ocr), margin=0, style="frame",
            color=_WORD_COLOR, width=1, chip="none",
        )
        out = paint_segments(
            out, _line_segments(debug.ocr), margin=0, style="frame",
            color=_LINE_COLOR, width=2,
        )
    if "layer-0" in wanted:
        out = paint_segments(
            out, _layer0_segments(debug, out.size), margin=0, style="frame",
            color=_LAYER0_COLOR, width=2,
        )
    if "locate" in wanted:
        out = paint_segments(
            out, _locate_segments(debug, out.size), margin=0, style="frame",
            color=_LOCATE_COLOR, width=2,
        )
    if "layer-1" in wanted and debug.ocr is not None:
        # Skipped first, so a painted span drawn over one is what reads on top:
        # the plan is the stronger fact.
        out = paint_segments(
            out, _skipped_segments(debug), margin=0, style="frame",
            color=_SKIPPED_COLOR, width=2,
        )
        out = paint_segments(
            out, _layer1_segments(debug), margin=0, style="frame",
            color=_LAYER1_COLOR, width=3,
        )
    return out


def _word_segments(ocr: RecognizerInput) -> list[Segment]:
    return [Segment("", [w.box]) for w in ocr.words]


def _line_segments(ocr: RecognizerInput) -> list[Segment]:
    """One box per assembled line, labelled with its index.

    Built from the words' boxes AND their region boxes, the same union
    `ocr_page._line_box` uses — an engine word box is inset from the glyph ink,
    so a word-box-only union would draw a line box that visibly slices its own
    first and last glyph."""
    by_line: dict[int, list[Box]] = {}
    for w in ocr.words:
        by_line.setdefault(w.line, []).extend([w.box, w.region_box])
    return [
        Segment(str(index), [_union(boxes)])
        for index, boxes in sorted(by_line.items())
    ]


def _layer0_segments(debug: PageDebug, size: tuple[int, int]) -> list[Segment]:
    """The model's own detections: its class on its own box, and nothing else.

    Strictly what layer 0 produced. A finding it gave no `bbox_2d` for draws
    nothing — under `--geometry ocr` that means this layer is empty, which is
    the truth about that regime rather than a gap: no boxes were ever asked
    for. Substituting the located span's geometry here would put the LOCATOR's
    answer under layer 0's name, which is the confusion this split exists to
    remove."""
    from pii.core.locator import denormalize

    width, height = size
    return [
        Segment(
            p.finding.entity_type,
            [denormalize(p.finding.box, width, height)],
        )
        for p in debug.placements
        if p.finding.box is not None
    ]


def _locate_segments(debug: PageDebug, size: tuple[int, int]) -> list[Segment]:
    """Where each layer-0 finding was placed, and by which tier.

    Geometry per tier, in descending confidence: a resolved span paints the OCR
    word boxes it covers (exact / squash / fuzzy alike — we have exact glyph
    geometry even for words we could not read correctly); tier 3 (`box`) has
    the model's padded box as its only geometry; a `dup` is drawn on the
    model's box, since the span it duplicates belongs to another finding.

    A finding nothing could place is deliberately absent: it HAS no geometry,
    and inventing one would draw a redaction that does not exist. Bare magenta
    with no box over it is what an unredacted detection looks like."""
    from pii.core.locator import denormalize

    width, height = size
    out = []
    for p in debug.placements:
        tier = _TIERS.get(p.kind)
        if tier is None:
            continue
        if p.spans and debug.ocr is not None:
            # One value, possibly several ranges — a wrapped value on a
            # two-column page draws a box per line, which is what makes the
            # column step visible against the layer-0 rectangle underneath.
            boxes = [
                box
                for start, end in p.spans
                for box in debug.ocr.boxes_for_span(start, end)
            ]
        elif p.box is not None:
            boxes = [p.box]
        elif p.finding.box is not None:
            boxes = [denormalize(p.finding.box, width, height)]
        else:
            continue
        out.append(Segment(tier, boxes))
    return out


def _layer1_segments(debug: PageDebug) -> list[Segment]:
    """The merged plan: the boxes that were painted, and where each came from."""
    return [
        Segment(
            f"{span.entity_type} {span_provenance(span, debug)}",
            debug.ocr.painted_boxes_for_span(span.start, span.end),
        )
        for span in debug.spans
    ]


def _skipped_segments(debug: PageDebug) -> list[Segment]:
    """Detected, then exempted by the keep list — NOT painted.

    Drawn with the boxes the span would have been painted with, so the outline
    marks the pixels that stayed readable and the chip names the class that was
    let through."""
    return [
        Segment(
            f"{span.entity_type} skipped",
            debug.ocr.painted_boxes_for_span(span.start, span.end),
        )
        for span in debug.skipped
    ]


def span_provenance(span, debug: PageDebug) -> str:
    """Which source put this span in the plan.

    The plan is a MERGE (`PiiPipeline.merge_detections`), so a span can have
    more than one source and this reports the strongest evidence, not an
    exclusive origin: the model saw it here (`L0`), else the document knew the
    value from elsewhere (`DOC`), else only a layer-1 pattern claimed it
    (`L1`). The last one is the interesting reading — it is what the semantic
    detector missed on this page and a deterministic rule caught."""
    if any(
        _overlaps(span, start, end)
        for p in debug.placements
        for start, end in p.spans
    ):
        return "L0"
    if any(_overlaps(span, b.start, b.end) for b in debug.borrowed):
        return "DOC"
    return "L1"


def _overlaps(span, start: int, end: int) -> bool:
    return span.start < end and start < span.end

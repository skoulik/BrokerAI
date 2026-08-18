"""Diagnostics: draw what a strip run saw, layer by layer, onto its own page.

Four independently selectable layers — one per STAGE of the pipeline — each
annotating the SAME pixels the run processed, and each written to ITS OWN file
(`pii strip ... --debug=ocr,layer-0,locate,layer-1` produces four; see
`DebugSpec` for why they are not combined into one):

- ``ocr``      — the perception layer: every word box, and the assembled line
  boxes numbered in assembly order, so `_rows` banding is visible. This is the
  geometry painting can use at all; a value missing from these lines can only
  be redacted from the model's own box. Word boxes are coloured by where the
  READING came from (`pii.core.text_layer`): grey where OCR is on its own,
  green where the document's text layer confirmed it, magenta where the text
  layer REPLACED it. On a text PDF that makes two things visible at a glance —
  which pixels the text layer vouches for, and which regions (an embedded
  image, a scanned footer) it does not reach, since those are exactly the words
  left grey.
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

Alongside the four overlays, a run also writes a **findings listing**
(`write_findings`, `<base>.findings.json`). It is not a fifth layer — it is
what the layers structurally cannot show. Every artifact above is geometry, so
a finding the model returned with no `bbox_2d` appears on none of them, while
still reaching the plan and, through `grouping`, every other page. Deliberate:
the layer-0 overlay draws the model's box and nothing else (Sergei,
2026-08-13), which keeps the layer honest and leaves the boxless findings to be
read here.

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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PIL import Image

from pii.core.linearization import RecognizerInput
from pii.core.ocr import Box, _union
from pii.core.ocr_page import SOURCE_AGREED, SOURCE_OCR, SOURCE_TEXT
from pii.core.paint import Segment, paint_segments

# The selectable layers, in DRAWING order — pipeline order, so the overlay
# stacks the way the run ran: perception underneath, then what the model said,
# then where it was placed, then what was painted on top. `parse_layers`
# returns this order whatever order the operator typed.
DEBUG_LAYERS = ("ocr", "layer-0", "locate", "layer-1")

# The layers that exist only because layer 0 ran: both are drawn from
# `PageDebug.placements`, which IS a layer-0 finding placed on a page. With no
# semantic detector (`--layer0 off`) there are no placements, so these two
# would render as unannotated copies of the ORIGINAL page — near-PII artifacts
# by the module docstring's own warning, carrying no diagnostic information at
# all. Not the same as the empty `layer-0` overlay under `--geometry ocr`,
# where layer 0 DID run and only its boxes are missing (`locate` is populated
# there, and the emptiness is the truth about that regime).
LAYER0_DEBUG_LAYERS = ("layer-0", "locate")

_WORD_COLOR = (90, 90, 90)  # thin grey — word boxes OCR read unaided
_AGREED_COLOR = (30, 150, 70)  # green — the text layer confirmed the reading
_REPAIRED_COLOR = (190, 40, 190)  # magenta — the text layer replaced it
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
    # Whether the layer-0 findings listing is written at all. False under
    # `--layer0 off`, where it would be an empty file: nothing was detected
    # because nothing was asked. Carried here rather than decided at each call
    # site so `--image` and `--pdf` inherit ONE decision.
    findings: bool = True

    def paths(self) -> list[tuple[str, str]]:
        """`(layer, path)` for every requested layer, in drawing order."""
        base = Path(self.path)
        return [
            (layer, str(base.with_suffix(f".{layer}{base.suffix}")))
            for layer in self.layers
        ]

    def findings_path(self) -> str:
        """Where `write_findings` puts the run's layer-0 listing."""
        return str(Path(self.path).with_suffix(".findings.json"))


def drop_layer0_layers(
    layers: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split requested layers into (drawable, dropped) for a run with no
    layer 0. See LAYER0_DEBUG_LAYERS for why the dropped ones are not simply
    rendered blank."""
    return (
        tuple(name for name in layers if name not in LAYER0_DEBUG_LAYERS),
        tuple(name for name in layers if name in LAYER0_DEBUG_LAYERS),
    )


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
    # The same, where the value came from a LAYER-1 needle rather than a
    # layer-0 one. Apart because the two answer different questions of an
    # operator: "the model read this elsewhere" against "a pattern matched
    # this elsewhere, and scored it below threshold here".
    pattern_borrowed: tuple = ()
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
        pattern_borrowed=tuple(getattr(result, "pattern_borrowed", ())),
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
        # Three passes, one per provenance, because a paint call carries one
        # colour. Repaired words are drawn LAST and thicker: they are the few
        # the page is worth inspecting for.
        for source, color, width in (
            (SOURCE_OCR, _WORD_COLOR, 1),
            (SOURCE_AGREED, _AGREED_COLOR, 1),
            (SOURCE_TEXT, _REPAIRED_COLOR, 2),
        ):
            out = paint_segments(
                out, _word_segments(debug.ocr, source), margin=0,
                style="frame", color=color, width=width, chip="none",
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


def findings_record(debug: PageDebug, page: int = 1) -> dict:
    """One page's layer-0 output as data — including what no layer can draw.

    The overlays are geometry, so a finding the model gave no `bbox_2d` for
    appears on NONE of them: layer-0 draws the model's own box and there is
    none, and its located span belongs to the locator's layer. Such a finding
    is nonetheless real, reaches the plan, and — through
    `grouping` — becomes a needle applied to every page. One was painted over
    a heading's hyphen and given a placeholder of its own before anybody could
    see where it came from (2026-08-13). This listing is where it is visible.

    `box` stays in MODEL space (0-1000), the coordinates the model actually
    answered in, so a `null` here means "the model returned no box" and never
    "the renderer had nothing to draw".
    """
    text = debug.ocr.text if debug.ocr is not None else ""
    return {
        "page": page,
        "findings": [
            {
                "type": p.finding.entity_type,
                "text": p.finding.text,
                "box": list(p.finding.box) if p.finding.box is not None else None,
                "placed": p.kind,
                "spans": [
                    {"start": start, "end": end, "text": text[start:end]}
                    for start, end in p.spans
                ],
            }
            for p in debug.placements
        ],
        # The other half of the locator, and the half with nothing to draw on
        # this page: these spans exist because some OTHER page's finding named
        # the value. `value` is set only where the pieces of one wrapped value
        # must collect one placeholder.
        "borrowed": [
            {
                "type": d.entity_type,
                "start": d.start,
                "end": d.end,
                "text": text[d.start : d.end],
                "value": getattr(d, "full_value", None),
            }
            for d in debug.borrowed
        ],
        # Occurrences recovered because layer 1 detected the same value
        # elsewhere in the document — the printings its per-occurrence context
        # boost scored below threshold here.
        "pattern_borrowed": [
            {
                "type": d.entity_type,
                "start": d.start,
                "end": d.end,
                "text": text[d.start : d.end],
                "value": getattr(d, "full_value", None),
            }
            for d in debug.pattern_borrowed
        ],
    }


def write_findings(
    path: str | Path, records: Sequence[dict], *, layer0: str = "on"
) -> None:
    """Write the run's layer-0 listing, plus a count of what has no geometry.

    Near-PII exactly like the overlays — it carries the values verbatim — so
    it lands beside them and is warned about with them.

    `layer0` names the detector that produced the listing ("vision", "text").
    A listing says what was found but not what was ASKED, and those diverge as
    soon as the two modalities can run independently (the switches in
    [TODO.md](TODO.md)) — a page read by the text pass and a page read by the
    vision pass fail differently, so "found nothing" means different things.
    The front end writes no listing at all under `--layer0 off` rather than an
    empty one (`DebugSpec.findings`); "off" stays representable for a library
    caller that wants the artifact anyway.
    """
    findings = [f for record in records for f in record["findings"]]
    payload = {
        "summary": {
            "layer0": layer0,
            "pages": len(records),
            "findings": len(findings),
            # The two an operator is looking for: named but never boxed, and
            # named but placed nowhere at all (which is unredacted).
            "without_box": sum(1 for f in findings if f["box"] is None),
            "unplaced": sum(1 for f in findings if f["placed"] is None),
        },
        "pages": list(records),
    }
    Path(path).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _word_segments(ocr: RecognizerInput, source: str) -> list[Segment]:
    """Word boxes of one provenance (`pii.core.ocr_page.OcrWord.source`).

    Unlabelled: the overlay outlines every word on the page and a chip on each
    would bury the page under its own labels — the colour carries it."""
    return [Segment("", [w.box]) for w in ocr.words if w.source == source]


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
    value from elsewhere (`DOC`), else a layer-1 pattern matched it elsewhere
    in the document and this printing was found by searching for it (`PAT`),
    else only a layer-1 pattern on THIS page claimed it (`L1`).

    The last two are the interesting readings. `L1` is what the semantic
    detector missed on this page and a deterministic rule caught; `PAT` is
    what the deterministic rule ITSELF missed here and only recovered because
    the same value scored above threshold somewhere else — the per-occurrence
    context boost made visible."""
    if any(
        _overlaps(span, start, end)
        for p in debug.placements
        for start, end in p.spans
    ):
        return "L0"
    if any(_overlaps(span, b.start, b.end) for b in debug.borrowed):
        return "DOC"
    if any(_overlaps(span, b.start, b.end) for b in debug.pattern_borrowed):
        return "PAT"
    return "L1"


def _overlaps(span, start: int, end: int) -> bool:
    return span.start < end and start < span.end

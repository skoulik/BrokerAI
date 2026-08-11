"""Image stripping: detect -> locate in the OCR text -> paint placeholders.

Painting is pseudonymization, not blank redaction: each box is filled
with the page background color and the span's placeholder (PERSON_1) is
drawn into it, so the stripped image stays analyzable by a cloud model
and its answers can be rehydrated. A span crossing lines paints one box
per line, each carrying the placeholder — self-describing over compact.

The drawing toolkit itself (`Segment` / `paint_segments` / fill / frame)
lives in `pii.core.paint`, shared with the OCR-debug overlay; the names used
by callers and the eval harness are re-exported here for backward compat.

Detection is layer 0: a `VlmDetector` reads the page image and names the
values (`strip_from_vlm`); `pii.core.locator` places each one in the OCR text
and layer 1 refines, validates and extends them. The detector is REQUIRED —
running layer 1 alone over OCR text is the patterns-only regime retired
2026-07-15 as unsafe, and the layered path was deleted with GLiNER2 on
2026-08-09.

Reading and painting are separate entry points (`read_page`, then
`strip_from_vlm`) because layer 0's findings are per-page OPINIONS: a
multi-page document reads every page, groups the findings across all of them
(`pii.core.grouping`), and only then redacts each page against that shared
view — otherwise a value named on page 1 and missed on page 4 leaks on page 4.
`strip_rendered_page` composes the two for the single-page case, where the page
is the whole document.

Painting geometry comes from OCR word boxes (the VLM's own
boxes are measured unsafe — see `pii.core.vlm`), so detection never decides
pixels and painting never sees raw analyzer results. The one exception is
the locator's tier-3 residue: values with no OCR text at all (a logo, a
barcode) where the model's padded box is the only geometry in existence.
Those are counted separately on the result, never mixed into the clean count.

The RecognizerInput in the returned ImageStripResult contains the recognized
plaintext INCLUDING the PII — like the pseudonym map, it is a local-only
artifact.
"""

import warnings
from dataclasses import dataclass, field, replace

from PIL import Image
from pii.core.detection import Detection

from pii.core.grouping import Grouping, group_findings
from pii.core.linearization import RecognizerInput, linearize
from pii.core.locator import locate_borrowed, locate_findings
from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box, get_ocr_page
from pii.core.paint import Segment, paint_segments
from pii.core.pipeline import InvalidFinding, PiiPipeline
from pii.core.vlm import (
    DEFAULT_GEOMETRY,
    DEFAULT_PAD,
    GEOMETRIES,
    VlmFinding,
)

# The drawing toolkit moved to pii.core.paint (2026-07-24); re-exported so
# existing imports keep working — tests use `_grow` / `_FRAME_COLOR`, the eval
# harness uses `Segment` / `paint_segments`.
from pii.core.paint import _FRAME_COLOR, _grow  # noqa: F401


@dataclass
class ImageStripResult:
    image: Image.Image  # redacted RGB copy
    # Recognized text + word boxes — near-PII, local-only. A page-level
    # RecognizerInput, or None when the VLM supplied geometry directly and OCR
    # never ran — callers that report span text must handle that.
    ocr: object | None
    spans: list  # applied detections; offsets into ocr.text
    invalid: list[InvalidFinding]
    # What was actually painted. Equals one entry per span on the OCR paths,
    # but it is the ONLY record on the VLM-geometry path, which has no text to
    # take offsets into and therefore no spans.
    segments: list = field(default_factory=list)
    # Layer-0 findings painted from the MODEL's box because no OCR text
    # matched them (locator tier 3) — a distinct confidence class: stochastic
    # geometry, and no text means layer 1 never saw the value, so it carries
    # no checksum and no *_INVALID shadow. Counted, not merely warned about.
    box_geometry: list = field(default_factory=list)
    # Layer-0 findings with neither text nor usable geometry. These are
    # UNREDACTED detections; a count that reaches the caller is the point.
    # ALL of them, including the subset below.
    unlocated: list = field(default_factory=list)
    # The subset of `unlocated` whose value was painted somewhere else on the
    # page — the tier-3 residue, where a first finding was painted from the
    # model's box (no char span, so nothing marks a later twin redundant).
    # Still unplaced, and still counted above; carried apart only so the
    # report does not call a painted value an outright leak. Whether the two
    # are the same printing cannot be decided from here.
    unlocated_painted_elsewhere: list = field(default_factory=list)
    # Spans this page owes to what the DOCUMENT knew rather than to what the
    # model said about this page — occurrences no layer-0 finding here claimed
    # (`locator.locate_borrowed`). On a multi-page run these are the values
    # that used to leak; on a single page they are the repeat occurrences of a
    # value the model named only once.
    borrowed: list = field(default_factory=list)
    # The document-wide groups this page was stripped against. Carried so the
    # front-end can print the vote each class was elected from — the election
    # can KEEP a value some page reported as PII, so it has to be auditable.
    groups: tuple = ()


@dataclass
class PageRead:
    """Sweep 1's record of one page: what layer 0 said, and the OCR text.

    Separating this from painting is what lets a document group its findings
    before any page is redacted — see `pii.core.grouping`. `ocr` is None only
    on the `geometry="vlm"` path, where OCR never runs."""

    findings: list
    ocr: RecognizerInput | None = None


def strip_image(
    image: Image.Image,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    lang: str = "eng",
    ocr_backend: str = "paddle",
    *,
    detector,
    geometry: str = DEFAULT_GEOMETRY,
    pad: int = DEFAULT_PAD,
) -> ImageStripResult:
    """Detect the PII on the page and replace it with painted placeholders.

    `detector` (a `pii.core.vlm.VlmDetector`) is layer 0: the model reads the
    page image and names the values, and layer 1 then refines and extends
    them. `geometry` chooses how those values are placed on the page:
    "hybrid" (production) adds a second model pass for boxes and uses them to
    constrain the search, "ocr" searches the whole page string unconstrained,
    "vlm" paints the model's own boxes and skips OCR altogether. See
    `pii.core.vlm` and `pii.core.locator` for the rationale."""
    ocr_engine = None
    if geometry != "vlm":
        engine = get_ocr_page(ocr_backend)
        ocr_engine = lambda im: engine(im, lang=lang)  # noqa: E731
    return strip_rendered_page(
        image, pipeline, pmap, ocr_engine=ocr_engine, detector=detector,
        geometry=geometry, pad=pad,
    )


def read_page(
    image: Image.Image,
    ocr_engine=None,
    *,
    detector,
    geometry: str = DEFAULT_GEOMETRY,
) -> PageRead:
    """Sweep 1: read one already-rendered page. Detects and OCRs, paints
    nothing.

    Split out of `strip_rendered_page` so a multi-page document can read every
    page before it redacts any of them — a value the model names on page 1 and
    misses on page 4 can only be recovered by a pass that has seen both. The
    geometry dispatch lives here, in one place, so `strip_pdf` resolves the OCR
    engine once per document and both entry points share exactly one decision
    about what runs."""
    if geometry not in GEOMETRIES:
        raise ValueError(f"unknown geometry: {geometry!r}")
    findings = detector.detect(image)
    if geometry == "hybrid":
        # Pass 2: the boxes are a search constraint for the locator, not
        # paint geometry. Kept a separate call from detect() so pass 1
        # stays byte-identical to the measured recall baseline.
        findings = detector.localize(image, findings)
    ocr = None if geometry == "vlm" else linearize(ocr_engine(image))
    return PageRead(findings=findings, ocr=ocr)


def strip_rendered_page(
    image: Image.Image,
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    ocr_engine=None,
    *,
    detector,
    geometry: str = DEFAULT_GEOMETRY,
    pad: int = DEFAULT_PAD,
) -> ImageStripResult:
    """Read and strip one page, its own findings being the whole document.

    The single-page entry point (`strip_image`, and the testbench). A document
    of one page still groups: the vote makes a value the model typed two ways
    on the same page consistent, and the borrowed pass paints every occurrence
    of a value it named only once. `strip_pdf` does not call this — it reads
    all pages first, then groups across them."""
    read = read_page(image, ocr_engine, detector=detector, geometry=geometry)
    return strip_from_vlm(
        image, read.findings, pipeline, pmap, ocr=read.ocr, pad=pad,
        grouping=group_findings([read.findings]),
    )


def strip_from_vlm(
    image: Image.Image,
    findings: list[VlmFinding],
    pipeline: PiiPipeline,
    pmap: PseudonymMap,
    ocr: RecognizerInput | None = None,
    pad: int = DEFAULT_PAD,
    grouping: Grouping | None = None,
) -> ImageStripResult:
    """Strip against layer-0 findings — the VLM detector seam.

    `grouping` is what the whole document knows (`pii.core.grouping`); with
    none given this page is treated as the whole document. It does two things
    here: every finding takes its group's elected class, so a value typed
    PII_NAME on this page and PII_COMPANY on four others is treated the same
    way everywhere, and every constituent of every group is searched against
    this page's OCR text — including values layer 0 never reported here, which
    is what stops a value detected on page 1 from leaking on page 4.

    Geometry comes from one of two places, decided by whether `ocr` was
    supplied:

    - `ocr` given  → `locator.locate_findings` places every value, using any
      box the model gave as a search constraint (see `pii.core.locator` for
      why a box too unreliable to paint is reliable enough to disambiguate).
      Matched values paint through `painted_boxes_for_span` — OCR word boxes
      are exact — and because the OCR text is there, layer 1 runs on it too
      (`merge_detections`): it refines IDENTIFIER_GENERIC into the precise
      checksummed class, restores the `*_INVALID` shadows the VLM cannot
      produce, and adds what the model missed. Values that match nothing fall
      back to the model's own padded box, and values with neither text nor
      geometry warn loudly and are counted, because a detection we cannot
      paint is a detection we cannot redact.
    - `ocr` None   → the model's own `bbox_2d` is painted wholesale and OCR
      never runs, so there is no text for layer 1 to refine against either.
      Measured unsafe (see `pii.core.vlm`): 16% of boxes clip by >20 px,
      stochastically, and the tail includes real account numbers.

    Returns spans only on the OCR path — the VLM-geometry path has no text to
    take offsets into, so `ImageStripResult.ocr` is None there."""
    if grouping is None:
        grouping = group_findings([findings])
    # The elected class replaces the page's own, both directions. Deliberate
    # (Sergei, 2026-08-11): a 10-to-1 majority for PII_COMPANY means it is a
    # company, and one page's slip should not fork the value into two
    # placeholders. The vote is reported so the operator can audit it.
    findings = [
        replace(f, entity_type=grouping.type_for(f.text) or f.entity_type)
        for f in findings
    ]
    if ocr is None:
        return _paint_vlm_boxes(image, findings, pmap, pad, grouping)

    placed = locate_findings(findings, ocr, image.size)
    detected = [
        Detection(
            entity_type=p.finding.entity_type,
            start=p.start,
            end=p.end,
            score=1.0,
        )
        for p in placed.located
    ]
    # What the document knows, applied to this page: every occurrence of every
    # known value, including ones layer 0 said nothing about here. Runs BESIDE
    # the box-guided placement above, never instead of it — that is the only
    # path to the fuzzy tier and to tier-3 geometry.
    borrowed = [
        Detection(entity_type=entity_type, start=start, end=end, score=1.0)
        for start, end, entity_type in locate_borrowed(
            grouping.needles(), ocr.text
        )
    ]
    # Reported apart: the coverage that exists ONLY because other pages were
    # read. Counted against what this page would have redacted ALONE — its own
    # layer-0 placements merged with layer 1 — because a value layer 1 catches
    # by pattern was never going to leak here, and counting it would overstate
    # what the document-wide pass bought. Also put through the strip policy, or
    # a kept merchant name matched from another page would inflate the count
    # without being painted. The extra merge is a regex sweep over a page string
    # against minutes of model time per page.
    alone, _ = pipeline.merge_detections(detected, ocr.text)
    borrowed_only = [
        d
        for d in borrowed
        if not any(d.start < a.end and a.start < d.end for a in alone)
        and pipeline.strips_value(d.entity_type, ocr.text[d.start : d.end])
    ]
    # Tier 3 is subject to the same strip policy as everything else — the
    # prompt carries no institutional carve-outs by design, so the model
    # reports (and boxes) merchant logos, and the kept-ORGANIZATION rule is
    # what leaves them alone.
    box_only = [
        p
        for p in placed.box_only
        if pipeline.strips_value(p.finding.entity_type, p.finding.text)
    ]
    if box_only:
        warnings.warn(
            f"{len(box_only)} VLM finding(s) matched no OCR text and were "
            f"painted from the MODEL's own box — stochastic geometry, and no "
            f"layer-1 refinement or checksum was possible: "
            f"{[p.finding.text for p in box_only]!r}",
            RuntimeWarning,
            stacklevel=2,
        )
    # Split by whether an identical value was painted somewhere on the page.
    # Both are unplaced detections and both stay counted; the distinction is
    # what stops the second group being reported as an outright leak when the
    # pixels were in fact painted (see locator._mark_painted_elsewhere).
    nothing_painted = [
        p for p in placed.unlocated if not p.value_painted_elsewhere
    ]
    painted_elsewhere = [
        p for p in placed.unlocated if p.value_painted_elsewhere
    ]
    if nothing_painted:
        warnings.warn(
            f"{len(nothing_painted)} VLM finding(s) could not be located in "
            f"the OCR text and had no usable box — these are unredacted "
            f"detections: {[p.finding.text for p in nothing_painted]!r}",
            RuntimeWarning,
            stacklevel=2,
        )
    if painted_elsewhere:
        warnings.warn(
            f"{len(painted_elsewhere)} VLM finding(s) could not be placed, but "
            f"an identical value WAS painted elsewhere on the page — whether "
            f"this is the same printing or a second one cannot be decided "
            f"here: {[p.finding.text for p in painted_elsewhere]!r}",
            RuntimeWarning,
            stacklevel=2,
        )
    spans, invalid = pipeline.merge_detections(detected + borrowed, ocr.text)
    # Placeholders for tier 3 are allocated after the plan's, so document
    # order still numbers everything that has offsets; a box-only finding has
    # none to sort by. Consistency is by value, not by order, so a value
    # appearing in both places keeps one placeholder.
    extra = [
        Segment(
            label=pmap.placeholder_for(p.finding.entity_type, p.finding.text),
            boxes=[p.box],
        )
        for p in box_only
    ]
    return _paint_plan(
        image, ocr, spans, invalid, pmap,
        extra=extra,
        box_geometry=[p.finding for p in box_only],
        unlocated=[p.finding for p in placed.unlocated],
        unlocated_painted_elsewhere=[p.finding for p in painted_elsewhere],
        borrowed=borrowed_only,
        groups=grouping.groups,
    )


def _paint_vlm_boxes(image, findings, pmap, pad, grouping) -> ImageStripResult:
    """Paint the model's own boxes. No OCR, so no text and no offsets — and
    no borrowing either: there is no page text to search."""
    width, height = image.size
    segments = []
    for finding in findings:
        if finding.box is None:
            continue
        x1, y1, x2, y2 = finding.box
        left = max(int(x1 / 1000 * width) - pad, 0)
        top = max(int(y1 / 1000 * height) - pad, 0)
        right = min(int(x2 / 1000 * width) + pad, width)
        bottom = min(int(y2 / 1000 * height) + pad, height)
        if right <= left or bottom <= top:
            continue
        segments.append(
            Segment(
                label=pmap.placeholder_for(finding.entity_type, finding.text),
                boxes=[Box(left, top, right - left, bottom - top)],
            )
        )
    return ImageStripResult(
        image=paint_segments(image, segments), ocr=None, spans=[], invalid=[],
        segments=segments, groups=grouping.groups,
    )


def _paint_plan(
    image, ocr, spans, invalid, pmap, extra=(), box_geometry=(), unlocated=(),
    unlocated_painted_elsewhere=(), borrowed=(), groups=(),
) -> ImageStripResult:
    """Allocate a placeholder per span and paint it over the span's pixels.
    Spans arrive in document order, which is the numbering order. `extra`
    carries pre-built segments that have no offsets to sort by (locator tier
    3) and are painted alongside."""
    segments = [
        Segment(
            label=pmap.placeholder_for(r.entity_type, ocr.text[r.start : r.end]),
            boxes=ocr.painted_boxes_for_span(r.start, r.end),
        )
        for r in spans
    ] + list(extra)
    out = paint_segments(image, segments)
    return ImageStripResult(
        image=out, ocr=ocr, spans=spans, invalid=invalid, segments=segments,
        box_geometry=list(box_geometry), unlocated=list(unlocated),
        unlocated_painted_elsewhere=list(unlocated_painted_elsewhere),
        borrowed=list(borrowed), groups=tuple(groups),
    )

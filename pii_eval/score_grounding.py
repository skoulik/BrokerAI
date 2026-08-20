"""Score WHERE the redaction landed, not just whether the value survived.

The survival scorers (`score_image`, `score_pdf`) answer "can the value still be
read out of the output". That is the acceptance question and it stays the gate.
It is blind to a whole class of behaviour: a box that covers the right text by
accident, a box that covers half a value, a box that swallows a paragraph. This
scorer measures the geometry itself against the hand-authored truth boxes in
`truth.json`, which nothing consumed until now.

TWO geometries are scored, separately, because they are different claims:

- **model boxes** — what the VLM said. Under `hybrid` and `combined` these are
  never painted; they are a SEARCH CONSTRAINT that tells `locator.py` where to
  look for the value in the OCR text. So the question is not "is the box tight"
  but "does the truth text fall INSIDE it" — a loose box costs a little search,
  a box in the wrong place misdirects the locator entirely. Containment of the
  truth box by the model box is therefore the primary number here, with IoU
  reported alongside as the tightness signal.
- **painted boxes** — what actually covers pixels, from `ImageStripResult`.
  Here the question IS coverage: truth area left unpainted is PII still legible
  on the page, and it is the direction that matters. Over-paint is reported too
  but is the recoverable direction.

Why both: with one number a failure cannot be attributed. A truth value that is
painted badly might have been boxed badly by the model, or boxed correctly and
then lost by the locator. Two tables separate those.

**On over-paint, an honest limit.** The truth is a list of what should and
should not be stripped, NOT an exhaustive annotation of every string on the
page — `gt_draft.py` deliberately leaves some references unmarked as an open
policy question. So painted area that touches no truth box conflates genuine
over-paint with deliberate truth gaps, and is reported as a magnitude to
adjudicate rather than as an error rate.

Usage:
  python -m pii_eval ground --corpus pii_eval/corpora/real/1 \
      --geometry combined --reasoning-effort xhigh
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

from PIL import Image

from pii.core import PiiPipeline, PseudonymMap
from pii.core.image_mode import strip_image
from pii.core.locator import denormalize
from pii.core.vlm import DEFAULT_EFFORT, DEFAULT_GEOMETRY, Incomplete
from pii_eval.build import CORPUS_KEEP_FILE
from pii_eval.score_image import _squash, build_detector, find_value

# A model box counts as a usable constraint when it contains at least this much
# of the truth box. Not 1.0: the constraint only has to bracket the value well
# enough for the locator's own search to find it, and `locator.py` matches OCR
# words by overlap rather than requiring full containment.
USABLE = 0.5

# Painted coverage below this leaves a legible fragment of the value on the
# page. Deliberately strict — the whole point of the paint is that nothing of
# the value remains.
COVERED = 0.95


def _rect(box) -> tuple[int, int, int, int]:
    """Truth box dict -> (x1, y1, x2, y2)."""
    return (box["left"], box["top"],
            box["left"] + box["width"], box["top"] + box["height"])


def _corners(box) -> tuple[int, int, int, int]:
    """`ocr.Box` (left/top/width/height) -> (x1, y1, x2, y2)."""
    return (box.left, box.top, box.left + box.width, box.top + box.height)


def _area(r) -> int:
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def _intersection(a, b) -> int:
    return (max(0, min(a[2], b[2]) - max(a[0], b[0]))
            * max(0, min(a[3], b[3]) - max(a[1], b[1])))


def _iou(a, b) -> float:
    union = _area(a) + _area(b) - _intersection(a, b)
    return _intersection(a, b) / union if union else 0.0


def _contained(truth, box) -> float:
    """How much of `truth` falls inside `box` (0..1)."""
    return _intersection(truth, box) / _area(truth) if _area(truth) else 0.0


def _covered_by(truth, boxes) -> float:
    """How much of `truth` is covered by the UNION of `boxes`.

    Computed by scanning rows rather than summing intersections, because
    painted boxes overlap each other constantly (adjacent words, a value
    painted by two segments) and summing would report coverage above 1.0.

    Kept for the RECTANGLE comparison; the reported coverage uses ink instead
    (see `_ink`), because these two rectangles do not share a convention.
    """
    if not boxes or not _area(truth):
        return 0.0
    x1, y1, x2, y2 = truth
    hit = 0
    for y in range(y1, y2):
        spans = sorted(
            (max(x1, b[0]), min(x2, b[2]))
            for b in boxes if b[1] <= y < b[3] and b[0] < x2 and b[2] > x1
        )
        edge = x1
        for s, e in spans:
            if e > edge:
                hit += e - max(s, edge)
                edge = max(edge, e)
    return hit / _area(truth)


# Anything darker than this counts as ink. Statements are black on white or on
# pale banding; the threshold only has to separate glyphs from paper and from
# the light fills used for table rows.
INK = 160


def _ink(page, rect) -> list:
    """Dark pixel coordinates inside `rect`, in page coordinates.

    Coverage is measured against INK, not against the truth rectangle, because
    the two rectangles are built to different conventions and comparing them
    directly reports a failure that is not there. Truth boxes come from the PDF
    text layer, so they carry the font's ascender/descender whitespace; the
    boxes we paint come from OCR and are tight to the glyphs. Measured on
    d01.p1, the truth box for a name is 41px tall where the OCR box is 37px and
    sits 3px higher — a ~17% rectangle shortfall over blank paper, uniform
    across every value on the page.

    Ink has no such ambiguity: a dark pixel of the value still showing is a
    legible fragment, and one that is painted over is gone. It also makes the
    number mean the same thing for a barcode, a logo and a line of 7pt text.
    """
    x1, y1, x2, y2 = rect
    x1, y1 = max(0, x1), max(0, y1)
    crop = page.crop((x1, y1, x2, y2)).convert("L")
    width = crop.width
    return [
        (x1 + i % width, y1 + i // width)
        for i, value in enumerate(crop.getdata()) if value < INK
    ]


def _ink_covered(ink, boxes) -> float:
    """Fraction of ink pixels falling inside any of `boxes`.

    1.0 when there is no ink at all: a truth box over blank paper (a barcode
    region whose payload is not rendered, a value the render placed elsewhere)
    has nothing left to read, and calling that a miss would be noise."""
    if not ink:
        return 1.0
    inside = 0
    for x, y in ink:
        for b in boxes:
            if b[0] <= x < b[2] and b[1] <= y < b[3]:
                inside += 1
                break
    return inside / len(ink)


def _near(rect, boxes) -> list:
    """The boxes that could possibly cover `rect` — cheap prefilter."""
    return [b for b in boxes if _intersection(rect, b) > 0]


class _Recorder:
    """Wraps the detector so the scorer sees the model's own findings.

    `ImageStripResult` reports what was painted, not what the model said, and
    the model's boxes are half of what is being measured. A proxy keeps that
    out of the engine: nothing in `pii/` needs to grow an output for the
    benefit of a scorer."""

    def __init__(self, inner):
        self.inner = inner
        self.findings = []

    def detect(self, image):
        result = self.inner.detect(image)
        self.findings = list(result.findings)
        return result

    def localize(self, image, findings):
        result = self.inner.localize(image, findings)
        # Under `hybrid` the boxes arrive here, not from detect().
        self.findings = list(result.findings)
        return result


def score_grounding(corpus: str, threshold: float = 0.4,
                    ocr_backend: str = "paddle",
                    geometry: str = DEFAULT_GEOMETRY,
                    reasoning_effort: str = DEFAULT_EFFORT,
                    limit: int = 0) -> int:
    corpus_path = Path(corpus)
    manifest = json.loads((corpus_path / "manifest.json").read_text("utf-8"))
    truth = json.loads((corpus_path / "truth.json").read_text("utf-8"))
    by_id = {d["id"]: d for d in truth["docs"]}

    detector = build_detector(geometry, reasoning_effort)
    pipeline = PiiPipeline(threshold=threshold, entity_keep=CORPUS_KEEP_FILE)

    model_rows, paint_rows, spurious, overpaint = [], [], [], []
    incomplete = Incomplete()
    scored = 0
    valueless = 0

    for doc in manifest["docs"]:
        if limit and scored >= limit:
            break
        entities = by_id[doc["id"]]["entities"]
        pmap = PseudonymMap()
        for name in doc["pages"]:
            # Truth boxes for THIS page, kept with their value so a model box
            # can be matched to the right entity rather than to any overlap.
            #
            # Valueless entities are skipped, as `score_pdf` skips them: a
            # barcode has boxes but no string, so there is nothing to match a
            # model finding against, and nothing to call a leak until barcode
            # masking exists. Counted rather than silently dropped, and skipped
            # HERE rather than filtered later so the two scorers are looking at
            # the same population and their numbers stay comparable.
            wanted = [
                (e, _rect(b))
                for e in entities
                if e.get("strip_expected") and e.get("value")
                for b in e.get("boxes", []) if b["page"] == name
            ]
            valueless += sum(
                1 for e in entities
                if e.get("strip_expected") and not e.get("value")
                and any(b["page"] == name for b in e.get("boxes", []))
            )
            image = Image.open(corpus_path / "pages" / name)
            image.load()
            # Ink is read from the ORIGINAL page, before anything is painted
            # over it — afterwards the pixels that matter are gone, which is
            # the whole point.
            ink = [_ink(image, box) for _, box in wanted]
            recorder = _Recorder(detector)
            result = strip_image(image, pipeline, pmap,
                                 ocr_backend=ocr_backend, detector=recorder,
                                 geometry=geometry)
            incomplete += result.incomplete
            width, height = image.size
            model = [
                (f, _corners(denormalize(f.box, width, height)))
                for f in recorder.findings if f.box is not None
            ]
            painted = [
                _corners(b) for seg in result.segments for b in seg.boxes
            ]

            claimed = set()
            for (entity, truth_box), glyphs in zip(wanted, ink):
                # Model side: the best-containing box among findings whose
                # VALUE matches this entity, so a box is credited to the value
                # it named rather than to whatever it happens to overlap.
                # Containment is measured over INK for the same reason coverage
                # is — the truth rectangle and the model's box do not share a
                # convention, and the ink is what the locator has to find.
                best, best_c, best_iou = None, 0.0, 0.0
                for i, (finding, box) in enumerate(model):
                    if not _same_value(finding.text, entity["value"]):
                        continue
                    c = _ink_covered(glyphs, [box])
                    if c > best_c:
                        best, best_c, best_iou = i, c, _iou(truth_box, box)
                if best is not None:
                    claimed.add(best)
                model_rows.append({
                    "type": entity["type"], "value": entity["value"],
                    "page": name, "contained": best_c, "iou": best_iou,
                    "boxed": best is not None,
                })
                paint_rows.append({
                    "type": entity["type"], "value": entity["value"],
                    "page": name,
                    "covered": _ink_covered(glyphs, _near(truth_box, painted)),
                    "ink": len(glyphs),
                })

            spurious.extend(
                (name, model[i][0].text)
                for i in range(len(model)) if i not in claimed
            )
            truth_area = sum(_area(b) for _, b in wanted)
            painted_area = sum(_area(b) for b in painted)
            on_truth = sum(
                max((_intersection(b, t) for _, t in wanted), default=0)
                for b in painted
            )
            overpaint.append((name, painted_area, on_truth, truth_area))
            print(f"  scored {name}", file=sys.stderr)
            scored += 1
            if limit and scored >= limit:
                break

    if valueless:
        print(f"  note: {valueless} valueless truth box(es) (barcodes) "
              f"skipped - no string to match a model finding against",
              file=sys.stderr)
    return _summarize(model_rows, paint_rows, spurious, overpaint, incomplete,
                      geometry, reasoning_effort)


def _same_value(found: str, truth_value: str) -> bool:
    """Does a model finding name this truth entity?

    Containment either way: the model routinely returns a longer span than the
    truth value ("MR SERGEI KULIK") or a fragment of it, and for the purpose of
    crediting a BOX either is the same value in the same place."""
    if find_value(truth_value, found):
        return True
    a, b = _squash(found), _squash(truth_value)
    return bool(a) and bool(b) and (a in b or b in a)


def _summarize(model_rows, paint_rows, spurious, overpaint, incomplete,
               geometry, effort) -> int:
    print(f"\ngrounding: geometry={geometry} reasoning-effort={effort}")
    if incomplete:
        # Same reasoning as the survival scorers: a page whose answer never
        # finished measures the token budget, not grounding.
        print(f"  !! {incomplete.truncated} cut-off / {incomplete.malformed} "
              f"unparseable model response(s) — geometry below is affected")

    print("\nMODEL boxes (a search constraint, never painted)")
    print("  contain = share of the value's INK inside the model's box; "
          "IoU is rectangle overlap")
    print(f"{'entity type':<20}{'n':>5}{'boxed':>8}{'usable':>8}"
          f"{'contain':>9}{'IoU':>7}")
    for label, rows in _by_type(model_rows):
        n = len(rows)
        boxed = sum(r["boxed"] for r in rows)
        usable = sum(r["contained"] >= USABLE for r in rows)
        cont = _mean(r["contained"] for r in rows)
        iou = _mean(r["iou"] for r in rows if r["boxed"])
        print(f"{label:<20}{n:>5}{boxed:>8}{usable:>8}{cont:>9.0%}{iou:>7.0%}")
    print(f"  model boxes matching no truth occurrence: {len(spurious)}")

    print("\nPAINTED boxes (what actually covers the page)")
    print(f"  covered = the value's INK fully painted over (>= {COVERED:.0%}); "
          f"mean is the ink share")
    print(f"{'entity type':<20}{'n':>5}{'covered':>9}{'mean':>7}{'partial':>9}")
    for label, rows in _by_type(paint_rows):
        n = len(rows)
        full = sum(r["covered"] >= COVERED for r in rows)
        partial = sum(0 < r["covered"] < COVERED for r in rows)
        print(f"{label:<20}{n:>5}{full:>9}{_mean(r['covered'] for r in rows):>7.0%}"
              f"{partial:>9}")

    # The direction that matters: ink of the value still showing. Measured on
    # ink rather than on the truth rectangle this means what it says — a value
    # at 70% has three tenths of its glyph pixels visible — where the rectangle
    # version reported ~80% for EVERY value on the page and meant nothing but a
    # difference of box convention. Value-survival can miss this entirely,
    # because a surviving fragment need not match the whole value.
    fragments = [r for r in paint_rows if 0 < r["covered"] < COVERED]
    if fragments:
        print(f"\nPARTIALLY PAINTED ({len(fragments)}) — ink still visible:")
        for r in sorted(fragments, key=lambda r: r["covered"])[:25]:
            print(f"  {r['page']}: {r['type']} {r['covered']:.0%} "
                  f"{r['value']!r}")

    painted_area = sum(p for _, p, _, _ in overpaint)
    on_truth = sum(t for _, _, t, _ in overpaint)
    if painted_area:
        print(f"\npainted area on a truth box: {on_truth / painted_area:.0%} "
              f"— the remainder is over-paint PLUS deliberate truth gaps "
              f"(see this module's docstring); a magnitude to adjudicate, not "
              f"an error rate")
    # No acceptance gate here on purpose: the gate is value survival, and this
    # scorer exists to explain it, not to duplicate it.
    return 0


def _by_type(rows):
    groups = defaultdict(list)
    for r in rows:
        groups[r["type"]].append(r)
    for t in sorted(groups):
        yield t, groups[t]
    yield "ALL", rows


def _mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0

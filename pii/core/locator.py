"""Value location: layer-0 findings -> spans in the text they were read from.

This module owns *both* placement paths, because "a named value becomes a
span" is one concept and splitting it across two homes is how the two drift
apart. `locate_findings` is the image path — box-guided, three geometry tiers,
described below. `locate_in_text` is the text path and is far simpler: the
model was handed the very string it is quoting from, so there is nothing to
reconcile, no geometry to resolve, and every occurrence of a value can be
marked mechanically. It lives at the bottom of the file.

The rest of this docstring is about the image path.

The VLM names values; this decides WHERE each one is. Layer 0's box is used
here as a **search constraint**, not as paint geometry, and that distinction
is the whole design:

- Painting tolerance is zero pixels. The measured box distribution is bimodal
  (median excellent, p90 inward clip 63.9 px, a residual one-character shift
  on small print in the two-pass regime), so a box is NOT safe to paint —
  see `core/DONE.md` and reports/2026-08-08-vlm-oneshot-qwen36.md.
- Localization tolerance is about half a word. A box clipped by 60 px, or
  displaced by a character, still overlaps the correct words.

So the same signal that is too unreliable to paint is reliable enough to
disambiguate, and every mis-location failure of the old global search follows
from having had no positional constraint at all: a short identifier
squash-matching inside a monetary amount elsewhere on the page, a repeated
value claiming the wrong occurrence, a nested finding ("John" after "John
Smith") jumping to an unrelated John.

**This is also what makes edit distance admissible.** The retired invariant
("no fuzzier than the alphanumeric squash") was justified by GLOBAL search:
edit distance over a whole page always finds something, somewhere, wrong.
Restricted to the handful of words a box covers, a fuzzy match can only pick
something in the right place. Hence the rule that replaces it: *fuzzy
matching is permitted exactly where a box constrains the candidate set;
unconstrained search stays at exact-or-squash.*

Geometry then resolves in three tiers, in descending order of confidence:

1. **Text matched** (exact or squash) -> paint the OCR word boxes. Exact
   geometry; unchanged from the pre-box path, only better disambiguated.
2. **Text matched only fuzzily**, inside the box -> paint the same way. This
   is the OCR-damage case, and it still gets exact glyph geometry: we have
   word boxes for words we could not read correctly.
3. **Nothing matched** -> the model's own box is the only geometry there is
   (a logo, a barcode, handwriting — content with no OCR text at all). Padded
   generously and reported separately, because it is a different confidence
   class: stochastic geometry, and no OCR text means layer 1 never sees the
   value, so it carries no checksum and no `*_INVALID` shadow.

A finding with no usable box degrades to exactly the old behaviour, so the
change cannot regress any value that located correctly before.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pii.core import fuzzy
from pii.core.linearization import RecognizerInput
from pii.core.ocr import Box, _union
from pii.core.vlm import VlmFinding, squash_map

# Tier-3 padding. The report's 8 px was calibrated for the median box; the
# fallback only ever fires where matching already failed, so it is padded for
# the TAIL instead. Scaled by box height rather than fixed: the residual
# error in the two-pass regime is a one-character displacement, which scales
# with the font, and for digits a character is roughly 0.6x its height.
FALLBACK_PAD_RATIO = 0.6
FALLBACK_PAD_MIN = 8

# Words beyond the box's own span considered for a fuzzy window, to recover
# from a clipped box that missed its first or last word.
_WORD_SLACK = 2

# Above this many covered words a box is not a constraint any more, and fuzzy
# matching inside it would be the page-wide edit-distance search the design
# rules out. Such a box (a whole-page rectangle, say) still ranks candidates
# by overlap; it just stops licensing the fuzzy tier. A real value spans a few
# words, so the cap is far above anything legitimate.
_MAX_BOX_WORDS = 40

# A word is folded into a tier-3 fallback box only if the box really covers
# it; a one-pixel touch must not drag a whole neighbouring word in.
_SNAP_MIN_OVERLAP = 0.2

_KIND_RANK = {"exact": 3, "squash": 2, "fuzzy": 1}


@dataclass(frozen=True)
class Placement:
    """Where one finding landed. `kind` records which tier resolved it:
    exact/squash/fuzzy carry a char span, "box" carries pixel geometry only,
    "redundant" means an already-placed finding covers it, and None means
    nothing did — an unredacted detection."""

    finding: VlmFinding
    kind: str | None
    start: int | None = None
    end: int | None = None
    box: Box | None = None
    overlap: float = 0.0
    distance: float = 0.0


@dataclass
class LocateResult:
    placements: list[Placement] = field(default_factory=list)

    @property
    def located(self) -> list[Placement]:
        """Placements carrying a char span — tiers 1 and 2."""
        return [p for p in self.placements if p.start is not None]

    @property
    def box_only(self) -> list[Placement]:
        """Tier 3: painted from the model's own geometry."""
        return [p for p in self.placements if p.kind == "box"]

    @property
    def unlocated(self) -> list[Placement]:
        """Neither text nor usable geometry — detections we cannot redact."""
        return [p for p in self.placements if p.kind is None]


def denormalize(box, width: int, height: int) -> Box:
    """A model box (x1, y1, x2, y2 normalized to 1000) as pixels."""
    x1, y1, x2, y2 = box
    left = round(x1 / 1000 * width)
    top = round(y1 / 1000 * height)
    return Box(
        left=left,
        top=top,
        width=round(x2 / 1000 * width) - left,
        height=round(y2 / 1000 * height) - top,
    )


def locate_findings(
    findings: list[VlmFinding], ocr: RecognizerInput, size: tuple[int, int]
) -> LocateResult:
    """Place every finding, longest value first.

    The ordering matters for the containment rule: a broad finding must claim
    its span before a narrower one nested inside it is considered, so that
    "John" after "John Smith" is recognized as already covered rather than
    sent hunting for a different John. Placeholder numbering is unaffected —
    it is allocated later, in document order, from the merged plan.
    """
    width, height = size
    taken: list[tuple[int, int]] = []
    placements: list[Placement | None] = [None] * len(findings)
    order = sorted(range(len(findings)), key=lambda i: -len(findings[i].text))
    for i in order:
        finding = findings[i]
        box = _usable_box(finding, width, height)
        placement = _place(finding, box, ocr, taken)
        if placement.start is not None:
            taken.append((placement.start, placement.end))
        placements[i] = placement
    return LocateResult(placements=[p for p in placements if p is not None])


def _usable_box(finding: VlmFinding, width: int, height: int) -> Box | None:
    """The finding's box in pixels, or None if it carries no information.

    A zero-area box must not reach tier 3: padding one produces a small
    rectangle at an arbitrary spot, which would be painted and COUNTED as a
    redaction while covering nothing. Reporting the value as unplaced is the
    honest outcome."""
    if finding.box is None:
        return None
    box = denormalize(finding.box, width, height)
    return box if _area(box) > 0 else None


def _place(
    finding: VlmFinding,
    box: Box | None,
    ocr: RecognizerInput,
    taken: list[tuple[int, int]],
) -> Placement:
    candidates = _candidates(finding.text, box, ocr)

    free, contained = [], []
    for start, end, kind, dist in candidates:
        if any(start < t_end and t_start < end for t_start, t_end in taken):
            if any(t_start <= start and end <= t_end for t_start, t_end in taken):
                contained.append((start, end))
            continue
        free.append((start, end, kind, dist, _overlap(ocr, start, end, box)))

    if free:
        # Positional agreement outranks textual agreement: a value the box
        # points at beats a better-looking string match somewhere else on the
        # page. With no box every overlap is 0 and this falls through to
        # kind-then-position, which is exactly the pre-box behaviour.
        start, end, kind, dist, overlap = max(
            free,
            key=lambda c: (1 if c[4] > 0 else 0, _KIND_RANK[c[2]], c[4], -c[3]),
        )
        return Placement(
            finding=finding,
            kind=kind,
            start=start,
            end=end,
            overlap=overlap,
            distance=dist,
        )

    if contained:
        # Already covered by a wider finding's span — painting it again would
        # be a no-op, and sending it to another occurrence would be wrong.
        return Placement(finding=finding, kind="redundant")

    if box is not None:
        fallback = _fallback_box(box, ocr)
        if fallback.width > 0 and fallback.height > 0:
            return Placement(finding=finding, kind="box", box=fallback)

    return Placement(finding=finding, kind=None)


def _candidates(
    needle: str, box: Box | None, ocr: RecognizerInput
) -> list[tuple[int, int, str, float]]:
    """Every plausible (start, end, kind, distance) for `needle`.

    Exact and squash matches are collected page-wide — that is the floor the
    old locator provided and it must survive a useless box. Fuzzy windows are
    collected only inside `box`.
    """
    found: dict[tuple[int, int], tuple[str, float]] = {}

    def offer(start: int, end: int, kind: str, dist: float) -> None:
        prior = found.get((start, end))
        if prior is None or _KIND_RANK[kind] > _KIND_RANK[prior[0]]:
            found[(start, end)] = (kind, dist)

    at = ocr.text.find(needle)
    while at != -1:
        offer(at, at + len(needle), "exact", 0.0)
        at = ocr.text.find(needle, at + 1)

    hay_sq, index = squash_map(ocr.text)
    need_sq, _ = squash_map(needle)
    if need_sq:
        at = hay_sq.find(need_sq)
        while at != -1:
            offer(index[at], index[at + len(need_sq) - 1] + 1, "squash", 0.0)
            at = hay_sq.find(need_sq, at + 1)

    if box is not None and need_sq:
        for start, end, dist in _fuzzy_windows(need_sq, box, ocr):
            offer(start, end, "fuzzy", dist)

    return sorted(
        (start, end, kind, dist)
        for (start, end), (kind, dist) in found.items()
    )


def _fuzzy_windows(
    need_sq: str, box: Box, ocr: RecognizerInput
) -> list[tuple[int, int, float]]:
    """Word windows inside `box` whose squashed text is within edit budget.

    Windows are contiguous slices of the source map, so a window's character
    span is a genuine contiguous range of the page text (and may cross a line
    break — a wrapped address is one value). The slice is grown by
    `_WORD_SLACK` words each side because a clipped box routinely misses the
    first or last word of the value it marks.
    """
    covered = [
        i for i, w in enumerate(ocr.words) if _intersects(w.box, box)
    ]
    if not covered or len(covered) > _MAX_BOX_WORDS:
        return []
    lo = max(min(covered) - _WORD_SLACK, 0)
    hi = min(max(covered) + _WORD_SLACK, len(ocr.words) - 1)

    out = []
    for a in range(lo, hi + 1):
        for b in range(a, hi + 1):
            start = ocr.words[a].char_start
            end = ocr.words[b].char_end
            window, _ = squash_map(ocr.text[start:end])
            if not window:
                continue
            if len(window) > 2 * len(need_sq):
                break  # windows only grow from here
            if 2 * len(window) < len(need_sq):
                continue
            dist = fuzzy.matches(window, need_sq)
            if dist is not None:
                out.append((start, end, dist))
    return out


def _fallback_box(box: Box, ocr: RecognizerInput) -> Box:
    """Tier-3 geometry: the model box, padded, unioned with any OCR word it
    substantially covers.

    The union is the cheap half of the safety: where the box clips into a
    word, the word's own (exact) box completes it. Over-painting a neighbour
    is recoverable; leaving half an account number legible is not.
    """
    pad = max(FALLBACK_PAD_MIN, round(FALLBACK_PAD_RATIO * box.height))
    padded = Box(
        left=box.left - pad,
        top=box.top - pad,
        width=box.width + 2 * pad,
        height=box.height + 2 * pad,
    )
    snapped = [
        w.box
        for w in ocr.words
        if _area(w.box) > 0
        and _intersection_area(w.box, box) / _area(w.box) >= _SNAP_MIN_OVERLAP
    ]
    merged = _union([padded, *snapped]) if snapped else padded
    left = max(merged.left, 0)
    top = max(merged.top, 0)
    return Box(
        left=left,
        top=top,
        width=merged.right - left,
        height=merged.bottom - top,
    )


def _overlap(
    ocr: RecognizerInput, start: int, end: int, box: Box | None
) -> float:
    """How much of `box` and the candidate's pixels coincide, as a fraction of
    the smaller — tolerant of a box that clips (smaller than the value) and of
    one that bloats (larger), both of which are measured behaviours."""
    if box is None:
        return 0.0
    boxes = ocr.boxes_for_span(start, end)
    if not boxes:
        return 0.0
    candidate_area = sum(_area(b) for b in boxes)
    shared = sum(_intersection_area(b, box) for b in boxes)
    smaller = min(candidate_area, _area(box))
    return shared / smaller if smaller > 0 else 0.0


def _intersects(a: Box, b: Box) -> bool:
    return _intersection_area(a, b) > 0


def _intersection_area(a: Box, b: Box) -> int:
    wide = min(a.right, b.right) - max(a.left, b.left)
    high = min(a.bottom, b.bottom) - max(a.top, b.top)
    return wide * high if wide > 0 and high > 0 else 0


def _area(box: Box) -> int:
    return max(box.width, 0) * max(box.height, 0)


# --------------------------------------------------------------------------
# Text path: findings -> spans, no geometry involved.
# --------------------------------------------------------------------------

# Squash matching collapses separators, so it can match ACROSS word
# boundaries — on a page that is held in check by the box, and here there is
# no box. A short squashed needle would therefore match arbitrary runs of
# neighbouring words, so the fallback tier requires a value with some
# substance. There is deliberately NO floor on exact matching: real 2-char
# surnames (Wu, Ng) and 3-char organizations (NAB, ANZ) exist, and the
# no-floor-on-names decision is recorded in core/ARCHITECTURE.md.
_MIN_SQUASH_CHARS = 4


@dataclass(frozen=True)
class TextPlacement:
    """Where one finding landed in the text. `kind` records which tier
    resolved it — "exact", "squash", or None for a value that is not in the
    text at all. `spans` holds EVERY occurrence, not just the first."""

    finding: VlmFinding
    kind: str | None
    spans: tuple[tuple[int, int], ...] = ()


@dataclass
class TextLocateResult:
    placements: list[TextPlacement] = field(default_factory=list)

    @property
    def located(self) -> list[TextPlacement]:
        return [p for p in self.placements if p.spans]

    @property
    def unlocated(self) -> list[TextPlacement]:
        """Values the model reported that are not in the text. On the image
        path this means OCR could not read them; here the model was given the
        text itself, so it means the value was reformatted or invented. Either
        way it is a detection we cannot act on, and it is surfaced rather than
        dropped."""
        return [p for p in self.placements if not p.spans]


def locate_in_text(findings: list[VlmFinding], text: str) -> TextLocateResult:
    """Place every finding at EVERY occurrence of its value in `text`.

    Marking all occurrences ourselves — rather than trusting the model to
    enumerate them — is the whole reason the text prompt asks for distinct
    values only. Finding a known string in a known string is exact, free and
    complete; asking a model to do it costs output budget and degrades with
    document length.

    Nested and overlapping findings need no special handling here (unlike the
    image path, where each finding must claim its own geometry): "John" inside
    an already-marked "John Smith" simply produces an overlapping span, and
    `PiiPipeline._merge_overlaps` unions them into one replacement. A "John"
    elsewhere in the document is a separate occurrence that SHOULD also be
    marked — which is precisely what the image path's containment rule has to
    suppress, and what text wants.
    """
    squashed: tuple[str, list[int]] | None = None
    placements = []
    for finding in findings:
        spans = _text_occurrences(text, finding.text)
        kind = "exact" if spans else None
        if not spans:
            if squashed is None:
                squashed = squash_map(text)
            spans = _squash_occurrences(squashed, finding.text)
            kind = "squash" if spans else None
        placements.append(
            TextPlacement(finding=finding, kind=kind, spans=tuple(spans))
        )
    return TextLocateResult(placements=placements)


def _text_occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    """Case-insensitive occurrences of `needle`. Matched through `re` rather
    than by lower-casing both sides, so the offsets are always coordinates in
    the ORIGINAL string — some Unicode case mappings change length, which
    would silently shift every span after them."""
    if not needle:
        return []
    return [
        m.span() for m in re.finditer(re.escape(needle), text, re.IGNORECASE)
    ]


def _squash_occurrences(
    squashed: tuple[str, list[int]], needle: str
) -> list[tuple[int, int]]:
    """Occurrences of `needle` ignoring spacing, punctuation and case — the
    fallback for a value the model re-spaced or re-punctuated as it copied."""
    hay_sq, index = squashed
    need_sq, _ = squash_map(needle)
    if len(need_sq) < _MIN_SQUASH_CHARS:
        return []
    out = []
    at = hay_sq.find(need_sq)
    while at != -1:
        out.append((index[at], index[at + len(need_sq) - 1] + 1))
        at = hay_sq.find(need_sq, at + 1)
    return out

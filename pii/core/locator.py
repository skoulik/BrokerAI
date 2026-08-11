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
from dataclasses import dataclass, field, replace
from typing import Sequence

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
    nothing did — an unredacted detection.

    `value_painted_elsewhere` qualifies that last case only: an identical
    value WAS painted somewhere on this page. It does not make the placement
    redacted — the two may be separate printings — it exists so the report can
    say which of the two situations this is."""

    finding: VlmFinding
    kind: str | None
    start: int | None = None
    end: int | None = None
    box: Box | None = None
    overlap: float = 0.0
    distance: float = 0.0
    value_painted_elsewhere: bool = False


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
        """Neither text nor usable geometry — detections we cannot redact.

        ALL of them, including the ones whose value was painted elsewhere:
        being unable to place a detection is the fact that has to stay
        counted. `Placement.value_painted_elsewhere` distinguishes the two
        situations for the report."""
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
    return LocateResult(
        placements=_mark_painted_elsewhere([p for p in placements if p is not None])
    )


def _mark_painted_elsewhere(placements: list[Placement]) -> list[Placement]:
    """Flag each unplaced finding whose value was painted somewhere on the page.

    The tier-3 residue makes this necessary. A finding painted from the model's
    own box has no char span, and containment in a char span is the only thing
    that marks a later finding "redundant" — so a second finding of the same
    value with no box of its own resolves to nothing and would be reported as
    unredacted although the pixels were painted.

    Deliberately a REPORT distinction, not a suppression: two occurrences of a
    value can genuinely sit in two places, and painting one says nothing about
    the other. Containment in a span is positional evidence and may suppress;
    value identity is not, and may only annotate.
    """
    painted = {
        squash_map(p.finding.text)[0]
        for p in placements
        if p.kind is not None
    }
    return [
        replace(p, value_painted_elsewhere=True)
        if p.kind is None and squash_map(p.finding.text)[0] in painted
        else p
        for p in placements
    ]


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


# --------------------------------------------------------------------------
# Borrowed values: what the document knows, applied to one page.
# --------------------------------------------------------------------------

# A borrowed needle is unanchored — it comes from a page other than this one,
# so there is no box to say where on THIS page it should be. Exact matching
# deliberately carries no length floor (real 2-char surnames and 3-char
# organizations exist), which is safe when a box pins the match and unbounded
# when a value is hunted document-wide: 'Wu' would paint inside 'Would'. These
# guards make a match respect word edges in alphanumeric space, applied only
# where the needle's own edge character is alphanumeric (a value that starts
# with '(' or '+' needs no guard there).
_ALNUM_BEFORE = r"(?<![0-9A-Za-z])"
_ALNUM_AFTER = r"(?![0-9A-Za-z])"

# Fuzzy borrowed matching (2026-08-11). A page differs from a known value for
# two reasons that look the same to a matcher: the DOCUMENT truncated it to fit
# a fixed-width field ('SK BUSINESS TRUST' printed as 'SK BUSINESS TRUS' — what
# statements do constantly), or OCR damaged it. Weighted edit distance covers
# both, and truncation is simply deletions at the end.
#
# This does not weaken the rule above it. "Fuzzy is permitted exactly where a
# box constrains the candidate set" was argued for `locate_findings`, where
# placements COMPETE: a needle landing in the wrong place over-paints there and
# leaves the real occurrence unclaimed — a leak plus an over-strip. Borrowed
# needles do not compete: every occurrence is marked independently and nothing
# is consumed, so a spurious match is purely additive over-strip. The needle is
# corroborated too — a value the model already detected and we already located
# elsewhere in this document — rather than a fresh transcription.
#
# THE FLOOR IS THE GUARD, not the budget: at four characters any budget of 1
# matches a large fraction of a page, so short values never reach this tier
# ('sk' would otherwise paint everywhere). The budget is tighter than
# `fuzzy.budget_for` (the box-constrained path) for the same reason.
_BORROWED_FUZZY_MIN_CHARS = 8
_BORROWED_FUZZY_RATIO = 0.2
_BORROWED_FUZZY_CAP = 4.0

# Identifier-shaped needles are capped BELOW 2.0, and the number is derived
# rather than tuned: `fuzzy.identifier_substitution_cost` prices a digit read
# as another digit at infinity, but edit distance simply routes around it with
# a delete plus an insert for 2.0. So a cap of 2.0 or more would still let one
# account number match another that differs by a single digit — the
# prohibition only bites if no budget can pay the detour. What it costs is
# truncations of two characters or more on identifiers specifically; a
# one-character truncation still matches, as do any number of cross-class
# confusions (0.25 each), which are the cases that actually occur on
# identifiers. Names keep the general cap, where truncation is the common case
# and there are no digits to confuse.
_BORROWED_FUZZY_IDENTIFIER_CAP = 1.5


def borrowed_budget(need_sq: str) -> float:
    """Edit cost a borrowed needle of this length may absorb."""
    budget = min(
        _BORROWED_FUZZY_CAP, max(1.0, _BORROWED_FUZZY_RATIO * len(need_sq))
    )
    if fuzzy.identifier_shaped(need_sq):
        return min(budget, _BORROWED_FUZZY_IDENTIFIER_CAP)
    return budget


def locate_borrowed(
    needles: Sequence[tuple[str, str]], ocr: RecognizerInput
) -> list[tuple[int, int, str]]:
    """Every occurrence on this page of a value the DOCUMENT knows about.

    This is what makes a value the model named on page 1 and missed on page 4
    strip on both. It runs beside `locate_findings`, not instead of it: the
    page's own findings still go through the box-guided tiers (the only route
    to tier-3 geometry), and this pass adds every occurrence of every known
    value on top. Overlaps between the two are unioned by
    `PiiPipeline._merge_overlaps` like any other pair of spans.

    Three tiers, in two passes over the needles. Exact and squash run first for
    ALL needles, then fuzzy — so textual certainty always outranks edit
    distance no matter which needle gets there first.

    Fuzzy is **additive, not a fallback**: a page carrying a value's full form
    exactly AND a truncated form would otherwise find the exact one, skip the
    fuzzy tier, and leak the truncation. That is a real specimen, not a
    hypothetical (2026-08-11, `SK BUSINESS TRUS`).

    `needles` is `(value, entity_type)` LONGEST FIRST — two needles can land on
    one span ('John' inside 'John Smith') and the wider one must claim it.
    Returns `(start, end, entity_type)` triples in document order.
    """
    text = ocr.text
    squashed: tuple[str, list[int]] | None = None
    claimed: dict[tuple[int, int], str] = {}
    for value, entity_type in needles:
        spans = _bounded_occurrences(text, value)
        if not spans:
            if squashed is None:
                squashed = squash_map(text)
            spans = _bounded_squash_occurrences(squashed, text, value)
        for span in spans:
            claimed.setdefault(span, entity_type)

    fuzzy_needles = [
        (value, entity_type, squash_map(value)[0])
        for value, entity_type in needles
    ]
    fuzzy_needles = [
        n for n in fuzzy_needles if len(n[2]) >= _BORROWED_FUZZY_MIN_CHARS
    ]
    if fuzzy_needles:
        runs = _word_runs(
            ocr,
            max(len(sq) + borrowed_budget(sq) for _, _, sq in fuzzy_needles),
        )
        # Regions the certain tiers already own. A fuzzy match overlapping one
        # is a worse-evidenced view of text that is already claimed — most
        # often a sub-run of it ('9999 1234' inside '(02) 9999 1234') — and
        # keeping it would only inflate the count of what this pass found.
        taken = list(claimed)
        for _, entity_type, need_sq in fuzzy_needles:
            budget = borrowed_budget(need_sq)
            # A digit read as a letter is damage; a digit read as another digit
            # is a different account, and must not be discounted.
            costs = (
                fuzzy.IDENTIFIER_COSTS
                if fuzzy.identifier_shaped(need_sq)
                else fuzzy.CONFUSION_COSTS
            )
            hits = []
            for length in range(
                max(int(len(need_sq) - budget), 1),
                int(len(need_sq) + budget) + 1,
            ):
                for start, end, run_sq in runs.get(length, ()):
                    distance = fuzzy.distance(
                        run_sq, need_sq, ceiling=budget, costs=costs
                    )
                    if distance <= budget:
                        hits.append((distance, -(end - start), start, end))
            # CLOSEST FIRST, longest to break a tie. Runs are bucketed by
            # length, so scanning them in bucket order would let a worse view
            # of a region claim it before the best one is even tested —
            # 'BUSINESS TRUS' (3 edits) beating 'SK BUSINESS TRUS' (1) purely
            # because it is shorter.
            for _, _, start, end in sorted(hits):
                if any(
                    start < t_end and t_start < end
                    for t_start, t_end in taken
                ):
                    continue
                claimed[(start, end)] = entity_type
                taken.append((start, end))

    return [
        (start, end, entity_type)
        for (start, end), entity_type in sorted(claimed.items())
    ]


def _word_runs(
    ocr: RecognizerInput, max_chars: float
) -> dict[int, list[tuple[int, int, str]]]:
    """Contiguous word runs of the page, bucketed by squashed length.

    Runs rather than character windows so a match always covers whole words —
    which is the word-edge guard the exact and squash tiers apply, obtained
    here by construction. Bucketing by length is what keeps the scan cheap: a
    needle only ever tests runs within its own budget of its length, so a
    10-character account number never reaches a 6-character amount at all.
    """
    buckets: dict[int, list[tuple[int, int, str]]] = {}
    words = ocr.words
    for a in range(len(words)):
        for b in range(a, len(words)):
            start, end = words[a].char_start, words[b].char_end
            run_sq, _ = squash_map(ocr.text[start:end])
            if len(run_sq) > max_chars:
                break  # runs only grow from here
            buckets.setdefault(len(run_sq), []).append((start, end, run_sq))
    return buckets


def _bounded_occurrences(text: str, needle: str) -> list[tuple[int, int]]:
    """`_text_occurrences` with the word-edge guard."""
    if not needle:
        return []
    pattern = re.escape(needle)
    if needle[0].isalnum():
        pattern = _ALNUM_BEFORE + pattern
    if needle[-1].isalnum():
        pattern = pattern + _ALNUM_AFTER
    return [m.span() for m in re.finditer(pattern, text, re.IGNORECASE)]


def _bounded_squash_occurrences(
    squashed: tuple[str, list[int]], text: str, needle: str
) -> list[tuple[int, int]]:
    """`_squash_occurrences` with the word-edge guard.

    The guard is checked on the ORIGINAL text at the mapped-back offsets: a
    squashed match always begins and ends on an alphanumeric character, so an
    alphanumeric neighbour there means the match cut into a longer word."""
    return [
        (start, end)
        for start, end in _squash_occurrences(squashed, needle)
        if not _touches_alnum(text, start, end)
    ]


def _touches_alnum(text: str, start: int, end: int) -> bool:
    before = text[start - 1] if start > 0 else ""
    after = text[end] if end < len(text) else ""
    return before.isalnum() or after.isalnum()

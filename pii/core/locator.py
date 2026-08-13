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

**And a value is not always one range of the page string.** That string is
banded VISUALLY (`ocr_page._rows`), which is what puts a label beside its
value and is load-bearing for context promotion; the price is that two cards
side by side share every band, so a value that WRAPS inside one column has
the other card's row-mate spliced between its halves. Nothing contiguous
reaches across it. So tiers 1 and 2 also search the value's own reading
order, assembled a line at a time from the words the box covers — by the
NEEDLE (`_wrapped_occurrences`), never by scanning those words, which is what
lets a line be offered whole and picked from safely. Hence `Placement.spans`:
one value, several ranges, keyed on the whole of itself downstream
(`Detection.full_value`). `locate_borrowed` runs the same walk with no box to
lean on, constrained by geometry instead — consecutive lines whose pieces
share an x-column.
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

# Words beyond the box's own span considered for a box-local assembly, to
# recover from a clipped box that missed its first or last word.
_WORD_SLACK = 2

# Above this many covered words a box is not a constraint any more, and fuzzy
# matching inside it would be the page-wide edit-distance search the design
# rules out. Such a box (a whole-page rectangle, say) still ranks candidates
# by overlap; it just stops licensing the box-local tiers. A real value spans
# a few words, so the cap is far above anything legitimate.
_MAX_BOX_WORDS = 40

# How much of a word the box must hold for that word to count as covered —
# used both to select the box-local assembly and to fold a word into a tier-3
# fallback box. Lenient on purpose: the measured clip is INWARD, so a box that
# cuts into the first or last glyph of a word must still select it, and it is
# the needle match, not this threshold, that decides where the value is. A
# one-pixel touch must still not drag a whole neighbouring word in.
_BOX_WORD_OVERLAP = 0.2

_KIND_RANK = {"exact": 3, "squash": 2, "fuzzy": 1}


@dataclass(frozen=True)
class Placement:
    """Where one finding landed. `kind` records which tier resolved it:
    exact/squash/fuzzy carry char spans, "box" carries pixel geometry only,
    "redundant" means an already-placed finding covers it, and None means
    nothing did — an unredacted detection.

    `spans` is a TUPLE because one value is not always one range of the page
    string: `_rows` bands a page visually, so on a two-column layout a value
    that wraps inside one column has the other column's row-mate spliced
    between its halves. It is still ONE value — `image_mode` keys its
    pseudonym on the whole of it — so the pieces must travel together rather
    than as two findings.

    `value_painted_elsewhere` qualifies the unplaced case only: an identical
    value WAS painted somewhere on this page. It does not make the placement
    redacted — the two may be separate printings — it exists so the report can
    say which of the two situations this is."""

    finding: VlmFinding
    kind: str | None
    spans: tuple[tuple[int, int], ...] = ()
    box: Box | None = None
    overlap: float = 0.0
    distance: float = 0.0
    value_painted_elsewhere: bool = False


@dataclass
class LocateResult:
    placements: list[Placement] = field(default_factory=list)

    @property
    def located(self) -> list[Placement]:
        """Placements carrying char spans — tiers 1 and 2."""
        return [p for p in self.placements if p.spans]

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
        taken.extend(placement.spans)
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
    for spans, kind, dist in candidates:
        if any(
            start < t_end and t_start < end
            for start, end in spans
            for t_start, t_end in taken
        ):
            if all(
                any(
                    t_start <= start and end <= t_end
                    for t_start, t_end in taken
                )
                for start, end in spans
            ):
                contained.append(spans)
            continue
        free.append((spans, kind, dist, _overlap(ocr, spans, box)))

    if free:
        # Positional agreement outranks textual agreement: a value the box
        # points at beats a better-looking string match somewhere else on the
        # page. With no box every overlap is 0 and this falls through to
        # kind-then-position, which is exactly the pre-box behaviour.
        #
        # Being in the box at all is that agreement; how MUCH of the box a
        # candidate fills is not, and ranking by it ahead of edit distance
        # hands a clipped box to whichever candidate fits inside it — a
        # truncation of the value beating the whole of it. Exact and squash
        # candidates all score distance 0, so this only ever orders the fuzzy
        # tier, where closer text is the better evidence.
        spans, kind, dist, overlap = max(
            free,
            key=lambda c: (1 if c[3] > 0 else 0, _KIND_RANK[c[1]], -c[2], c[3]),
        )
        return Placement(
            finding=finding,
            kind=kind,
            spans=spans,
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
) -> list[tuple[tuple[tuple[int, int], ...], str, float]]:
    """Every plausible (spans, kind, distance) for `needle`.

    Exact and squash matches are collected page-wide — that is the floor the
    old locator provided and it must survive a useless box. Squash and fuzzy
    matches over the box-local assembly are collected only inside `box`.
    """
    found: dict[tuple[tuple[int, int], ...], tuple[str, float]] = {}

    def offer(
        spans: tuple[tuple[int, int], ...], kind: str, dist: float
    ) -> None:
        if not spans:
            return
        prior = found.get(spans)
        if prior is None or _KIND_RANK[kind] > _KIND_RANK[prior[0]]:
            found[spans] = (kind, dist)

    at = ocr.text.find(needle)
    while at != -1:
        offer(((at, at + len(needle)),), "exact", 0.0)
        at = ocr.text.find(needle, at + 1)

    hay_sq, index = squash_map(ocr.text)
    need_sq, _ = squash_map(needle)
    if need_sq:
        at = hay_sq.find(need_sq)
        while at != -1:
            offer(((index[at], index[at + len(need_sq) - 1] + 1),), "squash", 0.0)
            at = hay_sq.find(need_sq, at + 1)

    if box is not None and need_sq:
        allowed = _box_words(box, ocr)
        if allowed is not None:
            for spans in _wrapped_occurrences(ocr, need_sq, allowed):
                offer(spans, "squash", 0.0)
        for spans, dist in _fuzzy_windows(need_sq, box, ocr):
            offer(spans, "fuzzy", dist)

    return sorted(
        (spans, kind, dist) for spans, (kind, dist) in found.items()
    )


@dataclass(frozen=True)
class _LocalWord:
    """One word of a box-local assembly: where it sits in the local string,
    and the page-string range and word index it came from."""

    local_start: int
    local_end: int
    char_start: int
    char_end: int
    index: int


def _fuzzy_windows(
    need_sq: str, box: Box, ocr: RecognizerInput
) -> list[tuple[tuple[tuple[int, int], ...], float]]:
    """Windows of the box-local assembly within `need_sq`'s edit budget.

    The assembly is the words the box covers, read in order and joined into
    ONE line — see `_box_line`. Where nothing is spliced between them (the
    ordinary single-column case) it is character-for-character the page slice
    this used to scan; where the page interleaves two columns it is the value
    without the interloper, so a wrapped value can be recovered fuzzily too.
    """
    assembly = _box_line(box, ocr)
    if assembly is None:
        return []
    text, words = assembly
    out = []
    for a in range(len(words)):
        for b in range(a, len(words)):
            start, end = words[a].local_start, words[b].local_end
            window, _ = squash_map(text[start:end])
            if not window:
                continue
            if len(window) > 2 * len(need_sq):
                break  # windows only grow from here
            if 2 * len(window) < len(need_sq):
                continue
            dist = fuzzy.matches(window, need_sq)
            if dist is not None:
                spans = _to_spans(words, start, end)
                if spans:
                    out.append((spans, dist))
    return out


def _box_line(
    box: Box, ocr: RecognizerInput
) -> tuple[str, tuple[_LocalWord, ...]] | None:
    """The words the box covers, read in order and joined into one line.

    Selection is by how much of a WORD the box holds rather than by any
    overlap at all, so a box that cuts into a glyph still takes the word
    whole. Slack is added at the OUTER ends only, because a clipped box
    routinely misses the first or last word of the value it marks; filling the
    INTERIOR would splice the neighbouring column's row-mate straight back in,
    which is the very text this exists to step over.
    """
    covered = _covered_words(box, ocr)
    if covered is None:
        return None
    chosen = (
        list(range(max(covered[0] - _WORD_SLACK, 0), covered[0]))
        + covered
        + list(
            range(
                covered[-1] + 1,
                min(covered[-1] + _WORD_SLACK, len(ocr.words) - 1) + 1,
            )
        )
    )
    words, parts, pos = [], [], 0
    for n, i in enumerate(chosen):
        if n:
            parts.append(" ")
            pos += 1
        word = ocr.words[i]
        words.append(
            _LocalWord(
                local_start=pos,
                local_end=pos + len(word.text),
                char_start=word.char_start,
                char_end=word.char_end,
                index=i,
            )
        )
        parts.append(word.text)
        pos += len(word.text)
    return "".join(parts), tuple(words)


def _covered_words(box: Box, ocr: RecognizerInput) -> list[int] | None:
    """Indices of the words the box really holds, or None if it constrains
    nothing — it covers no word at all, or so many that fuzzy matching under
    it would be the page-wide edit-distance search the design rules out."""
    covered = [
        i
        for i, w in enumerate(ocr.words)
        if _area(w.box) > 0
        and _intersection_area(w.box, box) / _area(w.box) >= _BOX_WORD_OVERLAP
    ]
    if not covered or len(covered) > _MAX_BOX_WORDS:
        return None
    return covered


def _box_words(box: Box, ocr: RecognizerInput) -> frozenset[int] | None:
    """The words a wrapped match inside `box` may be built from.

    Per-LINE slack here, unlike `_box_line`'s outer-only slack, and the two
    differ because the searches differ. A flat assembly is scanned for a
    substring, so slack at a line seam splices junk into the middle of the
    needle and the match dies; the wrapped walk is needle-driven and simply
    never starts on a word the needle does not begin with, so it can be
    offered the whole line-end without harm. That is what recovers the word a
    box clips off the END of a wrapped value's first line — interior to the
    assembly, and unreachable from its outer ends.
    """
    covered = _covered_words(box, ocr)
    if covered is None:
        return None
    rows: dict[int, list[int]] = {}
    for i, word in enumerate(ocr.words):
        rows.setdefault(word.line, []).append(i)
    hit = set(covered)
    allowed = set(covered)
    for row in rows.values():
        marked = [n for n, i in enumerate(row) if i in hit]
        if not marked:
            continue
        allowed.update(
            row[
                max(marked[0] - _WORD_SLACK, 0) : min(
                    marked[-1] + _WORD_SLACK, len(row) - 1
                )
                + 1
            ]
        )
    return frozenset(allowed)


def _wrapped_occurrences(
    ocr: RecognizerInput,
    need_sq: str,
    allowed: frozenset[int] | None = None,
) -> list[tuple[tuple[int, int], ...]]:
    """Occurrences of a needle the page WRAPS, as one span per line it uses.

    The page string cannot be searched for these. `_rows` bands a page
    VISUALLY, which is what puts a label beside its value and is load-bearing
    for context promotion; the price is that two cards side by side share
    every band, so a value that wraps inside one column has the other column's
    row-mate spliced between its halves ('24 Stacey Dr' / 'Expiry date 12
    March 2025 11:59pm AEST' / 'Carrickalinga SA 5204', 2026-08-13). Squashing
    does not bridge that — the interloper is alphanumeric, not separators —
    and a contiguous word window has to swallow it whole.

    So the needle drives the search instead of the page: each line contributes
    one run of whole words that continues the needle exactly, and a run only
    ever starts where the needle's next character does. That is what lets the
    walk be offered a whole line without picking anything up from it.

    Two guards, both geometric, and they are the same primitive `_rows` bands
    with: pieces sit on CONSECUTIVE lines and share an x-column. That is
    exactly what separates 'Carrickalinga SA 5204' from the 'AEST' printed to
    its left on the same assembled line.

    `allowed` restricts the walk to the words a box covers (`_box_words`).
    With none given the whole page is in play, which is the borrowed case —
    there is no box to say where on this page the value belongs.

    Matching is squash-EQUALITY over whole words, never edit distance: the
    fuzzy tier is licensed by a box constraining the candidate set, and a
    needle assembled across a line break is already spending that licence.
    """
    by_line: dict[int, list[int]] = {}
    for i, word in enumerate(ocr.words):
        if allowed is None or i in allowed:
            by_line.setdefault(word.line, []).append(i)

    out: list[tuple[tuple[int, int], ...]] = []

    def walk(
        line: int,
        spans: tuple[tuple[int, int], ...],
        consumed: int,
        previous: Box | None,
    ) -> None:
        row = by_line.get(line)
        if row is None:
            return
        for first in range(len(row)):
            piece: list[int] = []
            at = consumed
            for i in row[first:]:
                if piece and i != piece[-1] + 1:
                    break  # a gap in the row is text the box or line dropped
                chunk, _ = squash_map(ocr.words[i].text)
                # A word of pure punctuation squashes to nothing, and
                # `startswith("")` is true at every position — so without this
                # it joins any piece anywhere, for free. A piece of one such
                # word then consumes NONE of the needle while still counting
                # as a proper prefix, and the walk carries it to the next line
                # where the real value completes the match: every needle
                # claims whatever stray '-' or '?' sits on the line above it
                # (2026-08-13, an insurance heading's hyphen and a card's help
                # icon, painted as ORGANIZATION and PERSON). Every piece must
                # earn its place in the needle.
                if not chunk or not need_sq.startswith(chunk, at):
                    break
                piece.append(i)
                at += len(chunk)
                extent = _union([ocr.words[j].box for j in piece])
                if previous is not None and not _shares_column(previous, extent):
                    continue
                span = (
                    ocr.words[piece[0]].char_start,
                    ocr.words[piece[-1]].char_end,
                )
                if at == len(need_sq):
                    # A needle that fits on one line is not wrapped, and the
                    # contiguous tiers already own it.
                    if spans:
                        out.append(spans + (span,))
                else:
                    walk(line + 1, spans + (span,), at, extent)

    for line in by_line:
        walk(line, (), 0, None)
    return out


def _shares_column(a: Box, b: Box) -> bool:
    """Whether two pieces on consecutive lines stand in the same column."""
    return min(a.right, b.right) > max(a.left, b.left)


def _to_spans(
    words: tuple[_LocalWord, ...], start: int, end: int
) -> tuple[tuple[int, int], ...]:
    """A local character range as page spans — one per contiguous run of words.

    Consecutive entries of the source map are consecutive in the page string,
    so a run of them is a genuine contiguous range of it; a jump in word index
    is where the assembly stepped over another column and starts a new span.
    Offsets INSIDE the first and last word are carried across, so an assembled
    match is never wider than the page-wide tiers would have been.
    """
    hit = [w for w in words if max(start, w.local_start) < min(end, w.local_end)]
    if not hit:
        return ()
    runs = [[hit[0]]]
    for word in hit[1:]:
        if word.index == runs[-1][-1].index + 1:
            runs[-1].append(word)
        else:
            runs.append([word])
    return tuple(
        (
            run[0].char_start + max(start - run[0].local_start, 0),
            run[-1].char_end - max(run[-1].local_end - end, 0),
        )
        for run in runs
    )


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
        and _intersection_area(w.box, box) / _area(w.box) >= _BOX_WORD_OVERLAP
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
    ocr: RecognizerInput,
    spans: tuple[tuple[int, int], ...],
    box: Box | None,
) -> float:
    """How much of `box` and the candidate's pixels coincide, as a fraction of
    the smaller — tolerant of a box that clips (smaller than the value) and of
    one that bloats (larger), both of which are measured behaviours. Over ALL
    the candidate's spans: an assembled value is one candidate, and scoring
    only part of it would rank it below a lesser match."""
    if box is None:
        return 0.0
    boxes = [b for start, end in spans for b in ocr.boxes_for_span(start, end)]
    if not boxes:
        return 0.0
    candidate_area = sum(_area(b) for b in boxes)
    shared = sum(_intersection_area(b, box) for b in boxes)
    smaller = min(candidate_area, _area(box))
    return shared / smaller if smaller > 0 else 0.0


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
) -> list[tuple[int, int, str, str | None]]:
    """Every occurrence on this page of a value the DOCUMENT knows about.

    This is what makes a value the model named on page 1 and missed on page 4
    strip on both. It runs beside `locate_findings`, not instead of it: the
    page's own findings still go through the box-guided tiers (the only route
    to tier-3 geometry), and this pass adds every occurrence of every known
    value on top. Overlaps between the two are unioned by
    `PiiPipeline._merge_overlaps` like any other pair of spans.

    Four tiers, in three passes over the needles. Exact and squash run first
    for ALL needles, then wrapped, then fuzzy — so textual certainty always
    outranks edit distance no matter which needle gets there first.

    Wrapped and fuzzy are both **additive, not fallbacks**: a page carrying a
    value's full form exactly AND a damaged or wrapped form would otherwise
    find the exact one, skip the later tiers, and leak the other. That is a
    real specimen, not a hypothetical (2026-08-11, `SK BUSINESS TRUS`;
    2026-08-13, an address printed twice on one page and wrapped both times).

    `needles` is `(value, entity_type)` LONGEST FIRST — two needles can land on
    one span ('John' inside 'John Smith') and the wider one must claim it.
    Returns `(start, end, entity_type, full_value)` in document order, where
    `full_value` is set only on the pieces of a wrapped value: they are ONE
    value and must collect one placeholder, which the span text alone cannot
    say (see `image_mode`).
    """
    text = ocr.text
    squashed: tuple[str, list[int]] | None = None
    claimed: dict[tuple[int, int], tuple[str, str | None]] = {}
    for value, entity_type in needles:
        spans = _bounded_occurrences(text, value)
        if not spans:
            if squashed is None:
                squashed = squash_map(text)
            spans = _bounded_squash_occurrences(squashed, text, value)
        for span in spans:
            claimed.setdefault(span, (entity_type, None))

    for value, entity_type in needles:
        need_sq, _ = squash_map(value)
        if len(need_sq) < _MIN_SQUASH_CHARS:
            continue
        for spans in _wrapped_occurrences(ocr, need_sq):
            free = [span for span in spans if span not in claimed]
            # The whole value only names itself when the whole of it is this
            # pass's to claim; where another needle already owns a piece, the
            # rest is claimed on its own terms rather than half-labelled with
            # a value it no longer accounts for.
            full = value if len(free) == len(spans) else None
            for span in free:
                claimed[span] = (entity_type, full)

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
                claimed[(start, end)] = (entity_type, None)
                taken.append((start, end))

    return [
        (start, end, entity_type, full_value)
        for (start, end), (entity_type, full_value) in sorted(claimed.items())
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

"""The PDF's own text layer: an OCR repair source, constrained by OCR geometry.

A text PDF carries the true characters already; the OCR that layer 1 reads
does not. On a real statement page the OCR read the account number `018057571`
as `O18057571`, and **no rule matched anything at all** — `\\b\\d{5,10}\\b`
cannot start after a letter — so the value survived the run unredacted while
the text layer of that same page had the digits.

This module pairs each OCR word with the text-layer word describing the same
pixels and prefers the text layer's characters where they agree about the
pixels but disagree about the reading. It does NOT overturn the
treat-PDFs-as-images decision (pii.core.pdf_mode): the text layer is a repair
source **constrained by OCR geometry, never an independent detection source**
— the same shape as "a model box is a search constraint, not paint geometry".
Only a word the OCR already saw is repaired, only where positionally matched
and similar, and a word the OCR missed is never added.

**A repair keeps the same words in the same order**, with the same
`region_box`; a word's `text` and its `box` are what may change, and they change
on separate evidence. Repair runs at the `OcrPage` word level, BEFORE
`linearize`, so the source map is built from repaired words and offsets, boxes,
painting and the pseudonym map stay consistent by construction, with no
remapping anywhere.

What "matches" means is the whole feature; the gates and the measurements
behind each of them are in core/ARCHITECTURE.md, and the corpus numbers in
core/DONE.md. In short, and in the order they run:

1. **Geometry buckets, it does not pair.** Text words are assigned to the OCR
   line they vertically overlap, and nothing further is decided by position
   alone: an independent per-word best-overlap pairing drifts by one across a
   whole line when OCR word boxes are interpolated (measured on the first page
   of the first specimen).
2. **Alignment decides the correspondence** — one order-preserving alignment
   per line, so a neighbour anchors every pair.
3. **Only a 1:1 match is repaired.** Merges are aligned so the correspondence
   stays right, never applied: every k>1 merge in the corpus was noise.
4. **The readings must agree** on the squashed forms, under `fuzzy.budget_for`
   — `_same_reading`, and the one gate the PAGE is judged on.
5. **The extent must agree**: a repair changes what a token says, never how
   much of the page it covers.
6. **The boxes must agree both ways**, never at `min(area)`.
7. **No non-graphic character may be introduced**, because a text layer can be
   worse than the OCR: one specimen renders a BSB with a SOFT HYPHEN, which
   would silently break the `[ -]` separator class.

Plus a page-level guard: a text layer that does not describe these pixels (a
different revision, another tool's OCR baked in) disables repair for THAT PAGE.
It counts gate 4 alone — a pair refused by 5, 6 or 7 is a good correspondence
we decline to act on, and holding those against the layer would disable repair
on a page whose only problem is that OCR interpolated some boxes.

**The BOX is corrected too, and on its own gates** (`_lendable`): a paddle word
box is a stochastic estimate, a text-layer box is where the renderer drew the
glyphs. The two corrections are independent because their common cases are
disjoint — the reading `O18057571` is wrong where its box is right, and the
reading `244616.` is right where its box is a word off, which painted 24.5% of
a credit licence number and destroyed the address beside it. Gate 6 explicitly
does NOT apply to a box; gates 5, plus vertical agreement and containment in
the word's own detection region, do.

The same pairing fills `OcrWord.font` / `OcrLine.font`, which no OCR engine can
supply. Font is RENDER-ONLY (pii.core.paint draws a placeholder in the face it
replaces) and must never reach a detection decision.

pymupdf is imported lazily, inside `page_text_words` alone: the alignment is
pure and testable without a PDF or an OCR engine.
"""

from dataclasses import dataclass, replace
import unicodedata

from pii.core.fuzzy import CONFUSION_COSTS, budget_for, distance
from pii.core.ocr import Box
from pii.core.ocr_page import (
    SOURCE_AGREED,
    SOURCE_TEXT,
    FontSpec,
    OcrLine,
    OcrPage,
    OcrWord,
    _line_box,
)
from pii.core.vlm import squash_map

# --- Gates. Every constant here was chosen against the reference corpus; the
# measurements are in core/DONE.md, the rationale in core/ARCHITECTURE.md. ---

# A text word joins the OCR line it overlaps vertically by this fraction of the
# shorter height. Bucketing only — which line, not which word.
_LINE_BUCKET = 0.5

# Alignment: cost of leaving a word unpaired, and the longest run of text words
# one OCR word may be aligned to (OCR glued them together). The merge exists to
# keep the CORRESPONDENCE right; it is never repaired.
_GAP = 0.6
_MERGE_MAX = 4

# A repair needs the two boxes to overlap by this fraction of EACH area. Two-way
# is the point: at `min(area)` a word wholly inside a much wider text word scores
# 1.0, which is how `185871` came to be "repaired" to `185871` plus a hundred
# leader dots (measured f_ocr=1.00, f_txt=0.06).
_MIN_OVERLAP = 0.3

# Lending a box: the two axes that are not broken by the drift being corrected
# (see `_lendable`) — the boxes must agree vertically, and the lent box must sit
# this far inside the OCR word's own detection region, measured horizontally.
_LEND_V_OVERLAP = 0.5
_LEND_IN_REGION = 0.9

# The page-level guard: with at least this many aligned pairs, this fraction of
# them must survive the similarity gate or the text layer is not describing
# these pixels and the page is left to the OCR alone. No specimen in the
# reference corpus triggers it (agreement runs 90-99%) — it is here for the
# junk-text-layer risk that made PDFs be treated as images in the first place.
_PAGE_MIN_PAIRS = 20
_PAGE_MIN_AGREEMENT = 0.5


@dataclass(frozen=True)
class TextWord:
    """One word of the PDF's text layer, in page-raster pixels."""

    text: str
    box: Box
    font: FontSpec | None = None


@dataclass(frozen=True)
class RepairReport:
    """What the text layer did to one page's OCR.

    Counted rather than merely warned about, for the reason
    `ImageStripResult.unlocated` is: Python's default warning filter shows one
    instance per code location, so page 2 onward would be silent.

    `disabled` is the page-level guard having fired — the page carries a text
    layer that does not describe its pixels, and nothing was repaired or
    attributed from it.
    """

    words: int = 0  # OCR words on the page
    paired: int = 0  # aligned to a text-layer word
    agreed: int = 0  # ...of those, within the similarity budget
    repaired: int = 0  # ...of those, whose reading actually changed
    relocated: int = 0  # ...of those, whose BOX moved to the text layer's
    disabled: bool = False

    def __bool__(self) -> bool:
        return bool(self.paired or self.disabled)

    def __add__(self, other: "RepairReport") -> "RepairReport":
        if not isinstance(other, RepairReport):
            return NotImplemented
        return RepairReport(
            words=self.words + other.words,
            paired=self.paired + other.paired,
            agreed=self.agreed + other.agreed,
            repaired=self.repaired + other.repaired,
            relocated=self.relocated + other.relocated,
            disabled=self.disabled or other.disabled,
        )


def page_text_words(page, dpi: int) -> tuple[TextWord, ...]:
    """The text layer of one pymupdf page, in the pixels of its `dpi` raster.

    **The transform is `page.rotation_matrix`, not a scalar.** `get_pixmap`
    applies `/Rotate` itself, so the raster is upright-as-displayed, while
    `get_text` returns UNROTATED page coordinates: on a 90-degree page a naive
    `x * dpi/72` puts a word at x=104 where its ink is at x=1041 (measured, all
    four rotations). A shifted CropBox needs no handling — `page.rect` is
    normalized to the origin and text coordinates come in that frame (also
    measured).

    Word boxes come from `get_text("words")`, which has accurate per-word
    geometry but no font; fonts come from the `get_text("dict")` spans, which
    have the font but cover several words. Each word takes the font of the span
    it overlaps most. Both views are the same text layer, so they agree by
    construction.
    """
    import pymupdf

    scale = dpi / 72
    matrix = page.rotation_matrix * pymupdf.Matrix(scale, scale)
    spans: list[tuple[Box, FontSpec]] = []
    for block in page.get_text("dict").get("blocks", ()):
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                spans.append((
                    _mapped_box(pymupdf.Rect(span["bbox"]), matrix),
                    _font_spec(span, scale),
                ))
    words = []
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        if not text:
            continue
        box = _mapped_box(pymupdf.Rect(x0, y0, x1, y1), matrix)
        words.append(TextWord(text=text, box=box, font=_font_at(box, spans)))
    return tuple(words)


def repair_page(
    page: OcrPage, words: tuple[TextWord, ...]
) -> tuple[OcrPage, RepairReport]:
    """Repair `page`'s readings from `words`, and attribute their fonts.

    Returns a new page whose words are the same words in the same order, with
    the same `region_box` — `text`, `box`, `font` and `source` can differ —
    plus the report. With no text layer, or with the page-level guard fired,
    the page comes back untouched (every word `SOURCE_OCR`) and nothing
    downstream can tell repair was available.

    Characters and boxes travel on SEPARATE gates (`_repairable` / `_lendable`)
    because they are corrected against different evidence and the common cases
    are disjoint: the reading `O18057571` is wrong where its box is right, and
    the reading `244616.` is right where its box is a word off.
    """
    total = sum(len(line.words) for line in page.lines)
    if not words or not total:
        return page, RepairReport(words=total)

    buckets = _bucket(page, words)
    # Two passes over one alignment: the first only measures, because whether
    # to trust this text layer at all is a PAGE decision and it has to be made
    # before any word is rewritten.
    #
    # The page decision counts READING agreement alone — not the repair gates
    # below. "Does this text layer describe these pixels" is answered by
    # whether aligned words say the same things; a pair refused for its
    # geometry or its extent is a good correspondence we decline to act on,
    # and counting those against the layer would disable repair on a page
    # whose only problem is that OCR interpolated some boxes.
    matches: list[list[tuple[int, TextWord, bool, bool]]] = []
    paired = agreed = 0
    for index, line in enumerate(page.lines):
        line_matches = []
        for word_index, partner, single in _matches(line, buckets[index], words):
            paired += 1
            same = _same_reading(line.words[word_index], partner)
            agreed += same
            line_matches.append((word_index, partner, single, same))
        matches.append(line_matches)

    if paired >= _PAGE_MIN_PAIRS and agreed < _PAGE_MIN_AGREEMENT * paired:
        return page, RepairReport(
            words=total, paired=paired, agreed=agreed, disabled=True
        )

    repaired = relocated = 0
    lines = []
    for line, line_matches in zip(page.lines, matches):
        replacements = {}
        for word_index, other, single, ok in line_matches:
            word = line.words[word_index]
            # Same printed row is the whole gate for FONT: it is cosmetic,
            # adjacent words share a face, and a word whose reading we declined
            # to trust still sits in the span that describes it. Deliberately
            # NOT the horizontal gate — that would deny a face to exactly the
            # drifted words this loop is here to relocate.
            if _v_overlap(word.box, other.box) < _LEND_V_OVERLAP:
                continue
            # The box travels on its own gates (`_lendable`), not the
            # character ones: a word whose reading needs no repair can still
            # need relocating, and that is the common case — the OCR read
            # `244616.` correctly and boxed it a word to the right.
            box = word.box
            if ok and _lendable(word, other):
                box = other.box
                relocated += box != word.box
            # Identical readings are marked `agreed` whatever the CHARACTER
            # gates say: nothing is being substituted, so the gates that guard a
            # substitution have no bearing on them. A pair that agrees but
            # DIFFERS and is then refused stays `ocr` — we declined the text
            # layer's opinion there, and the overlay should not claim
            # otherwise.
            if ok and word.text == other.text:
                replacements[word_index] = replace(
                    word, box=box, font=other.font, source=SOURCE_AGREED
                )
            elif ok and single and _repairable(word, other):
                repaired += 1
                replacements[word_index] = replace(
                    word, text=other.text, box=box, font=other.font,
                    source=SOURCE_TEXT,
                )
            else:
                replacements[word_index] = replace(
                    word, box=box, font=other.font
                )
        if not replacements:
            lines.append(line)
            continue
        new_words = tuple(
            replacements.get(index, word)
            for index, word in enumerate(line.words)
        )
        lines.append(
            replace(
                line,
                text=" ".join(w.text for w in new_words),
                words=new_words,
                box=_line_box(list(new_words)),
                font=_modal_font(new_words),
            )
        )
    return (
        replace(page, lines=tuple(lines)),
        RepairReport(
            words=total, paired=paired, agreed=agreed, repaired=repaired,
            relocated=relocated,
        ),
    )


# --- Bucketing: which LINE, never which word ---


def _bucket(
    page: OcrPage, words: tuple[TextWord, ...]
) -> list[list[int]]:
    """Assign each text word to the OCR line it vertically overlaps most.

    Left-to-right within the line, across the whole visual row: `_rows` bands a
    page visually, so one OCR line can span two columns — and both sources see
    those columns in the same x order, which is what keeps the alignment below
    meaningful on a two-column page.
    """
    buckets: list[list[int]] = [[] for _ in page.lines]
    for index, word in enumerate(words):
        best, chosen = _LINE_BUCKET, None
        for line_index, line in enumerate(page.lines):
            overlap = _v_overlap(word.box, line.box)
            if overlap > best:
                best, chosen = overlap, line_index
        if chosen is not None:
            buckets[chosen].append(index)
    for bucket in buckets:
        bucket.sort(key=lambda index: words[index].box.left)
    return buckets


def _v_overlap(a: Box, b: Box) -> float:
    height = min(a.bottom, b.bottom) - max(a.top, b.top)
    return height / max(min(a.height, b.height), 1) if height > 0 else 0.0


# --- Alignment: the correspondence, decided in reading order ---


def _matches(line: OcrLine, bucket: list[int], words: tuple[TextWord, ...]):
    """Yield `(word_index, partner, single)` for one line, where `partner` is a
    `TextWord` — the aligned one, or the pieces of a merge combined.

    `single` marks a 1:1 match, and it gates the CHARACTERS only. Where OCR read
    one token and the text layer has several, which characters belong where is
    not established — but the EXTENT is exactly established, it is the union of
    those pieces, so a merge still lends its box. Leaving merges on their
    original coordinates while their neighbours move is worse than either: it
    puts two coordinate systems on one line, and a lent box then starts inside
    an unlent one, which is how a span came to over-paint a neighbouring word by
    217 px in the first cut of box lending.
    """
    if not bucket or not line.words:
        return
    ocr_texts = [w.text for w in line.words]
    text_texts = [words[index].text for index in bucket]
    for word_index, run in _align(ocr_texts, text_texts):
        pieces = [words[bucket[index]] for index in run]
        yield word_index, _combined(pieces), len(run) == 1


def _combined(pieces: list[TextWord]) -> TextWord:
    """One or more aligned text words as a single partner: their readings joined
    (OCR saw no space between them, or it would have split them too) over their
    union extent, carrying the first piece's face."""
    if len(pieces) == 1:
        return pieces[0]
    return TextWord(
        text="".join(p.text for p in pieces),
        box=Box(
            left=min(p.box.left for p in pieces),
            top=min(p.box.top for p in pieces),
            width=max(p.box.right for p in pieces) - min(p.box.left for p in pieces),
            height=max(p.box.bottom for p in pieces) - min(p.box.top for p in pieces),
        ),
        font=pieces[0].font,
    )


def _align(a: list[str], b: list[str]) -> list[tuple[int, list[int]]]:
    """Order-preserving alignment of two word sequences.

    Needleman-Wunsch over normalized character distance, with gaps on both
    sides and runs of up to `_MERGE_MAX` `b` words aligned to one `a` word.
    Returns the matched pairs only; gaps are simply absent.

    Order preservation is the whole reason this exists. Nearest-box pairing has
    no such constraint and drifts: on the first page measured, a run of eight
    words each paired with its neighbour's partner (`AND`->`ADVISE`,
    `ADVISE`->`US`, `US`->`PROMPTLY`, ...), every one of them a wrong
    correspondence that only the similarity gate happened to reject. Aligned,
    the same run pairs exactly and confirms.
    """
    n, m = len(a), len(b)
    infinity = float("inf")
    cost = [[infinity] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int, int] | None]] = [
        [None] * (m + 1) for _ in range(n + 1)
    ]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            here = cost[i][j]
            if here == infinity:
                continue
            if i < n and here + _GAP < cost[i + 1][j]:
                cost[i + 1][j] = here + _GAP
                back[i + 1][j] = (i, j, 0)
            if j < m and here + _GAP < cost[i][j + 1]:
                cost[i][j + 1] = here + _GAP
                back[i][j + 1] = (i, j, 0)
            if i == n:
                continue
            for k in range(1, _MERGE_MAX + 1):
                if j + k > m:
                    break
                # A merge pays half a gap per extra word, so it is preferred
                # over gapping them only when the joined form really does read
                # like the one token OCR saw.
                total = (
                    here
                    + _pair_cost(a[i], "".join(b[j:j + k]))
                    + _GAP * (k - 1) * 0.5
                )
                if total < cost[i + 1][j + k]:
                    cost[i + 1][j + k] = total
                    back[i + 1][j + k] = (i, j, k)
    pairs = []
    i, j = n, m
    while (i, j) != (0, 0):
        previous_i, previous_j, k = back[i][j]
        if k:
            pairs.append((previous_i, list(range(previous_j, previous_j + k))))
        i, j = previous_i, previous_j
    return list(reversed(pairs))


def _pair_cost(a: str, b: str) -> float:
    """Alignment cost of reading `a` as `b`: 0 identical, 1 unrelated."""
    left, _ = squash_map(a)
    right, _ = squash_map(b)
    if not left or not right:
        return 1.0
    if left == right:
        return 0.0
    return min(
        distance(left, right, costs=CONFUSION_COSTS) / max(len(left), len(right)),
        1.0,
    )


# --- The gates a repair must pass ---


def _same_reading(word: OcrWord, other: TextWord) -> bool:
    """Whether an aligned pair says the same thing, to within OCR damage.

    The evidence the PAGE is judged on: two transcriptions of the same pixels
    disagree in ways the alphanumeric squash and `fuzzy`'s confusion tables
    between them absorb, and a text layer describing some OTHER page does not
    survive that at all.

    The permissive table, not the identifier one. The question here is "is this
    the same printed WORD", which the alignment and the geometry answer between
    them — not "is this the same VALUE", where a digit read as a different digit
    means a different account. So `396` and `395` in the same place on the same
    line are a misread digit, and repairing it is the point (Sergei,
    2026-08-18).
    """
    left, _ = squash_map(word.text)
    right, _ = squash_map(other.text)
    if not left or not right:
        return False
    budget = budget_for(right)
    return distance(left, right, ceiling=budget, costs=CONFUSION_COSTS) <= budget


def _lendable(word: OcrWord, other: TextWord) -> bool:
    """Whether a confirmed pair may lend its BOX as well as its characters.

    A paddle word box is a stochastic estimate; a text-layer box is the
    typesetting geometry — where the renderer actually drew the glyphs — so
    where the two disagree about position, the text layer is right. Measured on
    `ServletRetrieve (6).pdf` p1 the OCR boxes drift rightwards *within a
    detection region*, up to 185 px (more than a word width): the painted box
    for the credit licence number covered 24.5% of its digits and destroyed a
    chunk of the address instead, and an account number was painted at 37%.
    Both are partial paints, which is to say leaks.

    **The horizontal overlap gate deliberately does NOT apply here.** It is the
    evidence `_repairable` uses for identity, and on exactly the words worth
    relocating it fails — `244616.` overlaps its own true box by 0.25. Geometry
    cannot be both the evidence and the thing being corrected, so identity comes
    from the alignment and the reading, and the guards here are the two axes
    that are NOT broken:

    - **vertically**, the boxes must agree — the drift is horizontal, so a
      partner on another printed row is a mis-assignment, not a drift;
    - **horizontally**, the lent box must stay inside the OCR word's own
      DETECTION REGION. That is the rectangle paddle actually found on the
      pixels, and the drift is a stretch *inside* it (shifts along one region
      run 12, 16, 44, 93, 139 and then reset to 12 at the next region), so the
      region is exactly the distance a correction may legitimately travel and a
      box can never fly across the page.

    The extent gate applies here MORE than to characters: lending the box of a
    text word carrying thirty leader dots would over-paint the rest of the line.
    """
    if not _same_extent(word, other):
        return False
    if _v_overlap(word.box, other.box) < _LEND_V_OVERLAP:
        return False
    # HORIZONTALLY inside, not by area: the drift is horizontal, so that is
    # the axis on which "how far may a correction travel" is the question, and
    # the vertical is already answered above. A detection region is glyph-tight
    # vertically while a text-layer box is a glyph ADVANCE box with room for the
    # descender, so an area test fails a correct lending on two pixels of
    # overhang — which is how the credit licence number stayed unpainted the
    # first time this gate was written.
    region = word.region
    inside = max(
        0, min(other.box.right, region.right) - max(other.box.left, region.left)
    )
    return inside >= _LEND_IN_REGION * max(other.box.width, 1)


def _repairable(word: OcrWord, other: TextWord) -> bool:
    """Whether a pair that agrees may actually be acted on: same place, same
    extent, and a reading fit to substitute.

    The extent gate is not a refinement of the reading one. Squashing drops
    separators, so `30-743-3257` and `30-743-3257` followed by thirty leader
    dots squash to the SAME string and are at distance zero — the text layer
    puts a table-of-contents leader inside its word and OCR does not. Every
    such pair in the reference corpus is rejected here and by nothing else:
    the two-way overlap does not separate them (0.36 for a leader against 0.41
    for a real repair whose OCR box was interpolated), because the dots really
    are on the page. A repair changes what a token SAYS, never how much of the
    page it covers.
    """
    return (
        _same_extent(word, other)
        and _admissible(other.text)
        and _overlaps(word.box, other.box)
    )


def _same_extent(word: OcrWord, other: TextWord) -> bool:
    """Whether the two cover the same much of the page, measured on raw length.

    Squashing drops separators, so a text layer that puts a hundred
    table-of-contents leader dots inside its word is at distance ZERO from the
    number OCR read there. Nothing else in the gate set sees it — two-way
    overlap does not separate them either (0.36 for the worst leader against
    0.41 for a real repair whose OCR box was interpolated), because the dots
    really are on the page."""
    right, _ = squash_map(other.text)
    return abs(len(other.text) - len(word.text)) <= budget_for(right)


def _overlaps(a: Box, b: Box) -> bool:
    """Two-way box agreement: the intersection must be `_MIN_OVERLAP` of EACH
    area, so neither box may extend far beyond the other."""
    width = min(a.right, b.right) - max(a.left, b.left)
    height = min(a.bottom, b.bottom) - max(a.top, b.top)
    if width <= 0 or height <= 0:
        return False
    intersection = width * height
    return all(
        intersection >= _MIN_OVERLAP * max(box.width, 1) * max(box.height, 1)
        for box in (a, b)
    )


def _admissible(text: str) -> bool:
    """Whether a text-layer reading may replace an OCR one at all.

    A text layer can be WORSE than the OCR, which is why PDFs are treated as
    images to begin with. One reference statement renders a BSB with U+00AD
    (soft hyphen) where the page shows a hyphen: preferring it would delete the
    separator from `[ -]` and unmatch the rule. Anything in a Unicode C
    category — control, format, surrogate, private use, unassigned — and the
    replacement character are refused, and the OCR's own reading stands.
    """
    return text and all(
        ch != "�" and not unicodedata.category(ch).startswith("C")
        for ch in text
    )


# --- Fonts ---


def _font_spec(span: dict, scale: float) -> FontSpec:
    """A span's face. Style comes from the flags, which are reliable
    (`Arial-BoldMT`=16, `Courier`=8, `Calibri,Italic`=6), with the name as a
    backup for the families that encode weight numerically (`MuseoSans-700`).

    **The serifed flag is not used at all.** Measured across the reference
    corpus it is wrong more often than right: `ArialMT` and `Helvetica` each
    appear with the bit both set and clear in different documents, and
    `FrutigerLTPro`, `Roboto`, `MyriadPro` and `Gotham` are all flagged serifed
    — while not one true serif face appears in the corpus. Serif is read off
    the NAME instead, and defaults to sans, which is right for essentially
    every financial document; a wrong call here is cosmetic.
    """
    flags = int(span.get("flags", 0))
    name = _base_name(str(span.get("font", "")))
    return FontSpec(
        name=name,
        size=float(span.get("size", 0.0)) * scale,
        bold=bool(flags & 16) or _styled(name, ("bold", "black", "heavy")),
        italic=bool(flags & 2) or _styled(name, ("italic", "oblique")),
        mono=bool(flags & 8) or _stemmed(name, _MONO_STEMS),
        # Deliberately NOT `flags & 4` — see FontSpec.
        serif=_stemmed(name, _SERIF_STEMS),
    )


# Serif families by NAME. Short and conservative on purpose: a wrong call is
# cosmetic, and "book"/"roman" are excluded although they look diagnostic —
# both appear in sans names in the corpus (`Gotham-Book`,
# `HelveticaNeueLT-Roman`).
_SERIF_STEMS = (
    "times", "georgia", "garamond", "cambria", "palatino", "baskerville",
    "minion", "caslon", "serif", "utopia", "sabon",
)
_MONO_STEMS = ("courier", "consolas", "mono", "menlo", "monaco")


def _base_name(font: str) -> str:
    """A PDF font name without its subset prefix: `AAIBMH+Gotham-Bold` ->
    `Gotham-Bold`."""
    _, _, rest = font.rpartition("+")
    return rest or font


def _styled(name: str, stems: tuple[str, ...]) -> bool:
    """Style read off the name, as a backup for the flags: `MuseoSans-700,Bold`
    carries no bold flag."""
    lowered = name.lower()
    return any(stem in lowered for stem in stems)


def _stemmed(name: str, stems: tuple[str, ...]) -> bool:
    lowered = name.lower()
    return any(stem in lowered for stem in stems)


def _font_at(box: Box, spans: list[tuple[Box, FontSpec]]) -> FontSpec | None:
    """The font of the span covering `box` most."""
    best, chosen = 0, None
    for span_box, spec in spans:
        width = min(box.right, span_box.right) - max(box.left, span_box.left)
        height = min(box.bottom, span_box.bottom) - max(box.top, span_box.top)
        if width > 0 and height > 0 and width * height > best:
            best, chosen = width * height, spec
    return chosen


def _modal_font(words: tuple[OcrWord, ...]) -> FontSpec | None:
    """The line's font: the one most of its words carry.

    A line is one face in practice; where it is not, the words keep their own
    and only the line-level summary is approximate."""
    counts: dict[FontSpec, int] = {}
    for word in words:
        if word.font is not None:
            counts[word.font] = counts.get(word.font, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: item[1])[0]


def _mapped_box(rect, matrix) -> Box:
    """A pymupdf rect through the render matrix, as a pixel Box. Built from the
    extremes rather than from `x0`/`y0`, because a rotation swaps them."""
    mapped = rect * matrix
    left, right = sorted((mapped.x0, mapped.x1))
    top, bottom = sorted((mapped.y0, mapped.y1))
    return Box(
        left=int(left),
        top=int(top),
        width=max(int(right) - int(left), 1),
        height=max(int(bottom) - int(top), 1),
    )

"""Box-guided value location.

Two things are under test and they pull in opposite directions:

- the box must SHARPEN matching (repeats, collisions, nested findings) and
  admit fuzzy matching that would be reckless page-wide;
- and it must never make things worse than the unconstrained search that
  preceded it, which is why every no-box case here asserts the old behaviour.

Model-free: findings are constructed directly, and the OCR page is built
through the real perception -> linearization seam. Dual coverage per the
project rule — the corpus probe is the other half.
"""

from __future__ import annotations

import pytest

from pii.core.linearization import linearize
from pii.core.locator import denormalize, locate_borrowed, locate_findings
from pii.core.ocr import Box
from pii.core.ocr_page import OcrFrame, build_page
from pii.core.vlm import VlmFinding

# Page geometry for the synthetic layouts below: one row per line, words laid
# out left to right, all boxes the same height.
_CHAR_W = 10
_LINE_H = 12
_LINE_GAP = 20
_PAGE_W = 1000
_PAGE_H = 400


def _page(*lines: str):
    """A RecognizerInput from one string per text line."""
    rows = []
    for row_index, line in enumerate(lines):
        row, x = [], 0
        for token in line.split(" "):
            width = _CHAR_W * len(token)
            row.append((token, Box(x, row_index * _LINE_GAP, width, _LINE_H), 99.0))
            x += width + _CHAR_W
        rows.append(row)
    return linearize(
        build_page(rows, OcrFrame(width=_PAGE_W, height=_PAGE_H, page=1))
    )


def _two_columns(*rows: tuple[str, str, str]):
    """A page of two cards side by side: `(left, right label, right value)`
    per visual row, the value right-aligned as a statement field is.

    `_rows` bands such a page into ONE line per row — deliberately, it is what
    puts a label beside its value — and the consequence under test is that a
    value which WRAPS inside the right-hand column has the left card's
    row-mate spliced between its halves in the page string.
    """
    out = []
    for row_index, (left, label, value) in enumerate(rows):
        top = row_index * _LINE_GAP
        row = []

        def place(text: str, x: int) -> int:
            for token in text.split(" "):
                width = _CHAR_W * len(token)
                row.append((token, Box(x, top, width, _LINE_H), 99.0))
                x += width + _CHAR_W
            return x

        if left:
            place(left, 0)
        if label:
            place(label, _PAGE_W // 2)
        if value:
            # Tokens are `_CHAR_W` per character with a `_CHAR_W` gap between
            # them, so a phrase is exactly `_CHAR_W * len(phrase)` wide.
            place(value, _PAGE_W - _CHAR_W - _CHAR_W * len(value))
        out.append(row)
    return linearize(
        build_page(out, OcrFrame(width=_PAGE_W, height=_PAGE_H, page=1))
    )


def _span_of(ocr, needle: str) -> tuple[int, int]:
    at = ocr.text.find(needle)
    assert at != -1, f"{needle!r} not on the page"
    return (at, at + len(needle))


def _box_spanning(ocr, *needles: str):
    """The model-space box a well-behaved model returns for a value the page
    wrapped: ONE rectangle around all of its pieces."""
    boxes = [_box_over(ocr, needle) for needle in needles]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _box_over(ocr, needle: str, occurrence: int = 0):
    """The model-space (0-1000) box a well-behaved model would return for the
    `occurrence`-th appearance of `needle` in the page text."""
    at = -1
    for _ in range(occurrence + 1):
        at = ocr.text.find(needle, at + 1)
        assert at != -1, f"{needle!r} not on the page"
    boxes = ocr.boxes_for_span(at, at + len(needle))
    left = min(b.left for b in boxes)
    top = min(b.top for b in boxes)
    right = max(b.right for b in boxes)
    bottom = max(b.bottom for b in boxes)
    return (
        round(left / _PAGE_W * 1000),
        round(top / _PAGE_H * 1000),
        round(right / _PAGE_W * 1000),
        round(bottom / _PAGE_H * 1000),
    )


def _place(ocr, *findings):
    return locate_findings(list(findings), ocr, (_PAGE_W, _PAGE_H))


def _text_of(ocr, placement) -> str:
    """The placed value as it reads on the page. A placement carries a TUPLE
    of spans — a value the page wraps inside one column of a two-column layout
    is one value in several ranges — joined here by the newline the page put
    between them."""
    return "\n".join(ocr.text[start:end] for start, end in placement.spans)


# ------------------------------------------------- no box: the old behaviour
#
# These are the pre-box locator's own tests. They must keep passing verbatim:
# a finding without a box has to behave exactly as it did before, or the
# change is a regression for every value the model declines to place.


def test_exact_match_without_a_box():
    ocr = _page("name: Sergei Kulik here")
    (p,) = _place(ocr, VlmFinding("Sergei Kulik", "PERSON")).placements
    assert _text_of(ocr, p) == "Sergei Kulik" and p.kind == "exact"


def test_formatting_differences_are_absorbed_without_a_box():
    # The two transcriptions of the same pixels need not agree on separators.
    ocr = _page("BSB 083-064 acct")
    (p,) = _place(ocr, VlmFinding("083 064", "IDENTIFIER_GENERIC")).placements
    assert _text_of(ocr, p) == "083-064" and p.kind == "squash"


def test_a_missing_value_is_reported_not_guessed():
    ocr = _page("nothing relevant")
    (p,) = _place(ocr, VlmFinding("Sergei Kulik", "PERSON")).placements
    assert p.kind is None and p.spans == ()


def test_repeated_value_maps_to_successive_occurrences_without_a_box():
    ocr = _page("24 Stacey Dr and 24 Stacey Dr")
    result = _place(
        ocr,
        VlmFinding("24 Stacey Dr", "ADDRESS"),
        VlmFinding("24 Stacey Dr", "ADDRESS"),
    )
    first, second = result.placements
    assert first.spans != second.spans
    assert _text_of(ocr, second) == "24 Stacey Dr"


def test_no_fuzzy_matching_without_a_box():
    # The constraint is what licenses edit distance. With no box there is
    # nothing to confine the search, so damaged text stays unlocatable rather
    # than being matched somewhere plausible.
    ocr = _page("acct 162-09711-4 closed")
    (p,) = _place(ocr, VlmFinding("162-097111-4", "IDENTIFIER_GENERIC")).placements
    assert p.kind is None


# --------------------------------------------------- the box as disambiguator


def test_box_picks_the_right_occurrence_of_a_repeated_value():
    ocr = _page("24 Stacey Dr billing", "posted to 24 Stacey Dr")
    box = _box_over(ocr, "24 Stacey Dr", occurrence=1)
    (p,) = _place(ocr, VlmFinding("24 Stacey Dr", "ADDRESS", box=box)).placements
    assert p.spans[0][0] == ocr.text.rfind("24 Stacey Dr")


def test_box_beats_a_squash_collision_elsewhere_on_the_page():
    # "4000" squash-matches inside "$14,000.00" — the exact failure the
    # unconstrained search had no way to reject. Here the real value is
    # OCR-damaged (0 read as O), so the collision is the only clean string
    # match on the page and still must not win.
    ocr = _page("ref 4OOO issued", "total $14,000.00 paid")
    box = _box_over(ocr, "4OOO")
    (p,) = _place(ocr, VlmFinding("4000", "IDENTIFIER_GENERIC", box=box)).placements
    assert _text_of(ocr, p) == "4OOO" and p.kind == "fuzzy"


def test_a_useless_box_still_leaves_the_page_wide_match():
    # Boxes are stochastic: one pointing at empty space must not cost a value
    # that plain matching would have found. The floor is unconditional.
    ocr = _page("name: Sergei Kulik here")
    (p,) = _place(
        ocr, VlmFinding("Sergei Kulik", "PERSON", box=(900, 900, 950, 950))
    ).placements
    assert _text_of(ocr, p) == "Sergei Kulik"


def test_nested_finding_is_recognized_as_already_covered():
    # "John" reported separately after "John Smith" must not go hunting for a
    # different John — the wider span already paints those pixels.
    ocr = _page("paid John Smith today")
    result = _place(
        ocr,
        VlmFinding("John Smith", "PERSON"),
        VlmFinding("John", "PERSON"),
    )
    wide, narrow = result.placements
    assert _text_of(ocr, wide) == "John Smith"
    assert narrow.kind == "redundant" and narrow.spans == ()


def test_a_nested_value_elsewhere_is_still_found():
    # ...but redundancy is about the pixels, not the string: a second John on
    # the page is a real second detection.
    ocr = _page("paid John Smith and John Doe")
    result = _place(
        ocr,
        VlmFinding("John Smith", "PERSON"),
        VlmFinding("John", "PERSON"),
    )
    wide, narrow = result.placements
    assert narrow.spans[0][0] == ocr.text.rfind("John")
    assert narrow.spans != wide.spans


# ------------------------------------------------------ the box licenses fuzzy


def test_fuzzy_recovers_a_dropped_character_inside_the_box():
    ocr = _page("acct 162-09711-4 closed")
    box = _box_over(ocr, "162-09711-4")
    (p,) = _place(
        ocr, VlmFinding("162-097111-4", "IDENTIFIER_GENERIC", box=box)
    ).placements
    assert _text_of(ocr, p) == "162-09711-4" and p.kind == "fuzzy"


def test_fuzzy_span_paints_exact_word_boxes_not_the_model_box():
    # Tier 2: we could not read the value, but we do have its glyph geometry.
    ocr = _page("acct 162-09711-4 closed")
    box = _box_over(ocr, "162-09711-4")
    (p,) = _place(
        ocr, VlmFinding("162-097111-4", "IDENTIFIER_GENERIC", box=box)
    ).placements
    assert p.box is None  # nothing falls back to model geometry
    ((start, end),) = p.spans
    assert ocr.boxes_for_span(start, end) == ocr.boxes_for_span(
        ocr.text.find("162-09711-4"), ocr.text.find("162-09711-4") + 11
    )


def test_a_clipped_box_still_locates_the_whole_value():
    # p90 inward clip is ~64 px. Localization tolerance is far looser than
    # painting tolerance, which is the premise of the whole design.
    ocr = _page("acct 162-09711-4 closed")
    x1, y1, x2, y2 = _box_over(ocr, "162-09711-4")
    clipped = (x1 + 30, y1, x2 - 30, y2)
    (p,) = _place(
        ocr, VlmFinding("162-09711-4", "IDENTIFIER_GENERIC", box=clipped)
    ).placements
    assert _text_of(ocr, p) == "162-09711-4"


def test_a_value_wrapped_across_lines_is_one_span():
    ocr = _page("PAKENHAM", "VIC 3810")
    box = _box_over(ocr, "PAKENHAM")
    (p,) = _place(
        ocr, VlmFinding("PAKENHAM VIC 3810", "ADDRESS", box=box)
    ).placements
    assert _text_of(ocr, p) == "PAKENHAM\nVIC 3810"


# ------------------------------------- a wrapped value in a two-column layout
#
# The page string is banded VISUALLY, so two cards side by side share every
# band. A value that wraps inside one column therefore has the other card's
# row-mate spliced between its halves, and no contiguous search can reach
# across it: the interloper is alphanumeric, so squashing does not collapse
# it, and a word window has to swallow it whole. Specimen 2026-08-13, an
# insurance certificate: layer 0 boxed the postal address perfectly, OCR read
# both lines perfectly, and the locator still fell through to tier 3.


def _wrapped_page():
    return _two_columns(
        ("Start date 13 March 2024", "Postal address", "24 Stacey Dr"),
        ("Expiry date 12 March 2025", "", "Carrickalinga SA 5204"),
    )


def test_a_value_wrapped_inside_one_column_is_located_not_dropped():
    ocr = _wrapped_page()
    assert "24 Stacey Dr\nExpiry date" in ocr.text  # the splice under test
    box = _box_spanning(ocr, "24 Stacey Dr", "Carrickalinga SA 5204")
    (p,) = _place(
        ocr,
        VlmFinding("24 Stacey Dr Carrickalinga SA 5204", "ADDRESS", box=box),
    ).placements
    assert p.kind == "squash"
    assert _text_of(ocr, p) == "24 Stacey Dr\nCarrickalinga SA 5204"


def test_a_wrapped_value_steps_over_the_neighbouring_column():
    # One value, two ranges — and the left card's row-mate between them is
    # NOT one of them. Painting it would strip an expiry date to redact an
    # address, and the placeholder would be keyed on the pair.
    ocr = _wrapped_page()
    box = _box_spanning(ocr, "24 Stacey Dr", "Carrickalinga SA 5204")
    (p,) = _place(
        ocr,
        VlmFinding("24 Stacey Dr Carrickalinga SA 5204", "ADDRESS", box=box),
    ).placements
    assert p.spans == (
        _span_of(ocr, "24 Stacey Dr"),
        _span_of(ocr, "Carrickalinga SA 5204"),
    )


def test_a_box_clipping_a_line_end_still_takes_the_whole_wrapped_value():
    # The clip that matters here is not at the value's outer edges but at the
    # END of its first line — interior to the assembly and unreachable from
    # either end of it. Recovering it is why the wrapped search is driven by
    # the needle rather than by a substring scan of the covered words.
    ocr = _wrapped_page()
    x1, y1, x2, y2 = _box_spanning(ocr, "24 Stacey Dr", "Carrickalinga SA 5204")
    clipped = (x1, y1, x2 - round(_CHAR_W * 3 / _PAGE_W * 1000), y2)
    (p,) = _place(
        ocr,
        VlmFinding("24 Stacey Dr Carrickalinga SA 5204", "ADDRESS", box=clipped),
    ).placements
    assert _text_of(ocr, p) == "24 Stacey Dr\nCarrickalinga SA 5204"


def test_a_wrapped_match_still_needs_the_box_to_point_at_it():
    # The floor from the no-box tests holds here too: without a constraint the
    # halves are two unrelated runs of page text, and joining them is a guess.
    ocr = _wrapped_page()
    (p,) = _place(
        ocr, VlmFinding("24 Stacey Dr Carrickalinga SA 5204", "ADDRESS")
    ).placements
    assert p.kind is None


# --------------------------------------------------------- tier 3 and the residue


def test_a_box_over_pixels_with_no_text_falls_back_to_model_geometry():
    # The logo case: a correctly boxed value the OCR engine cannot see at all.
    ocr = _page("statement of account")
    (p,) = _place(
        ocr, VlmFinding("Budget Direct", "ORGANIZATION", box=(600, 600, 800, 700))
    ).placements
    assert p.kind == "box" and p.spans == () and p.box is not None


def test_the_fallback_box_is_padded_for_the_tail():
    ocr = _page("statement of account")
    raw = (600, 600, 800, 700)
    (p,) = _place(
        ocr, VlmFinding("Budget Direct", "ORGANIZATION", box=raw)
    ).placements
    exact = denormalize(raw, _PAGE_W, _PAGE_H)
    assert p.box.left < exact.left and p.box.top < exact.top
    assert p.box.right > exact.right and p.box.bottom > exact.bottom


def test_the_fallback_box_absorbs_a_word_it_clips():
    # Where the box cuts through a word, the word's own exact box completes
    # it — over-painting a neighbour is recoverable, half a legible account
    # number is not.
    ocr = _page("acct 162-09711-4 closed")
    word = next(w for w in ocr.words if w.text == "162-09711-4")
    clipped = (
        round(word.box.left / _PAGE_W * 1000),
        round(word.box.top / _PAGE_H * 1000),
        round((word.box.left + word.box.width // 3) / _PAGE_W * 1000),
        round(word.box.bottom / _PAGE_H * 1000),
    )
    (p,) = _place(
        ocr, VlmFinding("something unreadable", "IDENTIFIER_GENERIC", box=clipped)
    ).placements
    assert p.kind == "box"
    assert p.box.right >= word.box.right


def test_a_box_over_unmatchable_words_still_falls_back_rather_than_vanishing():
    ocr = _page("acct 162-09711-4 closed")
    box = _box_over(ocr, "162-09711-4")
    (p,) = _place(
        ocr, VlmFinding("Q7ZZZ9WWWW", "IDENTIFIER_GENERIC", box=box)
    ).placements
    assert p.kind == "box"


def test_a_degenerate_box_is_reported_unplaced_not_painted():
    # Padding a zero-area box yields a small rectangle at an arbitrary spot.
    # Painting it would COUNT as a redaction while covering nothing, which is
    # a hidden leak — worse than an honest "could not place this".
    ocr = _page("statement of account")
    (p,) = _place(
        ocr, VlmFinding("Budget Direct", "ORGANIZATION", box=(0, 0, 0, 0))
    ).placements
    assert p.kind is None and p.box is None


def test_a_page_sized_box_does_not_license_page_wide_fuzzy_matching():
    # A box covering everything is no constraint at all, and fuzzy matching
    # under it would be exactly the global edit-distance search the design
    # rules out. Exact/squash still work; only the fuzzy tier is withdrawn.
    ocr = _page(*[f"row{i} acct 162-09711-4 closed" for i in range(12)])
    (p,) = _place(
        ocr,
        VlmFinding("162-097111-4", "IDENTIFIER_GENERIC", box=(0, 0, 1000, 1000)),
    ).placements
    assert p.kind != "fuzzy"


def test_the_three_outcomes_are_reported_separately():
    ocr = _page("paid Sergei Kulik today")
    result = _place(
        ocr,
        VlmFinding("Sergei Kulik", "PERSON"),
        VlmFinding("Budget Direct", "ORGANIZATION", box=(600, 600, 800, 700)),
        VlmFinding("nowhere at all", "PERSON"),
    )
    assert [p.finding.text for p in result.located] == ["Sergei Kulik"]
    assert [p.finding.text for p in result.box_only] == ["Budget Direct"]
    assert [p.finding.text for p in result.unlocated] == ["nowhere at all"]


# ------------------------------------------------------------------ geometry


@pytest.mark.parametrize(
    "box,expected",
    [
        ((0, 0, 1000, 1000), Box(0, 0, _PAGE_W, _PAGE_H)),
        ((0, 0, 500, 500), Box(0, 0, _PAGE_W // 2, _PAGE_H // 2)),
    ],
)
def test_denormalize_maps_model_space_onto_pixels(box, expected):
    assert denormalize(box, _PAGE_W, _PAGE_H) == expected


# ------------------------------------------------- borrowed: what the document
#                                                    knows, applied to one page


def _borrowed(ocr, *needles):
    return [
        (ocr.text[start:end], entity_type)
        for start, end, entity_type, _ in locate_borrowed(needles, ocr)
    ]


def test_a_borrowed_value_is_marked_at_every_occurrence():
    # The page's own findings claim one occurrence each; a document-wide value
    # has no such budget — every printing of it is PII.
    ocr = _page("Sergei Kulik paid", "Sergei Kulik again")
    assert _borrowed(ocr, ("Sergei Kulik", "PERSON")) == [
        ("Sergei Kulik", "PERSON"),
        ("Sergei Kulik", "PERSON"),
    ]


def test_a_borrowed_value_matches_case_insensitively():
    ocr = _page("SERGEI KULIK paid")
    assert _borrowed(ocr, ("Sergei Kulik", "PERSON")) == [
        ("SERGEI KULIK", "PERSON")
    ]


def test_a_short_borrowed_needle_does_not_match_inside_a_word():
    # Exact matching carries no length floor by design (Wu, Ng, NAB, ANZ are
    # real), which is safe when a box pins the match and unbounded when a
    # value is hunted document-wide.
    ocr = _page("Would Wu wonder")
    assert _borrowed(ocr, ("Wu", "PERSON")) == [("Wu", "PERSON")]


def test_the_word_edge_guard_does_not_apply_to_a_non_alphanumeric_edge():
    ocr = _page("call (02) 9999 1234 now")
    assert _borrowed(ocr, ("(02) 9999 1234", "PHONE_NUMBER")) == [
        ("(02) 9999 1234", "PHONE_NUMBER")
    ]


def test_a_borrowed_value_falls_back_to_squash_when_respaced():
    ocr = _page("account 014936 111873883 closed")
    assert _borrowed(ocr, ("014-936 111873883", "IDENTIFIER_GENERIC")) == [
        ("014936 111873883", "IDENTIFIER_GENERIC")
    ]


def test_squash_matching_keeps_its_length_floor_when_borrowed():
    # Squash collapses separators, so a short needle would match across word
    # boundaries — page-wide, with no box, that is unbounded.
    ocr = _page("ab cd ef")
    assert _borrowed(ocr, ("a-b", "PERSON")) == []


def test_two_needles_resolving_to_one_span_go_to_the_first():
    # needles arrive longest-first (Grouping.needles), so where two spellings
    # of a value land on exactly the same characters the wider one labels them.
    ocr = _page("account 014936 closed")
    assert _borrowed(
        ocr, ("014-936", "IDENTIFIER_GENERIC"), ("014936", "PERSON")
    ) == [("014936", "IDENTIFIER_GENERIC")]


def test_a_nested_borrowed_value_keeps_its_own_span():
    # Nothing needs suppressing here, unlike the box-guided path where each
    # finding must claim its own geometry: the two spans simply overlap and
    # PiiPipeline._merge_overlaps unions them into one replacement.
    ocr = _page("John Smith paid")
    assert _borrowed(ocr, ("John Smith", "PERSON"), ("John", "PERSON")) == [
        ("John", "PERSON"),
        ("John Smith", "PERSON"),
    ]


def test_a_borrowed_value_absent_from_the_page_yields_nothing():
    ocr = _page("nothing to see here")
    assert _borrowed(ocr, ("Sergei Kulik", "PERSON")) == []


def test_a_borrowed_value_is_found_where_the_page_wraps_it():
    # The other half of the 2026-08-13 specimen. The page prints the address
    # twice and wraps it both times; the model boxed one of them, and without
    # this the second printing stays legible in the output.
    ocr = _two_columns(
        ("Permitted use of car", "Usually parked at", "24 Stacey Dr"),
        ("Registration number", "", "Carrickalinga SA 5204"),
    )
    assert _borrowed(ocr, ("24 Stacey Dr Carrickalinga SA 5204", "ADDRESS")) == [
        ("24 Stacey Dr", "ADDRESS"),
        ("Carrickalinga SA 5204", "ADDRESS"),
    ]


def test_the_pieces_of_a_borrowed_wrapped_value_name_the_whole_of_it():
    # They are ONE value, so they must collect ONE placeholder — which the
    # span text alone cannot say. `full_value` is what carries it.
    ocr = _two_columns(
        ("Permitted use of car", "Usually parked at", "24 Stacey Dr"),
        ("Registration number", "", "Carrickalinga SA 5204"),
    )
    value = "24 Stacey Dr Carrickalinga SA 5204"
    assert [full for _, _, _, full in locate_borrowed([(value, "ADDRESS")], ocr)] == [
        value,
        value,
    ]


def test_a_borrowed_wrapped_match_must_stay_in_one_column():
    # Consecutive lines are not enough: the halves have to stand in the same
    # column, or the tier would join any two runs the page happens to print
    # above one another. Here they are in opposite cards.
    ocr = _two_columns(
        ("paid 24 Stacey Dr", "ref", "9911"),
        ("nothing here", "", "Carrickalinga SA 5204"),
    )
    assert _borrowed(ocr, ("24 Stacey Dr Carrickalinga SA 5204", "ADDRESS")) == []


def test_a_wrapped_piece_must_earn_its_place_in_the_needle():
    """A word of pure punctuation squashes to nothing, and every needle starts
    with the empty string — so a piece of one such word consumes NONE of the
    needle while still counting as a proper prefix, and the walk carries it to
    the next line where the real value completes the match. Left unguarded,
    EVERY needle claims whatever stray '-' or '?' sits on the line above it
    (2026-08-13: an insurance heading's hyphen painted as ORGANIZATION and a
    card's help icon as PERSON, both from unrelated needles)."""
    ocr = _page("ref -", "ref Sk Business Trust")
    assert _borrowed(ocr, ("Sk Business Trust", "ORGANIZATION")) == [
        ("Sk Business Trust", "ORGANIZATION")
    ]


def test_punctuation_after_a_wrapped_piece_does_not_break_the_match():
    # The guard above stops a piece at a word that spells nothing, which must
    # not cost the wrap itself: the piece is already complete by then.
    ocr = _two_columns(
        ("Permitted use of car", "Usually parked at", "24 Stacey Dr ,"),
        ("Registration number", "", "Carrickalinga SA 5204"),
    )
    assert _borrowed(ocr, ("24 Stacey Dr Carrickalinga SA 5204", "ADDRESS")) == [
        ("24 Stacey Dr", "ADDRESS"),
        ("Carrickalinga SA 5204", "ADDRESS"),
    ]


def test_a_wrapped_borrowed_value_is_additive_not_a_fallback():
    # Same reasoning as the fuzzy tier: a page carrying the value on one line
    # AND wrapped would otherwise find the contiguous one and leak the other.
    ocr = _two_columns(
        ("on file", "Postal address", "24 Stacey Dr Carrickalinga SA 5204"),
        ("Permitted use of car", "Usually parked at", "24 Stacey Dr"),
        ("Registration number", "", "Carrickalinga SA 5204"),
    )
    assert _borrowed(ocr, ("24 Stacey Dr Carrickalinga SA 5204", "ADDRESS")) == [
        ("24 Stacey Dr Carrickalinga SA 5204", "ADDRESS"),
        ("24 Stacey Dr", "ADDRESS"),
        ("Carrickalinga SA 5204", "ADDRESS"),
    ]


# --------------------------------- the tier-3 residue and the "NOT redacted"
#                                    line it used to cry wolf on


def test_an_unplaced_twin_of_a_box_painted_value_is_flagged_not_silenced():
    """The insurance-page case (2026-08-09): the model boxes a value OCR
    cannot read, so it paints from tier 3 — which leaves no char span. The
    model reports the same value a second time with no box of its own, so
    nothing marks that one redundant and it lands unplaced, although the
    pixels were painted.

    It stays counted as unplaced (the tool cannot tell whether the two are
    the same printing), but it is distinguishable, so the report can stop
    calling a painted value an outright leak."""
    ocr = _page("nothing readable here")
    result = locate_findings(
        [
            VlmFinding("Budget Direct", "ORGANIZATION",
                       box=(100, 100, 300, 200)),
            VlmFinding("Budget Direct", "ORGANIZATION"),
        ],
        ocr,
        (_PAGE_W, _PAGE_H),
    )
    assert [p.finding.text for p in result.box_only] == ["Budget Direct"]
    (unplaced,) = result.unlocated
    assert unplaced.value_painted_elsewhere is True


def test_an_unplaced_value_painted_nowhere_is_not_flagged():
    ocr = _page("nothing readable here")
    result = locate_findings(
        [VlmFinding("Sergei Kulik", "PERSON")], ocr, (_PAGE_W, _PAGE_H)
    )
    (unplaced,) = result.unlocated
    assert unplaced.value_painted_elsewhere is False


# ------------------------------------------- borrowed: fuzzy, and its guards


def test_a_truncated_field_is_matched_from_the_full_value():
    # The observed leak (2026-08-11): pii_map.json carried
    # 'sk business trust' -> PERSON_5 while 'SK BUSINESS TRUS' leaked. The
    # needle is a strict SUPERSTRING of what the page prints, so both certain
    # tiers miss it — statements truncate names to fit fixed-width fields.
    ocr = _page("ATF SK BUSINESS TRUS")
    assert _borrowed(ocr, ("SK BUSINESS TRUST", "PERSON")) == [
        ("SK BUSINESS TRUS", "PERSON")
    ]


def test_a_heavier_truncation_is_still_matched():
    ocr = _page("ATF SK BUSINESS TRU")
    assert _borrowed(ocr, ("SK BUSINESS TRUST", "PERSON")) == [
        ("SK BUSINESS TRU", "PERSON")
    ]


def test_ocr_damage_mid_value_is_matched():
    # 'b' read as '6' — not in the confusion table, so it costs a full 1.0 and
    # the budget absorbs it rather than the discount.
    ocr = _page("ATF SK 6USINESS TRUST")
    assert _borrowed(ocr, ("SK BUSINESS TRUST", "PERSON")) == [
        ("SK 6USINESS TRUST", "PERSON")
    ]


def test_a_short_needle_never_reaches_the_fuzzy_tier():
    # The floor is the guard, not the budget: 'sk' at any budget >= 1 would
    # paint over half the page.
    ocr = _page("sk so ok sky ask", "task desk")
    assert _borrowed(ocr, ("sk", "PERSON")) == [("sk", "PERSON")]


def test_fuzzy_runs_even_when_the_value_also_appears_exactly():
    # Additive, not a fallback. Were it an "else" tier, the exact hit on this
    # page would suppress it and the truncation would leak — which is exactly
    # how the observed specimen could survive.
    ocr = _page("SK BUSINESS TRUST holds", "ATF SK BUSINESS TRUS")
    assert _borrowed(ocr, ("SK BUSINESS TRUST", "PERSON")) == [
        ("SK BUSINESS TRUST", "PERSON"),
        ("SK BUSINESS TRUS", "PERSON"),
    ]


def test_a_different_account_number_is_not_matched():
    # An identifier's identity is its digits. The strict table prices a digit
    # read as another digit at infinity, and the identifier budget cap sits
    # below 2.0 so edit distance cannot route around it with a delete plus an
    # insert either — which it otherwise does, at exactly 2.0.
    ocr = _page("account 4936117499 closed")
    assert _borrowed(ocr, ("8936117499", "AU_BANK_ACCOUNT")) == []


def test_an_identifier_still_matches_through_a_glyph_confusion():
    # Cross-class damage is what the strict table is FOR: 'l' read for '1'
    # costs 0.25, so several of them fit under the cap.
    ocr = _page("account l23456789O closed")
    assert _borrowed(ocr, ("1234567890", "AU_BANK_ACCOUNT")) == [
        ("l23456789O", "AU_BANK_ACCOUNT")
    ]


def test_an_identifier_still_matches_through_a_one_character_truncation():
    ocr = _page("account 123456789 closed")
    assert _borrowed(ocr, ("1234567890", "AU_BANK_ACCOUNT")) == [
        ("123456789", "AU_BANK_ACCOUNT")
    ]


def test_a_digit_run_cannot_reach_an_amount_of_another_length():
    # The length filter does this one: a 10-char needle never tests a 6-char
    # run, which is the collision class the box constraint was introduced for.
    ocr = _page("total 3,074.33 paid")
    assert _borrowed(ocr, ("307433257", "AU_BANK_ACCOUNT")) == []


def test_a_fuzzy_match_never_takes_a_region_the_certain_tiers_own():
    ocr = _page("call (02) 9999 1234 now")
    assert _borrowed(ocr, ("(02) 9999 1234", "PHONE_NUMBER")) == [
        ("(02) 9999 1234", "PHONE_NUMBER")
    ]


def test_an_unrelated_value_of_the_same_length_is_not_matched():
    ocr = _page("paid to Brendan Whitfield today")
    assert _borrowed(ocr, ("SK BUSINESS TRUST", "PERSON")) == []

"""Geometric label attachment (`pii/core/layout.py`).

Model-free and OCR-free: a page is a handful of `PlacedWord`s laid out by hand,
assembled exactly as `linearize` assembles one (words joined by spaces, lines by
newlines), so these run in the fast suite and pin the geometry rather than the
recognizer.
"""

import pytest

from pii.core.engine import STRICT, Analyzer, Pattern, PatternRule
from pii.core.layout import PageLayout
from pii.core.linearization import PlacedWord, RecognizerInput
from pii.core.ocr import Box

LINE_HEIGHT = 20
CHAR_WIDTH = 10


def _page(rows, line_height=LINE_HEIGHT, gap=2.0):
    """`rows` is a list of lines, each a list of (x, text).

    Each (x, text) is one OCR detection REGION starting at x; a text with
    spaces in it is a multi-word region, which is what real OCR produces
    (`Account Number` comes back as one run, its value as another). `gap` is the
    line pitch in line heights, so a test can push lines apart and watch the
    `above` band stop reaching.
    """
    words, lines, offset = [], [], 0
    for line_no, row in enumerate(rows):
        top = int(line_no * line_height * gap)
        texts = []
        for x, run in row:
            region = Box(x, top, len(run) * CHAR_WIDTH, line_height)
            at = x
            for word in run.split(" "):
                box = Box(at, top, len(word) * CHAR_WIDTH, line_height)
                words.append(
                    PlacedWord(word, box, region, line_no, offset, offset + len(word))
                )
                at += (len(word) + 1) * CHAR_WIDTH
                offset += len(word) + 1  # the joining space, or the newline
            texts.append(run)
        lines.append(" ".join(texts))
    return RecognizerInput("\n".join(lines), tuple(words))


def _bands(source, needle, word_floor=4):
    start = source.text.index(needle)
    return {
        c.relation: c.text
        for c in PageLayout(source).contexts(start, start + len(needle), word_floor)
    }


# ------------------------------------------------------------------- bands


def test_the_left_band_is_the_same_line_in_reading_order():
    page = _page([[(0, "Account Number"), (400, "12345678")]])
    assert _bands(page, "12345678")["left"] == "Account Number"


def test_the_left_band_stops_at_its_own_line():
    """The half of the specimen that a line-bounded window alone would fix:
    `013795` sat at the start of its line, promoted by the line above."""
    page = _page([[(0, "Account Number"), (400, "4564")],
                  [(0, "013795"), (200, "Statement")]])
    assert _bands(page, "013795").get("left", "") == ""


def test_the_word_floor_is_what_keeps_another_column_out():
    """The other half of the specimen: `_rows` puts two columns on one
    assembled line, which is how `cheque` came to promote a phone number nine
    words away. No distance rule separates those cases (see the constants), so
    the word count is what does."""
    page = _page([[(0, "with your cheque to Group Card Services, Please call us on"),
                   (2000, "12345678")]])
    assert "cheque" not in _bands(page, "12345678", word_floor=3)["left"]
    assert _bands(page, "12345678", word_floor=3)["left"] == "call us on"


def test_the_above_band_holds_only_the_overhead_column():
    """The case that motivates two bands instead of one flat concatenation:
    flattened in reading order, `unrelated` would appear to sit between the
    label and its value."""
    page = _page([[(0, "unrelated text"), (400, "ABN:")],
                  [(0, "unrelated text"), (400, "12345")]])
    bands = _bands(page, "12345")
    assert bands["above"] == "ABN:"
    assert bands["left"] == "unrelated text"


def test_the_above_band_stops_after_two_line_heights():
    page = _page([[(400, "ABN:")], [(400, "filler")], [(400, "12345")]], gap=2.0)
    assert "ABN:" not in _bands(page, "12345").get("above", "")


def test_a_wrapped_label_still_assembles_in_the_above_band():
    """Why the vertical reach is two line heights and not one."""
    page = _page([[(400, "Australian Financial")],
                  [(400, "Services Licence")],
                  [(400, "285571")]], gap=1.0)
    assert _bands(page, "285571")["above"] == "Australian Financial Services Licence"


def test_the_vertical_reach_scales_with_the_line_height():
    """Measured in line heights, so one set of constants holds at any DPI."""
    for line_height in (10, 20, 80):
        page = _page([[(400, "ABN:")], [(400, "12345")]],
                     line_height=line_height, gap=2.0)
        assert _bands(page, "12345")["above"] == "ABN:", line_height


def test_a_span_with_no_geometry_gets_no_context():
    """A caller mixing sources is told nothing rather than told wrong."""
    page = _page([[(0, "Account"), (200, "12345678")]])
    assert PageLayout(page).contexts(10_000, 10_008, 3) == []


# ------------------------------------------------- through the analyzer


class _Labelled(PatternRule):
    entity = "TEST"
    context = ("abn", "account")
    patterns = (Pattern("digits", r"\d{5,}", 0.5, attach=STRICT),)


@pytest.mark.parametrize(
    "rows, expected",
    [
        # The label directly above its value, other text on both lines.
        ([[(0, "unrelated text"), (400, "ABN:")],
          [(0, "unrelated text"), (400, "12345")]], True),
        # The label above, but a value of its own already between them.
        ([[(0, "ABN:")], [(0, "99999")], [(0, "12345")]], False),
        # Left-adjacent with a filler word.
        ([[(0, "Account No."), (400, "12345")]], True),
        # Left-adjacent with a word of its own in between.
        ([[(0, "Account enquiries"), (400, "12345")]], False),
    ],
)
def test_strict_attachment_over_a_page(rows, expected):
    page = _page(rows, gap=1.0)
    found = Analyzer([_Labelled()]).analyze(page.text, 0.4, PageLayout(page))
    assert bool([d for d in found if page.text[d.start : d.end] == "12345"]) is expected


class _Phoneish(PatternRule):
    entity = "TEST"
    context = ("account", "enquir")
    patterns = (Pattern("digits", r"\d{5,}", 0.15),)


class _Grouped(PatternRule):
    entity = "TEST"
    context = ("account",)
    patterns = (Pattern("grouped", r"\b\d{2,6}(?:[ -]\d{1,6}){1,3}\b", 0.15),)


# ------------------------------------------------- nearest label wins


def test_the_nearest_label_wins_across_bands():
    """Left-then-above priority lets a bogus left label outrank a good one
    directly overhead. On the specimen page the value's own column carried
    `Enquiries` one line up while the line to its left carried a date range."""
    page = _page([[(0, "Account Statement"), (400, "Enquiries")],
                  [(0, "From 1 January 2022 to 30 June"), (400, "133174")]],
                 gap=1.0)
    found = Analyzer([_Phoneish()]).analyze(page.text, 0.4, PageLayout(page))
    hit = next(d for d in found if page.text[d.start:d.end] == "133174")
    assert hit.attachment.term == "Enquiries"
    assert hit.attachment.relation == "above"


def test_a_closer_left_label_still_beats_one_overhead():
    """Nearest-wins is a distance rule, not a preference for `above`."""
    page = _page([[(400, "Enquiries")],
                  [(0, "Account"), (200, "12345")]], gap=1.0)
    found = Analyzer([_Phoneish()]).analyze(page.text, 0.4, PageLayout(page))
    hit = next(d for d in found if page.text[d.start:d.end] == "12345")
    assert hit.attachment.relation == "left"
    assert hit.attachment.term == "Account"


# ------------------------------------------------- one value, one column


def test_a_match_straddling_a_column_is_rejected():
    """`linearize` joins every word with ONE space, so a column gap and a word
    space are the same character and no separator class can tell them apart.
    On the specimen page a date range and an enquiries phone matched as the
    account number `2022 133 174` across a 34-line-height gap."""
    page = _page([[(0, "Account to 30 June 2022"), (2000, "133 174")]])
    found = Analyzer([_Grouped()]).analyze(page.text, 0.4, PageLayout(page))
    assert [page.text[d.start:d.end] for d in found] == []


def test_an_ordinary_word_space_inside_a_value_is_kept():
    """The guard must not touch a value that is merely grouped: a printed space
    is about 0.8 line heights against the three the check allows."""
    page = _page([[(0, "Account"), (200, "133 174")]])
    found = Analyzer([_Grouped()]).analyze(page.text, 0.4, PageLayout(page))
    assert [page.text[d.start:d.end] for d in found] == ["133 174"]

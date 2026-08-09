"""OcrPage building (pii/core/ocr_page.py) — model-free: `build_page` is
pure, so hand-built rows exercise the line-box discipline directly.

Focus here is `_line_box`: a line's box CONTAINS the glyph ink, so it unions
the word boxes with their region boxes. Engine word boxes are inset from the
ink, so a box built from them alone slices the first and last glyph — which
is a leak, because that box is what the paint layer grows a run out to.
"""

from pii.core.ocr import Box
from pii.core.ocr_page import OcrFrame, OcrWord, build_page

_FRAME = OcrFrame(width=400, height=200, page=1, backend="test")

# One detection region [10, 20, 210, 44] whose word boxes are inset from it by
# 6px on the left and 4px on the right — the measured paddle fragment inset.
_REGION = Box(10, 20, 200, 24)
_WORDS = [("TFN", Box(16, 20, 44, 24)), ("123", Box(120, 20, 86, 24))]


def _row(region=_REGION, words=_WORDS):
    """One visual row whose words all came from `region` — the shape
    `_result_to_rows` produces."""
    return [(text, box, 90.0, region) for text, box in words]


class TestLineBoxContainsInk:
    def test_line_box_grows_to_the_region_box(self):
        # The inset fragments must not define the line box: it spans the whole
        # detection region, which is what contains the glyphs.
        (line,) = build_page([_row()], _FRAME).lines
        assert line.box == _REGION

    def test_line_box_contains_every_word_box(self):
        (line,) = build_page([_row()], _FRAME).lines
        for w in line.words:
            assert line.box.left <= w.box.left and w.box.right <= line.box.right
            assert line.box.top <= w.box.top and w.box.bottom <= line.box.bottom

    def test_row_spans_all_its_source_regions(self):
        # A banded visual row aggregates words from several detection regions;
        # the line box is the union of those regions, not of the words.
        left_region, right_region = Box(10, 20, 100, 24), Box(200, 20, 120, 24)
        row = [
            ("DATE", Box(16, 20, 40, 24), 90.0, left_region),
            ("AMOUNT", Box(206, 20, 100, 24), 90.0, right_region),
        ]
        (line,) = build_page([row], _FRAME).lines
        assert line.box == Box(10, 20, 310, 24)

    def test_glyph_tight_backend_is_unaffected(self):
        # No region geometry -> OcrWord.region falls back to the word box, so
        # the union is exactly the word extent (no silent growth).
        row = [("TFN", Box(16, 20, 44, 24), 90.0),
               ("123", Box(120, 20, 86, 24), 90.0)]
        (line,) = build_page([row], _FRAME).lines
        assert line.box == Box(16, 20, 190, 24)


class TestStaleRegionBox:
    """Paddle occasionally emits a region box that does NOT contain its own
    words (the ea9e056 footer case). Unioning — rather than taking the region
    alone — keeps such a region from pulling the line box in past its words."""

    def test_region_narrower_than_its_words_cannot_shrink_the_line_box(self):
        stale = Box(10, 20, 60, 24)  # right edge 70, well left of the words
        (line,) = build_page([_row(region=stale)], _FRAME).lines
        assert line.box.left == 10  # region's left edge still used
        assert line.box.right == 206  # but the words' extent wins on the right
        for w in line.words:
            assert line.box.left <= w.box.left and w.box.right <= line.box.right

    def test_line_box_is_never_narrower_than_the_word_union(self):
        for region in (_REGION, Box(10, 20, 60, 24), Box(100, 30, 20, 4)):
            (line,) = build_page([_row(region=region)], _FRAME).lines
            assert line.box.left <= min(b.left for _t, b in _WORDS)
            assert line.box.right >= max(b.right for _t, b in _WORDS)
            assert line.box.top <= min(b.top for _t, b in _WORDS)
            assert line.box.bottom >= max(b.bottom for _t, b in _WORDS)


class TestNoLineIsDropped:
    """A dropped line is unredacted PII, so every row carrying words becomes
    a line — and an empty row contributes nothing rather than an empty one."""

    def test_every_row_with_words_becomes_a_line(self):
        page = build_page([_row(), _row(region=Box(10, 60, 200, 24))], _FRAME)
        assert len(page.lines) == 2

    def test_empty_rows_are_skipped_without_disturbing_the_rest(self):
        page = build_page([[], _row(), []], _FRAME)
        assert [ln.text for ln in page.lines] == ["TFN 123"]


def test_word_region_defaults_to_its_own_box():
    word = OcrWord(text="TFN", box=Box(16, 20, 44, 24))
    assert word.region == word.box

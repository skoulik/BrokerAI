"""OcrPage builders (pii/core/ocr_page.py) — model-free: both builders are
pure, so hand-built parts exercise the line-box discipline directly.

Focus here is `_line_box`, the rule both builders share: a line's box CONTAINS
the glyph ink, so it unions the word boxes with their region boxes. Engine word
boxes are inset from the ink, so a box built from them alone slices the first
and last glyph — and the two layout backends disagreed on it, because
PP-Structure's normalized result carries no word fragments (its words are
interpolated across the whole region box) while the doclayout path passes
`return_word_box=True` and gets real, inset fragments.
"""

from pii.core.ocr import Box
from pii.core.ocr_page import (
    OcrBlock,
    OcrFrame,
    OcrWord,
    build_layout_page,
    build_page,
)

_FRAME = OcrFrame(width=400, height=200, page=1, backend="test")

# One detection region [10, 20, 210, 44] whose word boxes are inset from it by
# 6px on the left and 4px on the right — the measured paddle fragment inset.
_REGION = Box(10, 20, 200, 24)
_WORDS = [("TFN", Box(16, 20, 44, 24)), ("123", Box(120, 20, 86, 24))]


def _layout_page(lines, blocks=None):
    blocks = blocks if blocks is not None else [
        OcrBlock(id=0, kind="text", origin="detected",
                 box=Box(0, 0, 400, 200), reading_order=0, page_id=1)
    ]
    return build_layout_page(blocks, lines, _FRAME)


class TestLineBoxContainsInk:
    def test_layout_line_box_grows_to_the_region_box(self):
        # The inset fragments must not define the line box: it spans the whole
        # detection region, which is what contains the glyphs.
        (line,) = _layout_page([(_REGION, _WORDS, 90.0)]).lines
        assert line.box == _REGION

    def test_layout_line_box_contains_every_word_box(self):
        (line,) = _layout_page([(_REGION, _WORDS, 90.0)]).lines
        for w in line.words:
            assert line.box.left <= w.box.left and w.box.right <= line.box.right
            assert line.box.top <= w.box.top and w.box.bottom <= line.box.bottom

    def test_line_only_row_spans_all_its_source_regions(self):
        # A banded visual row aggregates words from several detection regions;
        # the line box is the union of those regions, not of the words.
        left_region, right_region = Box(10, 20, 100, 24), Box(200, 20, 120, 24)
        row = [
            ("DATE", Box(16, 20, 40, 24), 90.0, left_region),
            ("AMOUNT", Box(206, 20, 100, 24), 90.0, right_region),
        ]
        (line,) = build_page([row], _FRAME).lines
        assert line.box == Box(10, 20, 310, 24)
        # the synthetic block wrapping the row is the same rectangle
        assert line.box == build_page([row], _FRAME).blocks[0].box

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
        (line,) = _layout_page([(stale, _WORDS, 90.0)]).lines
        assert line.box.left == 10  # region's left edge still used
        assert line.box.right == 206  # but the words' extent wins on the right
        for w in line.words:
            assert line.box.left <= w.box.left and w.box.right <= line.box.right

    def test_line_box_is_never_narrower_than_the_word_union(self):
        for region in (_REGION, Box(10, 20, 60, 24), Box(100, 30, 20, 4)):
            (line,) = _layout_page([(region, _WORDS, 90.0)]).lines
            assert line.box.left <= min(b.left for _t, b in _WORDS)
            assert line.box.right >= max(b.right for _t, b in _WORDS)
            assert line.box.top <= min(b.top for _t, b in _WORDS)
            assert line.box.bottom >= max(b.bottom for _t, b in _WORDS)


class TestOrphanHandling:
    def test_orphan_line_box_matches_its_synthetic_block(self):
        # A line overlapping no detected block gets its own synthetic block;
        # both are the region-grown rectangle, so the overlay and the
        # containment geometry agree.
        far_block = [OcrBlock(id=0, kind="text", origin="detected",
                              box=Box(300, 150, 90, 40), reading_order=0,
                              page_id=1)]
        page = _layout_page([(_REGION, _WORDS, 90.0)], blocks=far_block)
        (line,) = page.lines
        block = page.block_of(line)
        assert block.origin == "synthetic"
        assert line.box == _REGION == block.box


def test_word_region_defaults_to_its_own_box():
    word = OcrWord(text="TFN", box=Box(16, 20, 44, 24))
    assert word.region == word.box

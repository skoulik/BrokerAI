"""PP-DocLayoutV3 adapter (pii/core/ocr_doclayout.py) — model-free: the
conversion is pure, so a captured real result pair exercises block building,
line->block containment, reading order and orphan handling without paddle.

_LAYOUT/_OCR are a faithful capture of PP-DocLayoutV3 + PP-OCRv6_medium run on
page 1 of the ANZ policy PDF at 150 dpi (2026-07-25) — the SAME page the
PP-Structure fixture uses, and its OCR half is byte-identical to that
fixture's, so the two backends' block treatment can be compared directly:
V3 reports 6 blocks (it splits the body paragraph in two and types the logo
`footer_image`) where PP-Structure reported 4 (one body block, `footer`).
"""

from collections import Counter

from pii.core.linearization import linearize
from pii.core.ocr import _to_box
from pii.core.ocr_doclayout import doclayout_result_to_page
from pii.core.ocr_page import OcrFrame

_FRAME = OcrFrame(width=1241, height=1754, page=1, backend="doclayout:v3",
                  tier="v6_medium")

# In model reading order — list position IS the order (see the adapter's
# module docstring); the `order` field is deliberately not captured because
# the adapter ignores it.
_LAYOUT = [
    {"label": "text", "score": 0.807, "coordinate": [846, 586, 1187, 659]},
    {"label": "text", "score": 0.766, "coordinate": [831, 785, 1186, 810]},
    {"label": "text", "score": 0.751, "coordinate": [1049, 833, 1184, 857]},
    {"label": "text", "score": 0.848, "coordinate": [142, 1228, 1148, 1276]},
    {"label": "text", "score": 0.839, "coordinate": [142, 1286, 1116, 1335]},
    {"label": "footer_image", "score": 0.683,
     "coordinate": [941, 1616, 1194, 1701]},
]

_OCR = {
    "rec_texts": [
        "MORTGAGE CREDIT",
        "REQUIREMENTS",
        "RETAIL CREDIT RISK CONFIDENTIAL",
        "8 APRIL 2024",
        'All contents contained in the Mortgage Credit Requirements is '
        'classified as "Confidential" in line with ANZ\'s Information Security',
        "Policy. It is subject to the information classification and "
        "security guidelines for internal documents.",
        "Provision of any part of the credit to an external audience "
        "requires the specific permission of Head of Retail Credit Risk (or an",
        "authorised delegate).",
        "ANZ",
    ],
    "rec_scores": [0.982, 1.0, 0.999, 0.998, 0.999, 0.997, 0.998, 1.0, 1.0],
    "rec_boxes": [
        [850, 588, 1184, 615],
        [908, 625, 1182, 652],
        [834, 786, 1182, 803],
        [1050, 833, 1181, 853],
        [144, 1226, 1146, 1250],
        [146, 1251, 910, 1271],
        [146, 1287, 1112, 1307],
        [145, 1310, 314, 1330],
        [938, 1617, 1117, 1706],
    ],
}


def _anz():
    return doclayout_result_to_page(_LAYOUT, _OCR, _FRAME)


def test_blocks_typed_and_ordered_by_list_position():
    page = _anz()
    assert [b.kind for b in page.blocks] == [
        "text", "text", "text", "text", "text", "footer_image"]
    assert all(b.origin == "detected" for b in page.blocks)
    # id == reading_order == list position: the model's own order, with no
    # label pushed to the end (the trap the `order` field would set).
    assert [b.reading_order for b in page.blocks] == [0, 1, 2, 3, 4, 5]
    assert [b.id for b in page.blocks] == [0, 1, 2, 3, 4, 5]


def test_block_conf_scaled_to_percent():
    page = _anz()
    assert [round(b.conf) for b in page.blocks] == [81, 77, 75, 85, 84, 68]


def test_every_line_lands_in_a_detected_block():
    page = _anz()
    assert len(page.lines) == 9
    assert all(page.block_of(ln).origin == "detected" for ln in page.lines)
    # V3 splits the body paragraph the way PP-Structure kept it whole: 2 lines
    # in block 3 and 2 in block 4 where PP-Structure reported one 4-line block.
    counts = Counter(ln.block_id for ln in page.lines)
    assert [counts[b.id] for b in page.blocks] == [2, 1, 1, 2, 2, 1]


def test_line_order_and_transitive_page():
    page = _anz()
    assert [ln.text for ln in page.lines] == _OCR["rec_texts"]
    for ln in page.lines:  # block_id total; page reachable through the block
        assert page.block_of(ln).page_id == page.frame.page


def test_logo_line_lands_in_the_footer_image_block():
    page = _anz()
    anz = next(ln for ln in page.lines if ln.text == "ANZ")
    assert page.block_of(anz).kind == "footer_image"


def test_words_carry_line_region_box():
    page = _anz()
    line = next(ln for ln in page.lines if ln.text == "MORTGAGE CREDIT")
    assert [w.text for w in line.words] == ["MORTGAGE", "CREDIT"]
    # Every word carries the detection region it came from, and the line box
    # spans it — word boxes are inset from the glyph ink, so a line box built
    # from them alone would slice the first and last glyph (ocr_page._line_box).
    assert all(w.region_box == _to_box(_OCR["rec_boxes"][0]) for w in line.words)
    assert line.box == _to_box(_OCR["rec_boxes"][0])


def test_feeds_linearize():
    ri = linearize(_anz())
    assert ri.text.startswith("MORTGAGE CREDIT")
    for w in ri.words:
        assert ri.text[w.char_start : w.char_end] == w.text


def test_orphan_line_gets_own_synthetic_block():
    layout = [{"label": "text", "score": 0.9,
               "coordinate": [90, 90, 210, 140]}]
    ocr = {
        "rec_texts": ["INSIDE", "STRAY"],
        "rec_scores": [0.9, 0.9],
        "rec_boxes": [[100, 100, 200, 130], [900, 20, 1000, 50]],
    }
    page = doclayout_result_to_page(layout, ocr, _FRAME)
    stray = next(ln for ln in page.lines if ln.text == "STRAY")
    stray_block = page.block_of(stray)
    assert stray_block.origin == "synthetic" and stray_block.kind == "unassigned"
    assert stray_block.reading_order == 1  # after the detected run
    inside = next(ln for ln in page.lines if ln.text == "INSIDE")
    assert page.block_of(inside).origin == "detected"


def test_table_block_keeps_its_flow_position():
    """A `table` block carries `order: None` in the raw result (paddlex blanks
    it for float labels) — ranking by that field would sort every statement
    table last. List position must win instead."""
    layout = [
        {"label": "text", "score": 0.9, "coordinate": [0, 0, 500, 50]},
        {"label": "table", "score": 0.9, "coordinate": [0, 100, 500, 200]},
        {"label": "text", "score": 0.9, "coordinate": [0, 300, 500, 350]},
    ]
    ocr = {
        "rec_texts": ["HEADER", "ROW", "FOOTER"],
        "rec_scores": [0.9, 0.9, 0.9],
        "rec_boxes": [[10, 10, 100, 40], [10, 120, 100, 150],
                      [10, 310, 100, 340]],
    }
    page = doclayout_result_to_page(layout, ocr, _FRAME)
    assert [ln.text for ln in page.lines] == ["HEADER", "ROW", "FOOTER"]
    assert page.blocks[1].kind == "table" and page.blocks[1].reading_order == 1


def test_missing_score_leaves_conf_none():
    layout = [{"label": "text", "coordinate": [0, 0, 100, 100]}]
    page = doclayout_result_to_page(layout, {}, _FRAME)
    assert page.blocks[0].conf is None


def test_empty_result():
    page = doclayout_result_to_page([], {}, _FRAME)
    assert page.lines == () and page.blocks == ()

"""Linearization behavior: build_page + linearize must reproduce the old
assemble() text/offsets and the span->box mapping exactly.

Mirrors test_ocr.py (the retired assemble-based contract), routed through
the new perception -> linearization seam. Pure, no OCR engine."""

from pii.core.linearization import linearize
from pii.core.ocr import Box
from pii.core.ocr_page import OcrFrame, build_page

_FRAME = OcrFrame(width=1000, height=1000, page=1)


def _box(left, top=0, width=10, height=10):
    return Box(left=left, top=top, width=width, height=height)


def _ri(rows):
    return linearize(build_page(rows, _FRAME))


def _two_lines():
    # "TFN: 123 456 782" / "John Smith"
    return _ri(
        [
            [
                ("TFN:", _box(0), 96.0),
                ("123", _box(20), 81.0),
                ("456", _box(40), 84.0),
                ("782", _box(60), 84.0),
            ],
            [
                ("John", _box(0, top=20), 90.0),
                ("Smith", _box(20, top=20), 90.0),
            ],
        ]
    )


def test_assemble_text_and_offsets():
    result = _two_lines()
    assert result.text == "TFN: 123 456 782\nJohn Smith"
    for w in result.words:
        assert result.text[w.char_start : w.char_end] == w.text
    assert [w.line for w in result.words] == [0, 0, 0, 0, 1, 1]


def test_boxes_exact_word():
    result = _two_lines()
    start = result.text.index("456")
    assert result.boxes_for_span(start, start + 3) == [_box(40)]


def test_boxes_partial_overlap_mid_word_both_ends():
    # Span "3 45" — entity boundaries fall inside both digit groups; both
    # words must still be painted (recall-first).
    result = _two_lines()
    start = result.text.index("3 45")
    boxes = result.boxes_for_span(start, start + 4)
    assert boxes == [_box(20, width=30)]  # union of the two word boxes


def test_boxes_multiword_entity_unions_gaps():
    result = _two_lines()
    start = result.text.index("123")
    assert result.boxes_for_span(start, start + 11) == [_box(20, width=50)]


def test_boxes_span_across_lines_one_box_per_line():
    result = _two_lines()
    start = result.text.index("782")
    boxes = result.boxes_for_span(start, len(result.text))
    assert boxes == [_box(60), _box(0, top=20, width=30)]


def test_boxes_empty_span():
    result = _two_lines()
    assert result.boxes_for_span(5, 5) == []


# --- painted_boxes_for_span: grow the run out to the detection-line box so
# no glyph fringe survives (fragment word boxes are inset from the ink). ---


def _inset_line():
    region = _box(0, top=0, width=100, height=20)
    return _ri(
        [
            [
                ("OLGA", _box(15, top=2, width=30, height=16), 90.0, region),
                ("KULIK", _box(55, top=2, width=30, height=16), 90.0, region),
            ]
        ]
    )


def test_painted_grows_run_to_region_box():
    result = _inset_line()
    start = result.text.index("OLGA")
    end = start + len("OLGA KULIK")
    assert result.boxes_for_span(start, end) == [_box(15, top=2, width=70, height=16)]
    assert result.painted_boxes_for_span(start, end) == [
        _box(0, top=0, width=100, height=20)
    ]


def _inset_mid():
    region = _box(0, top=0, width=200, height=20)

    def w(left, word, width):
        return (word, _box(left, top=2, width=width, height=16), 90.0, region)

    return _ri(
        [[w(5, "FROM", 25), w(45, "OLGA", 30), w(85, "KULIK", 30), w(130, "PAID", 25)]]
    )


def test_painted_midline_clamps_to_neighbour_midpoint():
    result = _inset_mid()
    start = result.text.index("OLGA")
    (box,) = result.painted_boxes_for_span(start, start + len("OLGA KULIK"))
    assert box.left == (30 + 45) // 2
    assert box.right == (130 + 115) // 2
    assert box.left > 30 and box.right < 130


def _run_past_region_right():
    region = _box(0, top=0, width=100, height=20)  # right = 100

    def w(left, word, width):
        return (word, _box(left, top=2, width=width, height=16), 90.0, region)

    return _ri([[w(80, "FAX", 15), w(110, "O3", 20), w(135, "9708", 25)]])


def test_painted_run_past_stale_region_right_no_negative_width():
    result = _run_past_region_right()
    start = result.text.index("O3")
    (box,) = result.painted_boxes_for_span(start, start + len("O3 9708"))
    assert box.width >= 0 and box.height >= 0
    assert box.left <= 110 and box.right >= 160
    assert box.left > 95
    assert box.left == (95 + 110) // 2
    assert box.right == 160


def _run_before_region_left():
    region = _box(200, top=0, width=100, height=20)  # left = 200

    def w(left, word, width):
        return (word, _box(left, top=2, width=width, height=16), 90.0, region)

    return _ri([[w(150, "O3", 20), w(180, "9708", 25), w(230, "FAX", 15)]])


def test_painted_run_before_stale_region_left_no_negative_width():
    result = _run_before_region_left()
    start = result.text.index("O3")
    (box,) = result.painted_boxes_for_span(start, start + len("O3 9708"))
    assert box.width >= 0 and box.height >= 0
    assert box.left <= 150 and box.right >= 205
    assert box.right < 230
    assert box.left == 150
    assert box.right == (230 + 205) // 2


def test_painted_without_region_matches_boxes_for_span():
    # 3-tuple rows supply no region geometry (region falls back to the word
    # box), so painting must not differ from boxes_for_span.
    result = _two_lines()
    start = result.text.index("123")
    assert result.painted_boxes_for_span(start, start + 11) == result.boxes_for_span(
        start, start + 11
    )
    start = result.text.index("782")
    assert result.painted_boxes_for_span(
        start, len(result.text)
    ) == result.boxes_for_span(start, len(result.text))

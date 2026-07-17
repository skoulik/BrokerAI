"""OCR adapter: assembly offsets and span->box mapping.

All pure (no OCR engine): the interchange (`assemble`, `OcrResult`) is
engine-neutral. Real-engine end-to-end coverage lives in the paddle worker
tests (test_ocr_worker.py, gpu-marked)."""

from pii.core.ocr import Box, assemble


def _box(left, top=0, width=10, height=10):
    return Box(left=left, top=top, width=width, height=height)


def _two_lines():
    # "TFN: 123 456 782" / "John Smith"
    return assemble(
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
    # words must still be painted (the presidio-image-redactor substring
    # check silently skipped this case).
    result = _two_lines()
    start = result.text.index("3 45")
    boxes = result.boxes_for_span(start, start + 4)
    assert boxes == [_box(20, width=30)]  # union of the two word boxes


def test_boxes_multiword_entity_unions_gaps():
    # "123 456 782" — one rectangle covering the inter-word gaps, not
    # three with readable pixels between them.
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

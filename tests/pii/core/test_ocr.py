"""OCR adapter: assembly offsets, span->box mapping, Tesseract glue.

The assembly/mapping tests are pure (no Tesseract); the end-to-end test
needs the system Tesseract binary and a TrueType font and is skipped when
either is missing.
"""

import shutil
from pathlib import Path

import pytest

from pii.core.ocr import Box, assemble, ocr_image, _lines_from_tesseract

_ARIAL = Path(r"C:\Windows\Fonts\arial.ttf")


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


def test_lines_from_tesseract_drops_structural_and_empty_rows():
    data = {
        "text": ["", "TFN:", " ", "123"],
        "conf": [-1, 96, 95, 81],
        "left": [0, 30, 50, 60],
        "top": [0, 5, 5, 5],
        "width": [200, 18, 4, 16],
        "height": [40, 8, 8, 8],
        "page_num": [1, 1, 1, 1],
        "block_num": [1, 1, 1, 1],
        "par_num": [0, 1, 1, 1],
        "line_num": [0, 1, 1, 1],
    }
    lines = _lines_from_tesseract(data)
    assert lines == [
        [("TFN:", _box(30, top=5, width=18, height=8), 96.0),
         ("123", _box(60, top=5, width=16, height=8), 81.0)],
    ]


def test_lines_from_tesseract_offset_subtracted_and_clamped():
    data = {
        "text": ["Hi"],
        "conf": [90],
        "left": [10],
        "top": [30],
        "width": [20],
        "height": [10],
        "page_num": [1],
        "block_num": [1],
        "par_num": [1],
        "line_num": [1],
    }
    lines = _lines_from_tesseract(data, offset=25)
    assert lines == [[("Hi", Box(left=0, top=5, width=20, height=10), 90.0)]]


def _tesseract_available() -> bool:
    if shutil.which("tesseract"):
        return True
    return Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe").exists()


@pytest.mark.skipif(
    not (_tesseract_available() and _ARIAL.exists()),
    reason="needs system Tesseract and arial.ttf",
)
def test_ocr_image_end_to_end():
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(_ARIAL), 32)
    img = Image.new("RGB", (600, 100), "white")
    ImageDraw.Draw(img).text((40, 30), "TFN: 123 456 782", font=font, fill="black")

    result = ocr_image(img)
    assert "123 456 782" in result.text

    start = result.text.index("123 456 782")
    boxes = result.boxes_for_span(start, start + 11)
    assert len(boxes) == 1
    # Boxes are in ORIGINAL image coordinates despite edge padding: the
    # digits start right of the "TFN: " label and sit in the drawn band.
    box = boxes[0]
    assert 40 < box.left < 200
    assert 20 < box.top < 80
    assert box.right <= 600 and box.bottom <= 100

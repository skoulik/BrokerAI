"""Image stripping: painting, placeholder consistency, OCR round-trip.

Painting tests run on constructed OcrResults (no Tesseract); the
end-to-end test needs the system binary + arial.ttf and self-skips.
"""

from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from pii.core.image_mode import _grow, strip_from_ocr, strip_image
from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box, assemble

RED = (255, 0, 0)

_ARIAL = Path(r"C:\Windows\Fonts\arial.ttf")
_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


def _colors(image, box):
    region = image.crop((box.left, box.top, box.right, box.bottom))
    return {color for _, color in region.getcolors(box.width * box.height)}


def test_strip_from_ocr_paints_over_pii_pixels(pipeline):
    email_box = Box(left=60, top=20, width=120, height=14)
    img = Image.new("RGB", (300, 60), "white")
    ImageDraw.Draw(img).rectangle(
        (email_box.left, email_box.top, email_box.right, email_box.bottom),
        fill=RED,
    )
    ocr = assemble(
        [
            [
                ("Pay", Box(10, 20, 30, 14), 90.0),
                ("olga@example.com", email_box, 90.0),
                ("now", Box(200, 20, 30, 14), 90.0),
            ]
        ]
    )
    pmap = PseudonymMap()
    result = strip_from_ocr(img, ocr, pipeline, pmap)

    assert [r.entity_type for r in result.spans] == ["EMAIL_ADDRESS"]
    # The email's pixels are gone...
    assert RED not in _colors(result.image, email_box)
    # ...non-PII regions are untouched...
    assert _colors(result.image, Box(200, 20, 30, 14)) == {(255, 255, 255)}
    # ...the input image was not mutated, and the mapping was allocated.
    assert RED in _colors(img, email_box)
    assert pmap.placeholder_for("EMAIL_ADDRESS", "olga@example.com") == "EMAIL_1"


def test_strip_from_ocr_consistent_placeholder_across_lines(pipeline):
    boxes = [Box(10, 10, 120, 12), Box(10, 40, 120, 12)]
    img = Image.new("RGB", (200, 70), "white")
    for b in boxes:
        ImageDraw.Draw(img).rectangle((b.left, b.top, b.right, b.bottom), fill=RED)
    ocr = assemble(
        [
            [("olga@example.com", boxes[0], 90.0)],
            [("olga@example.com", boxes[1], 90.0)],
        ]
    )
    pmap = PseudonymMap()
    result = strip_from_ocr(img, ocr, pipeline, pmap)

    assert len(result.spans) == 2
    assert len(pmap) == 1  # one placeholder, both occurrences
    for b in boxes:
        assert RED not in _colors(result.image, b)


def test_grow_clamps_to_image_bounds():
    img = Image.new("RGB", (100, 50))
    grown = _grow(Box(0, 0, 10, 10), 2, img)
    assert grown == Box(left=0, top=0, width=12, height=12)
    grown = _grow(Box(95, 45, 5, 5), 2, img)
    assert grown == Box(left=93, top=43, width=7, height=7)


@pytest.mark.skipif(
    not (_TESSERACT.exists() and _ARIAL.exists()),
    reason="needs system Tesseract and arial.ttf",
)
def test_strip_image_end_to_end_tfn_unreadable(pipeline):
    from PIL import ImageFont

    from pii.core.ocr import ocr_image

    font = ImageFont.truetype(str(_ARIAL), 32)
    img = Image.new("RGB", (700, 120), "white")
    ImageDraw.Draw(img).text(
        (40, 40), "TFN: 123 456 782", font=font, fill="black"
    )

    pmap = PseudonymMap()
    result = strip_image(img, pipeline, pmap)

    assert [r.entity_type for r in result.spans] == ["AU_TFN"]
    assert "123 456 782" in result.ocr.text
    # The redacted image no longer OCRs to the TFN digits.
    reread = ocr_image(result.image).text
    assert "456" not in reread
    assert "782" not in reread

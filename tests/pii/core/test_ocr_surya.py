"""Surya 2 adapter (pii/core/ocr_surya.py).

The conversion pipeline (HTML flatten, line budget, page->OcrResult) is
exercised model-free with duck-typed fake blocks — `page_to_ocr` never
imports surya. One gpu+slow test drives the real stack end-to-end
(detection model + llama-server VLM; skipped when llama-server is not on
PATH).
"""

import shutil
from types import SimpleNamespace

import pytest
from PIL import Image

from pii.core.ocr import Box, get_ocr
from pii.core.ocr_surya import (
    _flatten_html,
    _line_budget,
    _split_line,
    page_to_ocr,
)


def _block(html, poly=((10, 20), (210, 20), (210, 44), (10, 44)),
           conf=0.9, skipped=False, error=False):
    return SimpleNamespace(
        polygon=[list(p) for p in poly], html=html, confidence=conf,
        skipped=skipped, error=error, label="Text", raw_label="Text",
    )


def _page(*blocks):
    return SimpleNamespace(blocks=list(blocks), image_bbox=[0, 0, 800, 600])


class TestFlattenHtml:
    def test_plain_paragraph(self):
        assert _flatten_html("<p>BSB 062-000 Account 1234</p>") == \
            "BSB 062-000 Account 1234"

    def test_inline_tags_do_not_split_identifiers(self):
        # A mid-word <b> must not break the token for value matching.
        assert _flatten_html("<p>AC<b>N</b> 004 085 616</p>") == \
            "ACN 004 085 616"

    def test_entities_unescaped(self):
        assert _flatten_html("<p>SMITH &amp; CO</p>") == "SMITH & CO"

    def test_br_and_block_boundaries_become_spaces(self):
        assert _flatten_html("<p>TFN</p><p>565 431 023</p>") == \
            "TFN 565 431 023"
        assert _flatten_html("first<br>second") == "first second"

    def test_empty_and_whitespace(self):
        assert _flatten_html("") == ""
        assert _flatten_html("<p>   </p>") == ""

    def test_pipe_separators_stripped(self):
        # Residual table-izing: the VLM separates perceived columns with
        # literal pipes; standalone pipes are artifacts, not content.
        assert _flatten_html("<p>PAYID | PAYMENT | FROM | ERIC | MOORE</p>") \
            == "PAYID PAYMENT FROM ERIC MOORE"

    def test_cross_script_digit_homoglyphs_folded(self):
        # Observed live: the VLM emitted U+06F5 (Arabic-Indic five) for a
        # clean ASCII '5' — visually identical, breaks value matching.
        assert _flatten_html("<p>TFN 56۵ 431 023</p>") == \
            "TFN 565 431 023"
        # Devanagari and fullwidth digits fold too; letters untouched.
        assert _flatten_html("<p>१२３ ABC</p>") == "123 ABC"


class TestLineBudget:
    def test_floor_and_rounding(self):
        assert _line_budget(0) == 50
        assert _line_budget(900) == 50   # ~30 tokens -> floor
        assert _line_budget(3000) == 100
        assert _line_budget(3000) % 50 == 0


class TestPageToOcr:
    def test_happy_path_words_and_boxes(self):
        page = _page(_block("<p>Payment to JOHN CITIZEN</p>"))
        result = page_to_ocr(page)
        assert result.text == "Payment to JOHN CITIZEN"
        assert [w.text for w in result.words] == \
            ["Payment", "to", "JOHN", "CITIZEN"]
        # Interpolated boxes stay inside the line box, ordered left-to-right
        lefts = [w.box.left for w in result.words]
        assert lefts == sorted(lefts)
        assert all(10 <= w.box.left and w.box.right <= 210
                   for w in result.words)
        # boxes_for_span over the name covers exactly the name's words
        start = result.text.index("JOHN")
        boxes = result.boxes_for_span(start, start + len("JOHN CITIZEN"))
        assert len(boxes) == 1  # one line -> one unioned box

    def test_line_confidence_scaled_to_words(self):
        page = _page(_block("<p>one two</p>", conf=0.75))
        assert {w.conf for w in page_to_ocr(page).words} == {75.0}

    def test_rows_band_side_by_side_lines(self):
        # Two detection regions on the same visual row assemble as one line
        left = _block("<p>Date</p>", poly=((0, 0), (80, 0), (80, 20), (0, 20)))
        right = _block("<p>Amount</p>",
                       poly=((400, 2), (500, 2), (500, 22), (400, 22)))
        result = page_to_ocr(_page(left, right))
        assert result.text == "Date Amount"
        assert "\n" not in result.text

    def test_separate_rows_are_separate_lines(self):
        top = _block("<p>row one</p>", poly=((0, 0), (100, 0), (100, 20), (0, 20)))
        bottom = _block("<p>row two</p>",
                        poly=((0, 60), (100, 60), (100, 80), (0, 80)))
        result = page_to_ocr(_page(top, bottom))
        assert result.text == "row one\nrow two"

    def test_skipped_and_blank_blocks_dropped(self):
        page = _page(
            _block("", skipped=True),
            _block("<p></p>"),
            _block("<p>kept</p>"),
        )
        assert page_to_ocr(page).text == "kept"

    def test_error_block_raises(self):
        page = _page(_block("<p>fine</p>"), _block("", error=True))
        with pytest.raises(RuntimeError, match="failed on 1 line"):
            page_to_ocr(page)

    def test_empty_page(self):
        result = page_to_ocr(_page())
        assert result.text == ""
        assert result.words == []


class TestSplitLine:
    def _line_image(self, clusters, width=600, height=30):
        """White image with black ink clusters at the given (x0, x1) runs."""
        img = Image.new("RGB", (width, height + 20), "white")
        px = img.load()
        for x0, x1 in clusters:
            for x in range(x0, x1):
                for y in range(14, 14 + height - 8):
                    px[x, y] = (0, 0, 0)
        return img

    def test_wide_gutter_splits(self):
        # Two clusters, 200 px apart on a 30 px line -> gutter >> 1.5x height
        img = self._line_image([(10, 120), (320, 480)])
        segs = _split_line(img, Box(0, 10, 600, 30))
        assert len(segs) == 2
        assert segs[0].left <= 10 and segs[0].right >= 118
        assert segs[1].left <= 320 and segs[1].right >= 478

    def test_word_gaps_do_not_split(self):
        # 12 px gaps (~0.4x height) are word spacing, not gutters
        img = self._line_image([(10, 60), (72, 130), (142, 200)])
        segs = _split_line(img, Box(0, 10, 600, 30))
        assert len(segs) == 1
        # ...and the single segment is trimmed to the inked extent
        assert segs[0].left <= 10 and 198 <= segs[0].right <= 210

    def test_blank_line_yields_nothing(self):
        img = Image.new("RGB", (300, 40), "white")
        assert _split_line(img, Box(0, 5, 300, 30)) == []


def test_get_ocr_surya_resolves_without_importing_surya():
    import sys

    fn = get_ocr("surya")
    assert callable(fn)
    assert "surya" not in sys.modules  # lazy: resolving must not load it


@pytest.mark.gpu
@pytest.mark.slow
def test_real_surya_end_to_end():
    """Real detection model + llama-server VLM: rendered text in, readable
    OcrResult with sane word boxes out."""
    if shutil.which("llama-server") is None:
        pytest.skip("needs llama-server on PATH")
    from pathlib import Path

    from PIL import ImageDraw, ImageFont

    arial = Path(r"C:\Windows\Fonts\arial.ttf")
    if not arial.exists():
        pytest.skip("needs arial.ttf")
    img = Image.new("RGB", (700, 110), "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(arial), 32)
    draw.text((40, 15), "TFN 565 431 023", font=font, fill="black")
    draw.text((40, 60), "Payment to JOHN CITIZEN", font=font, fill="black")

    result = get_ocr("surya")(img)
    assert "565 431 023" in result.text
    assert "JOHN CITIZEN" in result.text
    start = result.text.index("565")
    boxes = result.boxes_for_span(start, start + len("565 431 023"))
    assert boxes, "span must map to pixel boxes"
    assert all(b.width > 0 and b.height > 0 for b in boxes)

"""PaddleOCR adapter conversion (pii/core/ocr_paddle.py) — model-free:
`result_to_ocr` is pure, so fake PaddleOCR result dicts exercise the
line->word normalization without paddle installed or imported."""

import pytest

from pii.core.ocr import get_ocr
from pii.core.ocr_paddle import result_to_ocr


def _result(texts, boxes, scores, words=None, word_boxes=None):
    d = {"rec_texts": texts, "rec_boxes": boxes, "rec_scores": scores}
    if words is not None:
        d["text_word"] = words
        d["text_word_boxes"] = word_boxes
    return d


class TestGetOcr:
    def test_paddle_is_default(self):
        # Tesseract retired 2026-07-17: paddle is the only engine, the
        # default backend, and the old name is now an unknown backend.
        assert callable(get_ocr())
        with pytest.raises(ValueError):
            get_ocr("tesseract")

    def test_paddle_resolves_without_importing_paddle(self):
        fn = get_ocr("paddle")
        assert callable(fn)

    def test_paddle_tier_selection(self):
        assert callable(get_ocr("paddle:v5_server"))
        assert callable(get_ocr("paddle:v6_medium"))

    def test_unknown_paddle_tier_raises(self):
        with pytest.raises(ValueError):
            get_ocr("paddle:v7_giga")

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError):
            get_ocr("tessseract")


class TestResultToOcr:
    def test_lines_words_confs(self):
        ocr = result_to_ocr(_result(
            texts=["TFN 123", "BSB 999"],
            boxes=[[20, 10, 120, 30], [20, 60, 120, 80]],
            scores=[0.94, 0.5],
        ))
        assert ocr.text == "TFN 123\nBSB 999"
        assert [w.text for w in ocr.words] == ["TFN", "123", "BSB", "999"]
        assert [round(w.conf) for w in ocr.words] == [94, 94, 50, 50]
        assert [w.line for w in ocr.words] == [0, 0, 1, 1]

    def test_same_row_regions_join_one_line_left_to_right(self):
        # detection split one visual row into two regions, listed
        # right-region-first — assembly must re-order geometrically
        ocr = result_to_ocr(_result(
            texts=["AMOUNT", "DATE PARTICULARS"],
            boxes=[[400, 10, 500, 30], [20, 10, 250, 30]],
            scores=[0.9, 0.9],
        ))
        assert ocr.text == "DATE PARTICULARS AMOUNT"
        assert [w.line for w in ocr.words] == [0, 0, 0]

    def test_stacked_regions_stay_separate_lines(self):
        ocr = result_to_ocr(_result(
            texts=["second", "first"],
            boxes=[[20, 60, 120, 80], [20, 10, 120, 30]],
            scores=[0.9, 0.9],
        ))
        assert ocr.text == "first\nsecond"

    def test_merged_fragments_map_word_boxes(self):
        # the verified quirk: fragments "TFN123" / " " / "456" against
        # line text "TFN 123 456" — boxes come from char-stream overlap,
        # tokens always from the line text
        ocr = result_to_ocr(_result(
            texts=["TFN 123 456"],
            boxes=[[20, 10, 320, 30]],
            scores=[0.9],
            words=[["TFN123", " ", "456"]],
            word_boxes=[[[20, 10, 200, 30], [200, 10, 210, 30],
                         [210, 10, 320, 30]]],
        ))
        assert [w.text for w in ocr.words] == ["TFN", "123", "456"]
        tfn, one23, four56 = ocr.words
        assert (tfn.box.left, tfn.box.right) == (20, 200)
        assert (one23.box.left, one23.box.right) == (20, 200)
        assert (four56.box.left, four56.box.right) == (210, 320)

    def test_fragment_mismatch_falls_back_to_interpolation(self):
        # fragment chars disagree with the line text -> whole line
        # interpolates over the line box
        ocr = result_to_ocr(_result(
            texts=["AB CD"],
            boxes=[[0, 10, 100, 30]],
            scores=[0.9],
            words=[["ABX"]],
            word_boxes=[[[0, 10, 50, 30]]],
        ))
        ab, cd = ocr.words
        assert ab.box.left < cd.box.left
        assert ab.box.right <= cd.box.left + 1
        assert cd.box.right <= 100

    def test_no_word_data_interpolates(self):
        ocr = result_to_ocr(_result(
            texts=["one two"],
            boxes=[[0, 10, 140, 30]],
            scores=[0.9],
        ))
        one, two = ocr.words
        assert [w.text for w in ocr.words] == ["one", "two"]
        assert one.box.left == 0
        assert two.box.left > one.box.right - 2
        assert two.box.right <= 140

    def test_polys_when_rec_boxes_missing(self):
        ocr = result_to_ocr({
            "rec_texts": ["hi"],
            "rec_polys": [[(20, 10), (120, 10), (120, 30), (20, 30)]],
            "rec_scores": [1.0],
        })
        (word,) = ocr.words
        assert word.box.left == 20
        assert word.box.right == 120

    def test_empty_result(self):
        ocr = result_to_ocr({"rec_texts": [], "rec_scores": []})
        assert ocr.text == ""
        assert ocr.words == []

    def test_boxes_for_span_works_through_adapter(self):
        ocr = result_to_ocr(_result(
            texts=["TFN 123 456"],
            boxes=[[20, 10, 320, 30]],
            scores=[0.9],
        ))
        start = ocr.text.index("123")
        boxes = ocr.boxes_for_span(start, start + len("123 456"))
        assert len(boxes) == 1  # same line unions into one box
        assert boxes[0].right <= 320

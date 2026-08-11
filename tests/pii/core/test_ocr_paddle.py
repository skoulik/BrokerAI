"""PaddleOCR adapter conversion (pii/core/ocr_paddle.py) — model-free:
`result_to_page` is pure, so fake PaddleOCR result dicts exercise the
line->word normalization without paddle installed or imported.

Assertions about assembled text go through `linearize`, because that string
is what the locator searches and what the recognizers see."""

import pytest

from pii.core.linearization import linearize
from pii.core.ocr import get_ocr_page
from pii.core.ocr_page import OcrFrame
from pii.core.ocr_paddle import result_to_page

_FRAME = OcrFrame(width=1000, height=1000, page=1,
                  backend="paddle", tier="v6_medium")


def _result(texts, boxes, scores, words=None, word_boxes=None):
    d = {"rec_texts": texts, "rec_boxes": boxes, "rec_scores": scores}
    if words is not None:
        d["text_word"] = words
        d["text_word_boxes"] = word_boxes
    return d


def _page(result):
    return result_to_page(result, _FRAME)


def _text(result):
    return linearize(_page(result)).text


class TestGetOcrPage:
    """get_ocr_page resolves a backend to an (image) -> OcrPage callable
    without loading any engine (wheel-selected transport; the engine loads
    only when the callable is invoked)."""

    def test_default_is_the_default_tier(self):
        assert get_ocr_page.__defaults__ == ("paddle",)
        assert callable(get_ocr_page())

    def test_every_advertised_backend_resolves(self):
        from pii.core.ocr import OCR_PAGE_BACKENDS

        for backend in OCR_PAGE_BACKENDS:
            assert callable(get_ocr_page(backend))

    def test_retired_backend_names_raise(self):
        # tesseract/surya (2026-07-17) and the layout backends (2026-08-09)
        # are gone; their names must fail loudly, not resolve to something
        # else that silently changes what gets painted.
        for backend in ("tesseract", "surya", "doclayout", "doclayout:v3",
                        "ppstructure"):
            with pytest.raises(ValueError):
                get_ocr_page(backend)

    def test_unknown_paddle_tier_raises(self):
        with pytest.raises(ValueError):
            get_ocr_page("paddle:v7_giga")

    @pytest.mark.parametrize("backend,spec", [
        ("paddle", "v6_medium"),
        ("paddle:v6_medium", "v6_medium"),
        ("paddle:v5_server", "v5_server"),
    ])
    def test_tier_binds_in_process_on_either_wheel(self, backend, spec,
                                                   monkeypatch):
        """`get_ocr_page` binds the tier and runs in-process — on the GPU
        wheel too, since the worker subprocess was retired 2026-08-09 (the
        pipeline no longer imports torch, so there is nothing to isolate).
        The tier strings are the contract; a typo would surface only under a
        real engine, so pin them here."""
        from pii.core import ocr_paddle

        monkeypatch.setattr(ocr_paddle, "_gpu_wheel", lambda: True)
        bound = get_ocr_page(backend)
        assert bound.func is ocr_paddle.ocr_page_paddle
        assert bound.keywords == {"tier": spec}


class TestRowBanding:
    """Detection regions carry no reading order, so `_rows` bands them into
    visual rows by y-centre. Load-bearing, not cosmetic: a label and its value
    in two side-by-side regions must land on ONE assembled line, or context
    promotion never reaches the value."""

    def test_lines_and_confidence(self):
        page = _page(_result(
            texts=["TFN 123", "BSB 999"],
            boxes=[[20, 10, 120, 30], [20, 60, 120, 80]],
            scores=[0.94, 0.5],
        ))
        assert [ln.text for ln in page.lines] == ["TFN 123", "BSB 999"]
        # conf is per LINE — paddle scores regions, not words
        assert [round(ln.conf) for ln in page.lines] == [94, 50]

    def test_same_row_regions_join_one_line_left_to_right(self):
        # detection split one visual row into two regions, listed
        # right-region-first — assembly must re-order geometrically
        result = _result(
            texts=["AMOUNT", "DATE PARTICULARS"],
            boxes=[[400, 10, 500, 30], [20, 10, 250, 30]],
            scores=[0.9, 0.9],
        )
        assert _text(result) == "DATE PARTICULARS AMOUNT"
        assert len(_page(result).lines) == 1

    def test_label_and_value_columns_reach_one_line(self):
        # The d11.p2 shape: 'Account Number' in a left column, its value in a
        # right one. Banded into one line, the account recognizer's context
        # promotion fires; split across lines it does not, and the number
        # leaks. This is the behaviour the layout segmenter used to lose.
        assert _text(_result(
            texts=["Account Number", ": 162-097111-4"],
            boxes=[[20, 10, 260, 34], [300, 10, 520, 34]],
            scores=[0.9, 0.9],
        )) == "Account Number : 162-097111-4"

    def test_stacked_regions_stay_separate_lines(self):
        assert _text(_result(
            texts=["second", "first"],
            boxes=[[20, 60, 120, 80], [20, 10, 120, 30]],
            scores=[0.9, 0.9],
        )) == "first\nsecond"

    def test_tall_region_does_not_bridge_stacked_lines(self):
        # The BPAY block (issue #6): a tall logo sits between two stacked
        # label/value lines. y-center banding with max-height would let the
        # logo bridge them into one interleaved row; the x-overlap guard keeps
        # them separate — two regions sharing an x-column are stacked lines,
        # not one row — so the card line survives intact and reads as a card.
        text = _text(_result(
            texts=["Biller Code 22863", "BPAY", "Ref: 4564 9427 0001 0443"],
            boxes=[
                [358, 2715, 601, 2745],   # Biller Code, h30, y-center 2730
                [117, 2722, 251, 2780],   # BPAY logo, h58, y-center 2751
                [359, 2752, 703, 2779],   # Ref: card, h27, y-center 2766
            ],
            scores=[0.9, 0.9, 0.9],
        ))
        assert "Ref: 4564 9427 0001 0443" in text.split("\n")

    def test_close_columns_same_row_still_merge(self):
        # Regression guard for the x-overlap guard: side-by-side columns at the
        # same y (non-overlapping x) must still assemble as one row.
        assert _text(_result(
            texts=["01 APR PAYMENT", "2,148.74", "377,970.04"],
            boxes=[
                [20, 10, 300, 40],     # description
                [360, 10, 520, 40],    # debit
                [560, 10, 720, 40],    # balance
            ],
            scores=[0.9, 0.9, 0.9],
        )) == "01 APR PAYMENT 2,148.74 377,970.04"


class TestWordGeometry:
    def test_merged_fragments_map_word_boxes(self):
        # the verified quirk: fragments "TFN123" / " " / "456" against
        # line text "TFN 123 456" — boxes come from char-stream overlap,
        # tokens always from the line text
        (line,) = _page(_result(
            texts=["TFN 123 456"],
            boxes=[[20, 10, 320, 30]],
            scores=[0.9],
            words=[["TFN123", " ", "456"]],
            word_boxes=[[[20, 10, 200, 30], [200, 10, 210, 30],
                         [210, 10, 320, 30]]],
        )).lines
        assert [w.text for w in line.words] == ["TFN", "123", "456"]
        tfn, one23, four56 = line.words
        assert (tfn.box.left, tfn.box.right) == (20, 200)
        assert (one23.box.left, one23.box.right) == (20, 200)
        assert (four56.box.left, four56.box.right) == (210, 320)

    def test_fragment_mismatch_falls_back_to_interpolation(self):
        # fragment chars disagree with the line text -> whole line
        # interpolates over the line box
        (line,) = _page(_result(
            texts=["AB CD"],
            boxes=[[0, 10, 100, 30]],
            scores=[0.9],
            words=[["ABX"]],
            word_boxes=[[[0, 10, 50, 30]]],
        )).lines
        ab, cd = line.words
        assert ab.box.left < cd.box.left
        assert ab.box.right <= cd.box.left + 1
        assert cd.box.right <= 100

    def test_no_word_data_interpolates(self):
        (line,) = _page(_result(
            texts=["one two"],
            boxes=[[0, 10, 140, 30]],
            scores=[0.9],
        )).lines
        one, two = line.words
        assert [w.text for w in line.words] == ["one", "two"]
        assert one.box.left == 0
        assert two.box.left > one.box.right - 2
        assert two.box.right <= 140

    def test_polys_when_rec_boxes_missing(self):
        (line,) = _page({
            "rec_texts": ["hi"],
            "rec_polys": [[(20, 10), (120, 10), (120, 30), (20, 30)]],
            "rec_scores": [1.0],
        }).lines
        (word,) = line.words
        assert word.box.left == 20
        assert word.box.right == 120

    def test_line_box_is_the_region_box_with_or_without_fragments(self):
        # A paddle result may or may not carry usable word fragments (a
        # mismatch falls back to interpolation across the region box). The
        # LINE box must not move between the two — it spans the region, which
        # is what contains the ink and what painting grows a run out to.
        region = [20, 10, 320, 30]
        interpolated = _page(_result(
            texts=["TFN 123 456"], boxes=[region], scores=[0.9],
        )).lines[0]
        fragmented = _page(_result(
            texts=["TFN 123 456"],
            boxes=[region],
            scores=[0.9],
            # inset from the region by 6px left / 4px right, as paddle emits
            words=[["TFN", "123", "456"]],
            word_boxes=[[[26, 10, 120, 30], [130, 10, 220, 30],
                         [230, 10, 316, 30]]],
        )).lines[0]
        assert fragmented.box == interpolated.box
        assert (fragmented.box.left, fragmented.box.right) == (20, 320)
        # the inset fragments still survive as the WORD geometry
        assert fragmented.words[0].box.left == 26
        assert fragmented.words[-1].box.right == 316

    def test_boxes_for_span_works_through_adapter(self):
        ocr = linearize(_page(_result(
            texts=["TFN 123 456"],
            boxes=[[20, 10, 320, 30]],
            scores=[0.9],
        )))
        start = ocr.text.index("123")
        boxes = ocr.boxes_for_span(start, start + len("123 456"))
        assert len(boxes) == 1  # same line unions into one box
        assert boxes[0].right <= 320


def test_frame_carried():
    page = _page(_result(texts=["hi"], boxes=[[0, 0, 10, 10]], scores=[1.0]))
    assert page.frame.width == 1000
    assert page.frame.backend == "paddle" and page.frame.tier == "v6_medium"


def test_empty_result():
    page = _page({"rec_texts": [], "rec_scores": []})
    assert page.lines == ()
    assert linearize(page).text == ""

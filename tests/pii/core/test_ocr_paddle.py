"""PaddleOCR adapter conversion (pii/core/ocr_paddle.py) — model-free:
`result_to_page` is pure, so fake PaddleOCR result dicts exercise the
line->word normalization without paddle installed or imported.

Assertions about assembled text go through `linearize`, because that string
is what the locator searches and what the recognizers see."""

import pytest

from pii.core.linearization import linearize
from pii.core.ocr import get_ocr_page
from pii.core.ocr_page import OcrFrame
from pii.core.ocr_paddle import _reread_rotated, result_to_page

_FRAME = OcrFrame(width=1000, height=1000, page=1,
                  backend="paddle", tier="v6_medium")


def _result(texts, boxes, scores, words=None, word_boxes=None, rotations=None):
    d = {"rec_texts": texts, "rec_boxes": boxes, "rec_scores": scores}
    if words is not None:
        d["text_word"] = words
        d["text_word_boxes"] = word_boxes
    if rotations is not None:
        d["rec_rotations"] = rotations
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


class TestRotatedLines:
    """A page-edge stripe: never banded with the horizontal text it crosses,
    and measured along its own axis.

    The geometry here is the reference statement's, scaled: a stripe is ~30 px
    wide and hundreds tall, so its y-centre lands in the middle of the page and
    a y-centre band around it reaches a third of it."""

    def test_a_stripe_is_never_banded_with_the_lines_it_crosses(self):
        # 1.pdf p1: the enquiries phone and the stripe assembled as ONE line,
        # `13 13 14 XPRCAP0022-2309300323`, inside one 1488x275 box — a paint
        # box, a label neighbourhood and a `contiguous` answer all at once.
        assert _text(_result(
            texts=["Account number 12345678", "XPRCAP0022", "Phone 13 13 14"],
            boxes=[
                [200, 300, 900, 340],   # body line, centre 320
                [40, 100, 70, 900],     # the stripe, centre 500, h800
                [200, 500, 900, 540],   # body line, centre 520
            ],
            scores=[0.9, 0.9, 0.9],
            rotations=[0, 90, 0],
        )).split("\n") == [
            "Account number 12345678", "XPRCAP0022", "Phone 13 13 14",
        ]

    def test_a_stripe_does_not_split_the_row_it_crosses(self):
        # Banded in one pass, a stripe whose centre sorts BETWEEN the two
        # regions of a row would stop the second from joining the first and cut
        # the row in half. Rotated regions are banded apart and merged back.
        assert _text(_result(
            texts=["Interest charged", "STRIPE", "$12.34"],
            boxes=[
                [200, 498, 600, 538],   # left column, centre 518
                [40, 119, 70, 919],     # the stripe, centre 519
                [700, 500, 900, 540],   # right column, centre 520
            ],
            scores=[0.9, 0.9, 0.9],
            rotations=[0, 90, 0],
        )).split("\n") == ["Interest charged $12.34", "STRIPE"]

    def test_rotation_reaches_the_line_and_every_word(self):
        (line,) = _page(_result(
            texts=["AAA BBB"], boxes=[[40, 100, 70, 900]], scores=[0.9],
            rotations=[270],
        )).lines
        assert line.rotation == 270
        assert [w.rotation for w in line.words] == [270, 270]

    def test_a_bottom_to_top_line_runs_up_the_page(self):
        # `Statement20220630` p1: the left-margin stripe reads upward, so the
        # FIRST word is the LOWEST on the page. Sorting a rotated row by `left`
        # — or interpolating it along x — would reverse it or collapse it.
        (line,) = _page(_result(
            texts=["AAA BBB"], boxes=[[40, 100, 70, 900]], scores=[0.9],
            rotations=[90],
        )).lines
        first, second = line.words
        assert (first.text, second.text) == ("AAA", "BBB")
        assert first.box.top > second.box.top
        assert first.box.left == second.box.left == 40
        assert first.box.right == second.box.right == 70

    def test_a_top_to_bottom_line_runs_down_the_page(self):
        (line,) = _page(_result(
            texts=["AAA BBB"], boxes=[[40, 100, 70, 900]], scores=[0.9],
            rotations=[270],
        )).lines
        first, second = line.words
        assert first.box.top < second.box.top
        assert first.box.left == second.box.left == 40

    def test_a_tall_region_is_unbanded_even_with_no_rotations_given(self):
        # A result dict from anywhere but `_reread_rotated` — the direction is
        # unknown, but the banding damage a stripe does is a fact about its
        # SHAPE and must not wait on a recognizer.
        page = _page(_result(
            texts=["Phone 13 13 14", "XPRCAP0022"],
            boxes=[[200, 500, 900, 540], [40, 100, 70, 900]],
            scores=[0.9, 0.9],
        ))
        assert [line.text for line in page.lines] == [
            "XPRCAP0022", "Phone 13 13 14",
        ]
        assert [line.rotation for line in page.lines] == [270, 0]

    def test_a_lone_glyph_is_not_a_rotated_line(self):
        # The gate is 2:1 and the tallest upright region measured over the
        # reference corpus is 1.5:1 — a single glyph, which must stay in its
        # row.
        assert _text(_result(
            texts=["$", "1,234.00"],
            boxes=[[200, 500, 230, 540], [260, 500, 460, 540]],
            scores=[0.9, 0.9],
        )) == "$ 1,234.00"


class _Reader:
    """A stand-in for the pipeline's recognition model: scripted answers, in
    call order (`_read_both_ways` reads 90 first, then 270)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def predict(self, array):
        self.calls += 1
        text, score = self.answers.pop(0)
        return [{"rec_text": text, "rec_score": score}]


class TestRereadRotated:
    """`_reread_rotated`: which direction a rotated region is read in, decided
    on the pixels. Model-free — the recognizer is scripted."""

    def _image(self):
        from PIL import Image

        return Image.new("RGB", (200, 1000), "white")

    def test_the_better_reading_wins_and_names_the_direction(self):
        # The measured case: paddle turns every tall crop one way and read
        # `1584.3694.1.2 ZZ258R3 ...` as `235*` at 0.555.
        result = _result(
            texts=["235*"], boxes=[[40, 100, 70, 900]], scores=[0.555],
            words=[["235", "*"]],
            word_boxes=[[[40, 700, 70, 890], [40, 100, 70, 690]]],
        )
        reader = _Reader([("1584.3694.1.2 ZZ258R3", 0.99), ("1331223.35", 0.71)])
        out = _reread_rotated(result, self._image(), recognizer=reader)
        assert out["rec_rotations"] == [90]
        assert out["rec_texts"] == ["1584.3694.1.2 ZZ258R3"]
        assert out["rec_scores"] == [0.99]
        # The fragments describe characters that are no longer there.
        assert out["text_word"] == [[]] and out["text_word_boxes"] == [[]]

    def test_an_upright_region_is_never_re_read(self):
        reader = _Reader([])  # popping from it would raise
        out = _reread_rotated(
            _result(["Total 12.00"], [[40, 100, 400, 140]], [0.9]),
            self._image(), recognizer=reader,
        )
        assert out["rec_rotations"] == [0] and reader.calls == 0
        assert out["rec_texts"] == ["Total 12.00"]

    def test_an_unchanged_reading_keeps_its_word_boxes(self):
        # 1.pdf: paddle's own turn was the right one. Its fragments describe
        # the reading that stands, so they are better geometry than
        # interpolation and must survive.
        result = _result(
            texts=["XPRCAP0022"], boxes=[[40, 100, 70, 900]], scores=[1.0],
            words=[["XPRCAP0022"]], word_boxes=[[[40, 110, 70, 890]]],
        )
        reader = _Reader([("3RC2932", 0.70), ("XPRCAP0022", 1.0)])
        out = _reread_rotated(result, self._image(), recognizer=reader)
        assert out["rec_rotations"] == [270]
        assert out["text_word"] == [["XPRCAP0022"]]

    def test_nothing_legible_keeps_what_paddle_read(self):
        # A dropped line is unredacted PII, so an unreadable stripe keeps its
        # reading — and paddle's own crop direction with it.
        reader = _Reader([("", 0.9), ("   ", 0.8)])
        out = _reread_rotated(
            _result(["235*"], [[40, 100, 70, 900]], [0.555]),
            self._image(), recognizer=reader,
        )
        assert out["rec_texts"] == ["235*"] and out["rec_rotations"] == [270]


def test_frame_carried():
    page = _page(_result(texts=["hi"], boxes=[[0, 0, 10, 10]], scores=[1.0]))
    assert page.frame.width == 1000
    assert page.frame.backend == "paddle" and page.frame.tier == "v6_medium"


def test_empty_result():
    page = _page({"rec_texts": [], "rec_scores": []})
    assert page.lines == ()
    assert linearize(page).text == ""

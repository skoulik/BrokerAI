"""OCR-fidelity sweep internals (pii_eval/ocr_report.py) — model-free:
alignment, error taxonomy, and geometric re-lining are exercised on
hand-built OcrResults; no OCR engine run."""

from pii.core.ocr import Box, assemble
from pii_eval.ocr_report import (
    _bucket,
    _edit_ops,
    align_lines,
    score_page,
    visual_lines,
)


def _word(text, left, top, conf=90.0, width=40, height=20):
    return (text, Box(left=left, top=top, width=width, height=height), conf)


class TestBucket:
    def test_letter_flip(self):
        assert _bucket("sub", "F", "E") == "sub_alpha"

    def test_digit_letter_confusion(self):
        assert _bucket("sub", "0", "O") == "sub_digit_alpha"
        assert _bucket("sub", "l", "1") == "sub_digit_alpha"

    def test_digit_digit(self):
        assert _bucket("sub", "1", "7") == "sub_digit_digit"

    def test_case_only(self):
        assert _bucket("sub", "a", "A") == "sub_case"

    def test_space_loss_is_merge_any_kind(self):
        assert _bucket("del", " ", "") == "merge"
        assert _bucket("sub", " ", ".") == "merge"

    def test_space_gain_is_split_any_kind(self):
        assert _bucket("ins", "", " ") == "split"
        assert _bucket("sub", ".", " ") == "split"

    def test_digit_dropout(self):
        assert _bucket("del", "5", "") == "del_digit"
        assert _bucket("ins", "", "8") == "ins_digit"


class TestEditOps:
    def test_single_substitution(self):
        assert _edit_ops("TFN 123", "TEN 123") == [("sub", 1, 1)]

    def test_merge_is_one_space_deletion(self):
        ops = _edit_ops("12 34", "1234")
        assert ops == [("del", 2, 2)]

    def test_equal_sequences_no_ops(self):
        assert _edit_ops("abc", "abc") == []

    def test_word_sequences(self):
        ops = _edit_ops(["BSB", "013", "999"], ["BSB", "013999"])
        kinds = sorted(kind for kind, _, _ in ops)
        assert kinds == ["del", "sub"]


class TestAlignLines:
    def test_dropped_middle_line(self):
        truth = ["alpha beta gamma", "delta epsilon", "zeta eta theta"]
        pairs, lost, spurious = align_lines(
            truth, ["alpha beta gamma", "zeta eta theta"]
        )
        assert pairs == [(0, 0), (2, 1)]
        assert lost == [1]
        assert spurious == []

    def test_spurious_ocr_line(self):
        pairs, lost, spurious = align_lines(
            ["alpha beta"], ["alpha beta", "%$#@!"]
        )
        assert pairs == [(0, 0)]
        assert lost == []
        assert spurious == [1]

    def test_noisy_line_still_pairs(self):
        pairs, lost, spurious = align_lines(
            ["TFN 123 456 782"], ["TEN 123 456 7B2"]
        )
        assert pairs == [(0, 0)]
        assert not lost and not spurious


class TestVisualLines:
    def test_block_split_columns_rejoin_one_visual_line(self):
        # an OCR engine fragmenting a wide-gutter row into two blocks =
        # two assembled lines at the same y; geometry re-joins them.
        result = assemble([
            [_word("DATE", 0, 0), _word("PARTICULARS", 50, 0)],
            [_word("AMOUNT", 400, 0)],
        ])
        lines = visual_lines(result)
        assert len(lines) == 1
        assert [w.text for w in lines[0]] == ["DATE", "PARTICULARS", "AMOUNT"]
        # words came from >1 assembled line — the resegmentation signal
        assert len({w.line for w in lines[0]}) == 2

    def test_separate_rows_stay_separate(self):
        result = assemble([
            [_word("first", 0, 0), _word("row", 50, 0)],
            [_word("second", 0, 60), _word("row", 50, 60)],
        ])
        lines = visual_lines(result)
        assert [[w.text for w in line] for line in lines] == [
            ["first", "row"], ["second", "row"],
        ]

    def test_tiny_punctuation_box_joins_its_row(self):
        result = assemble([
            [_word("total", 0, 0, height=20),
             _word("...", 50, 14, height=3),
             _word("42", 90, 0, height=20)],
        ])
        assert len(visual_lines(assemble([
            [_word("total", 0, 0, height=20)],
            [_word("...", 50, 14, height=3)],
            [_word("42", 90, 0, height=20)],
        ]))) == 1
        assert len(visual_lines(result)) == 1


class TestScorePage:
    def test_flip_and_merge_counted(self):
        truth = "TFN 123 456\nBSB 013 999\n"
        result = assemble([
            [_word("TEN", 0, 0, conf=61.0), _word("123", 50, 0),
             _word("456", 100, 0)],
            [_word("BSB", 0, 60), _word("013999", 50, 60, conf=55.0)],
        ])
        stats = score_page(truth, result)
        assert stats["buckets"] == {"sub_alpha": 1, "merge": 1}
        assert stats["confusion"] == {"F->E": 1}
        assert stats["char_errors"] == 2
        assert stats["truth_chars"] == 22
        assert stats["cer"] == 2 / 22
        # words: TFN→TEN sub; (013, 999) → 013999 = sub + del
        assert stats["word_errors"] == 3
        assert stats["conf_correct_n"] == 3
        assert stats["conf_error_n"] == 2
        assert stats["lines_lost"] == 0
        assert stats["lines_spurious"] == 0

    def test_lost_line_chars_count_as_errors(self):
        truth = "alpha beta gamma\ndelta epsilon zeta\n"
        result = assemble([
            [_word("alpha", 0, 0), _word("beta", 60, 0),
             _word("gamma", 120, 0)],
        ])
        stats = score_page(truth, result)
        assert stats["lines_lost"] == 1
        assert stats["lines_merged_elsewhere"] == 0
        assert stats["char_errors"] == len("delta epsilon zeta")

    def test_lost_line_found_elsewhere_flagged_merged(self):
        truth = "alpha beta\ndelta epsilon\n"
        result = assemble([
            [_word("alpha", 0, 0), _word("beta", 60, 0),
             _word("delta", 120, 0), _word("epsilon", 180, 0)],
        ])
        stats = score_page(truth, result)
        assert stats["lines_lost"] == 1
        assert stats["lines_merged_elsewhere"] == 1

    def test_perfect_page_zero_errors(self):
        truth = "clean page text\nsecond line here\n"
        result = assemble([
            [_word("clean", 0, 0), _word("page", 60, 0),
             _word("text", 120, 0)],
            [_word("second", 0, 60), _word("line", 60, 60),
             _word("here", 120, 60)],
        ])
        stats = score_page(truth, result)
        assert stats["char_errors"] == 0
        assert stats["word_errors"] == 0
        assert stats["cer"] == 0.0
        assert stats["conf_error_n"] == 0

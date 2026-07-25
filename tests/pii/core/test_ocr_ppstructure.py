"""PP-StructureV3 adapter (pii/core/ocr_ppstructure.py) — model-free: the
conversion is pure, so a captured real result dict exercises block building,
line->block containment, reading order and orphan handling without paddle.

_ANZ is a faithful capture of PP-StructureV3 (lean config) run on page 1 of
the ANZ policy PDF (2026-07-24) — 9 OCR lines, 4 layout blocks, no word
fragments (has_text_word was False)."""

from collections import Counter

from pii.core import ocr_ppstructure
from pii.core.linearization import linearize
from pii.core.ocr_page import OcrFrame
from pii.core.ocr_ppstructure import ppstructure_result_to_page

_FRAME = OcrFrame(width=1241, height=1754, page=1, backend="paddle:structure")

_ANZ = {
    "overall_ocr_res": {
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
        "rec_scores": [0.97, 0.99, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0],
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
    },
    "parsing_res_list": [
        {"label": "text", "bbox": [848, 585, 1186, 653],
         "num_of_lines": 2, "order_index": 1, "index": 0},
        {"label": "text", "bbox": [831, 783, 1184, 854],
         "num_of_lines": 2, "order_index": 2, "index": 1},
        {"label": "text", "bbox": [142, 1225, 1146, 1330],
         "num_of_lines": 4, "order_index": 3, "index": 2},
        {"label": "footer", "bbox": [942, 1616, 1191, 1700],
         "num_of_lines": 1, "order_index": None, "index": 3},
    ],
}


def _anz():
    return ppstructure_result_to_page(_ANZ, _FRAME)


def test_detected_blocks_typed_and_reading_ordered():
    page = _anz()
    det = [b for b in page.blocks if b.origin == "detected"]
    assert len(det) == 4
    assert [b.kind for b in det] == ["text", "text", "text", "footer"]
    assert [b.reading_order for b in det] == [0, 1, 2, 3]  # footer (None) last


def test_no_orphans_on_this_page():
    page = _anz()
    assert all(b.origin == "detected" for b in page.blocks)


def test_containment_reproduces_num_of_lines():
    # The independent cross-check: geometric containment must land the same
    # per-block line counts PP-Structure reported (2, 2, 4, 1).
    page = _anz()
    counts = Counter(ln.block_id for ln in page.lines)
    assert [counts[b.id] for b in page.blocks] == [2, 2, 4, 1]
    assert [b["num_of_lines"] for b in _ANZ["parsing_res_list"]] == [2, 2, 4, 1]


def test_line_order_and_transitive_page():
    page = _anz()
    assert [ln.text for ln in page.lines] == _ANZ["overall_ocr_res"]["rec_texts"]
    for ln in page.lines:  # block_id total; page reachable through the block
        assert page.block_of(ln).page_id == page.frame.page


def test_footer_line_lands_in_footer_block():
    page = _anz()
    anz = next(ln for ln in page.lines if ln.text == "ANZ")
    assert page.block_of(anz).kind == "footer"


def test_feeds_linearize():
    ri = linearize(_anz())
    assert ri.text.startswith("MORTGAGE CREDIT")
    for w in ri.words:
        assert ri.text[w.char_start : w.char_end] == w.text


def test_orphan_line_gets_own_synthetic_block():
    result = {
        "overall_ocr_res": {
            "rec_texts": ["INSIDE", "STRAY"],
            "rec_scores": [0.9, 0.9],
            "rec_boxes": [[100, 100, 200, 130], [900, 20, 1000, 50]],
        },
        "parsing_res_list": [
            {"label": "text", "bbox": [90, 90, 210, 140],
             "num_of_lines": 1, "order_index": 1, "index": 0},
        ],
    }
    page = ppstructure_result_to_page(result, _FRAME)
    stray = next(ln for ln in page.lines if ln.text == "STRAY")
    stray_block = page.block_of(stray)
    assert stray_block.origin == "synthetic" and stray_block.kind == "unassigned"
    inside = next(ln for ln in page.lines if ln.text == "INSIDE")
    assert page.block_of(inside).origin == "detected"


def test_empty_result():
    page = ppstructure_result_to_page({}, _FRAME)
    assert page.lines == () and page.blocks == ()


# --- layout threshold resolution (model-free; paddle never constructed) ------

_SHIPPED = {0: 0.3, 1: 0.5, 2: 0.4, 7: 0.3, 8: 0.5}
_LABELS = ["paragraph_title", "image", "text", "number", "abstract",
           "content", "figure_title", "formula", "table"]


def _patch(monkeypatch, config, labels):
    monkeypatch.setattr(ocr_ppstructure, "_paddlex_pipeline_config",
                        lambda: config)
    monkeypatch.setattr(ocr_ppstructure, "_layout_labels", lambda name: labels)


def _config(threshold=_SHIPPED, model_name="PP-DocLayout_plus-L"):
    return {"SubModules": {"LayoutDetection": {
        "threshold": threshold, "model_name": model_name}}}


def test_no_override_by_default(monkeypatch):
    # We ship PaddleX's own thresholds. None is what paddleocr's config merge
    # skips, so it genuinely means "leave the shipped config alone".
    _patch(monkeypatch, _config(), _LABELS)
    assert ocr_ppstructure._layout_thresholds() is None
    assert ocr_ppstructure._layout_thresholds({}) is None


def test_override_moves_only_the_named_class(monkeypatch):
    _patch(monkeypatch, _config(), _LABELS)
    thresholds = ocr_ppstructure._layout_thresholds({"text": 0.33})
    assert thresholds[2] == 0.33  # `text` is class 2 in this label list
    assert {k: v for k, v in thresholds.items() if k != 2} == {
        k: v for k, v in _SHIPPED.items() if k != 2
    }


def test_override_keeps_every_shipped_class(monkeypatch):
    # The dict must be COMPLETE: paddlex does threshold.get(cat_id, 0.5), so a
    # dropped key silently re-raises that class to 0.5 (paragraph_title's 0.3
    # is the one that would regress).
    _patch(monkeypatch, _config(), _LABELS)
    thresholds = ocr_ppstructure._layout_thresholds({"text": 0.33})
    assert set(thresholds) == set(_SHIPPED)
    assert thresholds[0] == 0.3


def test_override_follows_relabelled_model(monkeypatch):
    # A paddlex upgrade that reorders label_list must not retarget an override
    # at another class.
    _patch(monkeypatch, _config(), ["text", "image", "paragraph_title"])
    assert ocr_ppstructure._layout_thresholds({"text": 0.33})[0] == 0.33


def test_override_multiple_classes(monkeypatch):
    _patch(monkeypatch, _config(), _LABELS)
    thresholds = ocr_ppstructure._layout_thresholds({"text": 0.33,
                                                     "table": 0.4})
    assert (thresholds[2], thresholds[8]) == (0.33, 0.4)


def test_override_none_when_unresolvable(monkeypatch):
    # Never guess a class index — fall back to shipped behaviour instead.
    _patch(monkeypatch, _config(), ["image", "table"])  # no `text` label
    assert ocr_ppstructure._layout_thresholds({"text": 0.33}) is None
    _patch(monkeypatch, _config(), None)  # model dir missing
    assert ocr_ppstructure._layout_thresholds({"text": 0.33}) is None
    _patch(monkeypatch, _config(threshold=None), _LABELS)  # flat/absent config
    assert ocr_ppstructure._layout_thresholds({"text": 0.33}) is None
    _patch(monkeypatch, {}, _LABELS)  # config not found at all
    assert ocr_ppstructure._layout_thresholds({"text": 0.33}) is None

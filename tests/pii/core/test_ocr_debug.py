"""OCR-page diagnostics (pii/core/ocr_debug.py) — model-free: renderers run
on an OcrPage built by the pure PP-Structure adapter, no engine."""

import json

from PIL import Image

from pii.core.ocr_debug import (
    draw_overlay,
    page_from_dict,
    page_to_dict,
    page_to_json,
    page_to_text,
)
from pii.core.ocr_page import OcrFrame
from pii.core.ocr_ppstructure import ppstructure_result_to_page

_FRAME = OcrFrame(width=600, height=400, page=2, dpi=200, source="doc.pdf",
                  backend="paddle:structure")
_FIX = {
    "overall_ocr_res": {
        "rec_texts": ["HELLO WORLD", "STRAY LINE"],
        "rec_scores": [0.9, 0.8],
        "rec_boxes": [[10, 10, 110, 30], [400, 300, 520, 320]],
    },
    "parsing_res_list": [
        {"label": "title", "bbox": [5, 5, 120, 35],
         "num_of_lines": 1, "order_index": 1, "index": 0},
    ],
}


def _page():
    # "HELLO WORLD" sits in the title block; "STRAY LINE" lands in no block
    # -> a synthetic block. Exercises both detected and synthetic rendering.
    return ppstructure_result_to_page(_FIX, _FRAME)


def test_json_round_trips():
    page = _page()
    assert page_from_dict(page_to_dict(page)) == page


def test_json_is_serializable_and_carries_frame():
    data = json.loads(page_to_json(_page()))
    assert data["frame"]["source"] == "doc.pdf"
    assert data["frame"]["page"] == 2
    assert {b["origin"] for b in data["blocks"]} == {"detected", "synthetic"}


def test_text_summary_mentions_kinds_and_lines():
    txt = page_to_text(_page())
    assert "HELLO WORLD" in txt and "STRAY LINE" in txt
    assert "title" in txt  # the detected block's kind
    assert "synthetic" in txt  # the orphan's origin


def test_overlay_annotates_a_copy_without_touching_input():
    page = _page()
    base = Image.new("RGB", (600, 400), "white")
    out = draw_overlay(base, page)
    assert out.size == (600, 400)
    assert out.tobytes() != base.tobytes()  # something was drawn
    # input image is not mutated
    assert base.tobytes() == Image.new("RGB", (600, 400), "white").tobytes()

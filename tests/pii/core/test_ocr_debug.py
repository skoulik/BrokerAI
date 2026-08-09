"""OCR-page diagnostics (pii/core/ocr_debug.py) — model-free: the renderers
run on a hand-built OcrPage, no engine.

What they render is the geometry the strip path paints with, so the dump has
to be faithful enough to reason about a leak from: every line present, every
word box carried, the frame's provenance intact."""

import json

from PIL import Image

from pii.core.ocr import Box
from pii.core.ocr_debug import (
    draw_overlay,
    page_from_dict,
    page_to_dict,
    page_to_json,
    page_to_text,
)
from pii.core.ocr_page import OcrFrame, build_page

_FRAME = OcrFrame(width=600, height=400, page=2, dpi=200, source="doc.pdf",
                  backend="paddle", tier="v6_medium")


def _page():
    # Two rows, each aggregating two words from one detection region — the
    # shape `_result_to_rows` produces, including the inset word boxes.
    top, bottom = Box(10, 10, 100, 20), Box(400, 300, 120, 20)
    return build_page(
        [
            [("HELLO", Box(12, 10, 44, 20), 90.0, top),
             ("WORLD", Box(62, 10, 46, 20), 90.0, top)],
            [("STRAY", Box(402, 300, 52, 20), 80.0, bottom),
             ("LINE", Box(460, 300, 58, 20), 80.0, bottom)],
        ],
        _FRAME,
    )


def test_json_round_trips():
    page = _page()
    assert page_from_dict(page_to_dict(page)) == page


def test_json_is_serializable_and_carries_frame():
    data = json.loads(page_to_json(_page()))
    assert data["frame"]["source"] == "doc.pdf"
    assert data["frame"]["page"] == 2
    assert data["frame"]["dpi"] == 200
    assert [ln["text"] for ln in data["lines"]] == ["HELLO WORLD", "STRAY LINE"]


def test_json_carries_word_geometry():
    # The word boxes ARE the paint geometry; a dump that loses them cannot be
    # used to explain what survived.
    (first, _second) = json.loads(page_to_json(_page()))["lines"]
    assert [w["text"] for w in first["words"]] == ["HELLO", "WORLD"]
    assert first["words"][0]["box"] == [12, 10, 44, 20]
    assert first["words"][0]["region_box"] == [10, 10, 100, 20]


def test_text_summary_lists_every_line_in_assembly_order():
    txt = page_to_text(_page())
    assert "2 lines" in txt
    assert txt.index("HELLO WORLD") < txt.index("STRAY LINE")


def test_overlay_annotates_a_copy_without_touching_input():
    page = _page()
    base = Image.new("RGB", (600, 400), "white")
    out = draw_overlay(base, page)
    assert out.size == (600, 400)
    assert out.tobytes() != base.tobytes()  # something was drawn
    # input image is not mutated
    assert base.tobytes() == Image.new("RGB", (600, 400), "white").tobytes()

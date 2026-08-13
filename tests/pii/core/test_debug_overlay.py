"""The e2e debug overlay (pii/core/debug_overlay.py) — model-free: the
renderers run on a hand-built page record, no OCR engine and no detector.

What the overlay shows is how a strip run reached its decision, so the tests
pin the properties that make it readable as evidence: a layer the operator did
NOT ask for is not drawn, a layer they did ask for is, and the provenance tag
tells a value the model found here from one the document lent and from one only
a pattern caught."""

from pathlib import Path

import pytest
from PIL import Image

from pii.core.debug_overlay import (
    DEBUG_LAYERS,
    DebugSpec,
    PageDebug,
    draw_layers,
    page_debug,
    parse_layers,
    span_provenance,
)
from pii.core.detection import Detection
from pii.core.linearization import linearize
from pii.core.locator import Placement
from pii.core.ocr import Box
from pii.core.ocr_page import OcrFrame, build_page
from pii.core.vlm import VlmFinding

_WORD_COLOR = (90, 90, 90)
_LINE_COLOR = (30, 120, 220)
_LAYER0_COLOR = (190, 40, 190)
_LOCATE_COLOR = (235, 140, 0)
_LAYER1_COLOR = (220, 30, 30)
_SKIPPED_COLOR = (120, 130, 140)


def _ocr():
    """One row: 'Client SERGEI KULIK', with the inset word boxes a real engine
    produces (word box inside its detection region box)."""
    region = Box(10, 100, 220, 24)
    return linearize(
        build_page(
            [
                [
                    ("Client", Box(12, 102, 58, 20), 90.0, region),
                    ("SERGEI", Box(80, 102, 62, 20), 90.0, region),
                    ("KULIK", Box(150, 102, 58, 20), 90.0, region),
                ]
            ],
            OcrFrame(width=400, height=300, page=1),
        )
    )


def _debug():
    ocr = _ocr()
    start = ocr.text.index("SERGEI")
    end = start + len("SERGEI KULIK")
    # The model's box is deliberately displaced from the words it names — a
    # rough box is the normal case, and keeping the two rectangles apart is
    # what lets these tests tell the layer-0 stage from the locate stage.
    # Normalized to 1000 on a 400x300 page: (200,180)-(300,210) px.
    finding = VlmFinding(
        text="SERGEI KULIK", entity_type="PERSON", box=(500, 600, 750, 700)
    )
    return PageDebug(
        ocr=ocr,
        placements=(
            Placement(finding=finding, kind="exact", spans=((start, end),)),
        ),
        spans=(
            Detection(entity_type="PERSON", start=start, end=end, score=1.0),
        ),
    )


def _colors(image):
    return {color for _, color in image.getcolors(image.width * image.height)}


def _blank():
    return Image.new("RGB", (400, 300), "white")


# --- layer selection ------------------------------------------------------


def test_parse_layers_accepts_a_list_and_all():
    assert parse_layers("ocr,layer-1") == ("ocr", "layer-1")
    assert parse_layers("all") == DEBUG_LAYERS


def test_parse_layers_is_order_independent():
    # Drawing order is pipeline order (perception under, painted spans on top)
    # whatever order the operator typed, so an overlay always stacks the same
    # way.
    assert parse_layers("layer-1, locate, ocr") == ("ocr", "locate", "layer-1")


def test_parse_layers_rejects_an_unknown_name():
    # 'level-0' is the plausible typo — the flag names must not silently drop
    # the layer the operator asked for.
    with pytest.raises(ValueError, match="level-0"):
        parse_layers("ocr,level-0")
    with pytest.raises(ValueError, match="no debug layers"):
        parse_layers("  ")


def test_draw_layers_rejects_an_unknown_layer():
    with pytest.raises(ValueError, match="nope"):
        draw_layers(_blank(), _debug(), ["nope"])


def test_spec_paths_name_one_file_per_layer():
    # One artifact per layer, not one page carrying all of them — the layer
    # name goes before the extension so the set sorts together and each file
    # says what is in it.
    spec = DebugSpec(layers=("ocr", "layer-1"), path="out/doc.clean.debug.pdf")
    assert [Path(p).name for _, p in spec.paths()] == [
        "doc.clean.debug.ocr.pdf",
        "doc.clean.debug.layer-1.pdf",
    ]


# --- what each layer draws ------------------------------------------------


def test_a_skipped_detection_is_drawn_on_layer_1():
    """Found, then exempted by the keep list. It appeared on NO layer until
    this was added — and it is exactly the state behind "why is this value
    still readable" (2026-08-11: a truncated trust name sat here three times on
    one page with nothing to show it)."""
    ocr = _ocr()
    start = ocr.text.index("SERGEI")
    skipped = Detection(entity_type="ORGANIZATION", start=start,
                        end=start + len("SERGEI KULIK"), score=1.0)
    page = PageDebug(ocr=ocr, skipped=(skipped,))
    painted = _colors(draw_layers(_blank(), page, ["layer-1"]))
    assert _SKIPPED_COLOR in painted
    # ...and it is not confused with a painted span.
    assert _LAYER1_COLOR not in painted


@pytest.mark.parametrize(
    "layer, drawn",
    [
        ("ocr", {_WORD_COLOR, _LINE_COLOR}),
        ("layer-0", {_LAYER0_COLOR}),
        ("locate", {_LOCATE_COLOR}),
        ("layer-1", {_LAYER1_COLOR}),
    ],
)
def test_each_layer_draws_only_when_asked(layer, drawn):
    every = {_WORD_COLOR, _LINE_COLOR, _LAYER0_COLOR, _LOCATE_COLOR,
             _LAYER1_COLOR}
    painted = _colors(draw_layers(_blank(), _debug(), [layer]))
    assert drawn <= painted
    assert not (every - drawn) & painted


def test_all_layers_together_draw_all_of_them():
    every = _colors(draw_layers(_blank(), _debug(), DEBUG_LAYERS))
    assert {_WORD_COLOR, _LINE_COLOR, _LAYER0_COLOR, _LOCATE_COLOR,
            _LAYER1_COLOR} <= every


def test_draw_layers_does_not_mutate_the_input_image():
    base = _blank()
    before = base.tobytes()
    out = draw_layers(base, _debug(), DEBUG_LAYERS)
    assert out.tobytes() != before  # something was drawn...
    assert base.tobytes() == before  # ...on a copy


def test_layers_with_no_data_draw_nothing_rather_than_failing():
    """The --geometry vlm regime: no OCR ran, so there is no text and no plan.

    An overlay of that run must still render — 'this regime has no OCR text' is
    the fact it is there to show."""
    out = draw_layers(_blank(), PageDebug(), DEBUG_LAYERS)
    assert out.tobytes() == _blank().tobytes()


def test_layer0_draws_the_models_own_box_not_the_located_geometry():
    """Layer 0 is the model alone: its box at (200,180), never the rectangle
    the locator resolved, so the two stages stay distinguishable on the page."""
    debug = _debug()
    out = draw_layers(_blank(), debug, ["layer-0"])
    assert out.getpixel((200, 180)) == _LAYER0_COLOR
    # The located span's own word boxes belong to `locate`, not here.
    ((start, end),) = debug.placements[0].spans
    located = debug.ocr.boxes_for_span(start, end)[0]
    assert out.getpixel((located.left, located.top)) != _LAYER0_COLOR


def test_layer0_draws_nothing_when_the_model_gave_no_boxes():
    """The --geometry ocr regime: no boxes were ever requested, so layer 0
    contributed no geometry and the layer is empty. Substituting the located
    span here would file the LOCATOR's answer under layer 0's name."""
    debug = _debug()
    boxless = Placement(
        finding=VlmFinding(text="SERGEI KULIK", entity_type="PERSON"),
        kind="exact",
        spans=debug.placements[0].spans,
    )
    page = PageDebug(ocr=debug.ocr, placements=(boxless,))
    assert _LAYER0_COLOR not in _colors(draw_layers(_blank(), page, ["layer-0"]))
    # ...while the locator's own answer for it is still drawn.
    assert _LOCATE_COLOR in _colors(draw_layers(_blank(), page, ["locate"]))


def test_locate_draws_the_resolved_span_not_the_model_box():
    debug = _debug()
    ((start, end),) = debug.placements[0].spans
    box = debug.ocr.boxes_for_span(start, end)[0]
    out = draw_layers(_blank(), debug, ["locate"])
    assert out.getpixel((box.left, box.top)) == _LOCATE_COLOR
    assert out.getpixel((200, 180)) != _LOCATE_COLOR  # the model's box


def test_locate_falls_back_to_tier_3_geometry():
    # No text matched: the model's padded box is the only geometry there is.
    fallback = Box(10, 10, 60, 20)
    debug = PageDebug(
        ocr=_ocr(),
        placements=(
            Placement(
                finding=VlmFinding(text="logo", entity_type="ORGANIZATION",
                                   box=(100, 100, 200, 200)),
                kind="box", box=fallback,
            ),
        ),
    )
    out = draw_layers(_blank(), debug, ["locate"])
    assert out.getpixel((fallback.left, fallback.top)) == _LOCATE_COLOR


def test_an_unplaced_finding_draws_nothing_on_the_locate_layer():
    """The signal an operator must not miss: a layer-0 box with no locate box
    over it is an unredacted detection. Drawing one anyway — at the model's
    box, say — would claim a redaction that never happened."""
    debug = PageDebug(
        ocr=_ocr(),
        placements=(
            Placement(
                finding=VlmFinding(text="ghost", entity_type="PERSON",
                                   box=(100, 100, 300, 200)),
                kind=None,
            ),
        ),
    )
    assert _LAYER0_COLOR in _colors(draw_layers(_blank(), debug, ["layer-0"]))
    assert _LOCATE_COLOR not in _colors(draw_layers(_blank(), debug, ["locate"]))


# --- provenance -----------------------------------------------------------


def test_provenance_prefers_the_page_s_own_detection():
    debug = _debug()
    assert span_provenance(debug.spans[0], debug) == "L0"


def test_provenance_marks_a_value_the_document_lent():
    ocr = _ocr()
    start = ocr.text.index("SERGEI")
    span = Detection(entity_type="PERSON", start=start,
                     end=start + len("SERGEI KULIK"), score=1.0)
    debug = PageDebug(ocr=ocr, spans=(span,), borrowed=(span,))
    assert span_provenance(span, debug) == "DOC"


def test_provenance_marks_what_only_a_pattern_caught():
    # No layer-0 placement and nothing borrowed: this is the deterministic
    # recall floor catching what the semantic detector missed on this page.
    ocr = _ocr()
    span = Detection(entity_type="AU_TFN", start=0, end=6, score=0.8)
    assert span_provenance(span, PageDebug(ocr=ocr, spans=(span,))) == "L1"


# --- the strip-result seam ------------------------------------------------


def test_page_debug_gathers_a_strip_result():
    class _Result:
        ocr = "OCR"
        placements = ["p"]
        spans = ["s"]
        borrowed = ["b"]

    gathered = page_debug(_Result())
    assert gathered.ocr == "OCR"
    assert gathered.placements == ("p",)
    assert gathered.spans == ("s",)
    assert gathered.borrowed == ("b",)

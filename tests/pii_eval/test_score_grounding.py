"""The grounding scorer's geometry, which is where it can be quietly wrong.

Model-free by construction: these exercise the arithmetic, not the model. The
numbers this scorer prints are used to compare geometries against each other,
so an error here would not look like a failure — it would look like a verdict.
"""

from pii_eval.score_grounding import (
    _contained,
    _corners,
    _covered_by,
    _iou,
    _rect,
)


def test_truth_box_dict_becomes_corners():
    assert _rect({"left": 10, "top": 20, "width": 30, "height": 40}) == (
        10, 20, 40, 60
    )


def test_ocr_box_becomes_corners():
    from pii.core.ocr import Box

    assert _corners(Box(left=5, top=6, width=7, height=8)) == (5, 6, 12, 14)


def test_containment_is_asymmetric_and_iou_is_not():
    # The distinction the scorer rests on: a model box that SWALLOWS the truth
    # is a perfectly usable search constraint (containment 1.0) while being a
    # poor box by IoU. Reporting only IoU would call it a failure.
    truth = (10, 10, 20, 20)
    loose = (0, 0, 100, 100)
    assert _contained(truth, loose) == 1.0
    assert _iou(truth, loose) < 0.02


def test_a_box_that_misses_entirely_scores_zero_both_ways():
    truth = (10, 10, 20, 20)
    elsewhere = (500, 500, 600, 600)
    assert _contained(truth, elsewhere) == 0.0
    assert _iou(truth, elsewhere) == 0.0


def test_half_covered_truth_is_half_contained():
    truth = (0, 0, 10, 10)
    assert _contained(truth, (0, 0, 5, 10)) == 0.5


def test_union_coverage_does_not_double_count_overlapping_paint():
    # The reason coverage scans rows instead of summing intersections: painted
    # boxes overlap constantly (adjacent words, a value painted by two
    # segments), and summing would report more than 100% coverage.
    truth = (0, 0, 10, 10)
    overlapping = [(0, 0, 8, 10), (4, 0, 10, 10)]
    assert _covered_by(truth, overlapping) == 1.0


def test_partial_paint_leaves_a_measurable_fragment():
    # The direction that matters: an uncovered strip is legible PII, and the
    # scorer must report it as a fraction rather than as a boolean miss.
    truth = (0, 0, 10, 10)
    assert _covered_by(truth, [(0, 0, 7, 10)]) == 0.7


def test_paint_outside_the_truth_box_does_not_inflate_coverage():
    truth = (0, 0, 10, 10)
    assert _covered_by(truth, [(20, 20, 40, 40)]) == 0.0


def test_no_paint_at_all_is_zero_coverage():
    assert _covered_by((0, 0, 10, 10), []) == 0.0


# --- ink coverage: why the rectangle version was not good enough ------------


def _page(dark_pixels, size=(20, 20)):
    """A white page with the given pixels inked black."""
    from PIL import Image

    img = Image.new("L", size, 255)
    for x, y in dark_pixels:
        img.putpixel((x, y), 0)
    return img


def test_ink_finds_only_dark_pixels_and_reports_page_coordinates():
    from pii_eval.score_grounding import _ink

    page = _page([(5, 5), (6, 5)])
    assert sorted(_ink(page, (0, 0, 20, 20))) == [(5, 5), (6, 5)]
    # And it is scoped to the rect, in page coordinates.
    assert sorted(_ink(page, (5, 5, 7, 6))) == [(5, 5), (6, 5)]
    assert _ink(page, (10, 10, 20, 20)) == []


def test_a_truth_box_of_blank_paper_counts_as_covered():
    # A truth box over no ink (a barcode whose payload never rendered) has
    # nothing left to read. Calling that a miss would be noise, and it would
    # drag the mean down on exactly the entities that cannot leak.
    from pii_eval.score_grounding import _ink_covered

    assert _ink_covered([], []) == 1.0


def test_ink_coverage_ignores_the_whitespace_the_rectangle_counted():
    # The bug this metric exists for. A truth rectangle from the PDF text layer
    # is taller than the ink (font ascender/descender), so a paint box tight to
    # the glyphs covered ~80% of the RECTANGLE while covering 100% of the ink.
    # Reporting the former called every value on the page a partial leak.
    from pii_eval.score_grounding import _covered_by, _ink, _ink_covered

    truth = (0, 0, 10, 10)          # rectangle, with slack above and below
    page = _page([(x, 5) for x in range(10)])   # ink on one row only
    painted = [(0, 4, 10, 7)]       # tight to the ink

    assert _covered_by(truth, painted) == 0.3   # rectangle: looks like a leak
    assert _ink_covered(_ink(page, truth), painted) == 1.0   # ink: covered


def test_uncovered_ink_is_reported_as_a_fraction():
    from pii_eval.score_grounding import _ink, _ink_covered

    page = _page([(x, 5) for x in range(10)])
    # Paint covers only the left half of the inked row.
    assert _ink_covered(_ink(page, (0, 0, 10, 10)), [(0, 4, 5, 7)]) == 0.5


# --- the corpus shapes the scorer has to survive ----------------------------


def _corpus(tmp_path, entities):
    """A one-page corpus with the given truth entities."""
    import json

    from PIL import Image

    (tmp_path / "pages").mkdir()
    Image.new("RGB", (60, 40), "white").save(tmp_path / "pages" / "d01.p1.png")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "source": "wherever", "dpi": 300,
        "docs": [{"id": "d01", "source": "d01.pdf", "pages": ["d01.p1.png"]}],
    }), "utf-8")
    (tmp_path / "truth.json").write_text(json.dumps({
        "source": "wherever", "dpi": 300,
        "docs": [{"id": "d01", "source": "d01.pdf", "entities": entities}],
    }), "utf-8")
    return tmp_path


def _stub(monkeypatch):
    """Replace the model and the strip so only the scoring path runs."""
    from pii.core.ocr import Box
    from pii.core.vlm import DetectorResult
    from pii_eval import score_grounding as sg

    class Seg:
        boxes = [Box(left=0, top=0, width=10, height=10)]

    class Result:
        incomplete = sg.Incomplete()
        segments = [Seg()]

    monkeypatch.setattr(sg, "build_detector", lambda *a, **k: object())
    monkeypatch.setattr(sg, "strip_image",
                        lambda *a, **k: Result())
    monkeypatch.setattr(sg._Recorder, "detect",
                        lambda self, image: DetectorResult([]))


def test_a_valueless_truth_entity_does_not_crash_the_scorer(tmp_path,
                                                            monkeypatch):
    # The 2026-08-19 failure, 48 minutes into a corpus run: truth.json carries
    # valueless BARCODE entities (they have boxes but no string), `score_pdf`
    # skips them, and this scorer did not — so `find_value` was handed None.
    # A --limit smoke test could not catch it: the corpus's barcodes live in
    # d03, d05 and d10.
    _stub(monkeypatch)
    corpus = _corpus(tmp_path, [
        {"type": "BARCODE", "value": None, "strip_expected": True,
         "occurrences": 1,
         "boxes": [{"page": "d01.p1.png", "left": 0, "top": 0,
                    "width": 10, "height": 10}]},
        {"type": "PERSON", "value": "SERGEI KULIK", "strip_expected": True,
         "occurrences": 1,
         "boxes": [{"page": "d01.p1.png", "left": 0, "top": 0,
                    "width": 10, "height": 10}]},
    ])
    from pii_eval.score_grounding import score_grounding

    assert score_grounding(str(corpus)) == 0


def test_keep_entities_are_not_scored_as_grounding_targets(tmp_path,
                                                           monkeypatch):
    # strip_expected=False is detect-but-KEEP truth. Painting over it would be
    # the over-strip axis's business, not grounding's, and counting it here
    # would report a deliberate keep as an ungrounded value.
    _stub(monkeypatch)
    corpus = _corpus(tmp_path, [
        {"type": "ORGANIZATION", "value": "ANZ", "strip_expected": False,
         "occurrences": 1,
         "boxes": [{"page": "d01.p1.png", "left": 0, "top": 0,
                    "width": 10, "height": 10}]},
    ])
    from pii_eval.score_grounding import score_grounding

    assert score_grounding(str(corpus)) == 0

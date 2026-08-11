"""Image stripping: painting and placeholder consistency.

Painting tests run on hand-built OcrPages (no OCR engine); real-engine OCR
round-trips live in the paddle adapter tests (test_ocr_paddle.py).

These exercise the painting path with layer 0 finding NOTHING, so the plan
comes from layer 1 alone — which is what these assertions have always been
about. `strip_from_page` (the old layers entry point) went with GLiNER2 on
2026-08-09; `_strip` below is its exact equivalent through the surviving seam.
"""

from PIL import Image, ImageDraw

from pii.core.image_mode import (
    Segment,
    _grow,
    paint_segments,
    strip_from_vlm,
)
from pii.core.linearization import linearize
from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box
from pii.core.ocr_page import OcrFrame, build_page

RED = (255, 0, 0)


def _strip(image, page, pipeline, pmap, findings=()):
    """Strip an OcrPage with no layer-0 findings — layer 1 supplies the whole
    plan, exactly as the retired `strip_from_page` did."""
    return strip_from_vlm(
        image, list(findings), pipeline, pmap, ocr=linearize(page)
    )


def _colors(image, box):
    region = image.crop((box.left, box.top, box.right, box.bottom))
    return {color for _, color in region.getcolors(box.width * box.height)}


def _page(rows, width=400, height=200):
    return build_page(rows, OcrFrame(width=width, height=height, page=1))


def test_strip_layer1_paints_over_pii_pixels(pipeline):
    email_box = Box(left=60, top=20, width=120, height=14)
    img = Image.new("RGB", (300, 60), "white")
    ImageDraw.Draw(img).rectangle(
        (email_box.left, email_box.top, email_box.right, email_box.bottom),
        fill=RED,
    )
    page = _page(
        [
            [
                ("Pay", Box(10, 20, 30, 14), 90.0),
                ("olga@example.com", email_box, 90.0),
                ("now", Box(200, 20, 30, 14), 90.0),
            ]
        ],
        width=300, height=60,
    )
    pmap = PseudonymMap()
    result = _strip(img, page, pipeline, pmap)

    assert [r.entity_type for r in result.spans] == ["EMAIL_ADDRESS"]
    # The email's pixels are gone...
    assert RED not in _colors(result.image, email_box)
    # ...non-PII regions are untouched...
    assert _colors(result.image, Box(200, 20, 30, 14)) == {(255, 255, 255)}
    # ...the input image was not mutated, and the mapping was allocated.
    assert RED in _colors(img, email_box)
    assert pmap.placeholder_for("EMAIL_ADDRESS", "olga@example.com") == "EMAIL_1"


def test_strip_layer1_consistent_placeholder_across_lines(pipeline):
    boxes = [Box(10, 10, 120, 12), Box(10, 40, 120, 12)]
    img = Image.new("RGB", (200, 70), "white")
    for b in boxes:
        ImageDraw.Draw(img).rectangle((b.left, b.top, b.right, b.bottom), fill=RED)
    page = _page(
        [
            [("olga@example.com", boxes[0], 90.0)],
            [("olga@example.com", boxes[1], 90.0)],
        ],
        width=200, height=70,
    )
    pmap = PseudonymMap()
    result = _strip(img, page, pipeline, pmap)

    assert len(result.spans) == 2
    assert len(pmap) == 1  # one placeholder, both occurrences
    for b in boxes:
        assert RED not in _colors(result.image, b)


def test_paint_segments_paints_labels_without_detection():
    boxes = [Box(20, 10, 100, 14), Box(20, 40, 100, 14)]
    img = Image.new("RGB", (200, 70), "white")
    for b in boxes:
        ImageDraw.Draw(img).rectangle((b.left, b.top, b.right, b.bottom), fill=RED)
    out = paint_segments(
        img,
        [Segment("PERSON_1", [boxes[0]]), Segment("ACCOUNT_1", [boxes[1]])],
    )
    for b in boxes:
        colors = _colors(out, b)
        assert RED not in colors  # covered
        assert (0, 0, 0) in colors  # label ink drawn
    assert RED in _colors(img, boxes[0])  # input not mutated


def test_paint_segments_frame_style_keeps_content_readable():
    box = Box(40, 30, 100, 14)
    img = Image.new("RGB", (200, 70), "white")
    ImageDraw.Draw(img).rectangle((box.left, box.top, box.right, box.bottom), fill=RED)
    out = paint_segments(img, [Segment("PERSON_1", [box])], style="frame")
    inner = Box(box.left + 6, box.top + 6, box.width - 12, box.height - 12)
    assert RED in _colors(out, inner)  # content under the frame survives
    from pii.core.image_mode import _FRAME_COLOR

    assert _FRAME_COLOR in _colors(out, _grow(box, 2, out))  # outline drawn


def test_paint_segments_skips_degenerate_box_and_warns():
    # Belt-and-suspenders backstop: a segment box that survives to
    # paint_segments with an inverted (negative-width) rectangle must not
    # crash the page (the "ServletRetrieve (6).pdf" failure was Image.new
    # rejecting a negative dimension). It is skipped, but loudly.
    import warnings

    img = Image.new("RGB", (200, 60), "white")
    bad = Box(left=150, top=10, width=-40, height=14)  # right=110 < left
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = paint_segments(img, [Segment("PHONE_1", [bad])])
    # No crash, nothing painted (page stays white), and a warning names it.
    assert _colors(out, Box(0, 0, 200, 60)) == {(255, 255, 255)}
    assert any(
        issubclass(w.category, RuntimeWarning) and "PHONE_1" in str(w.message)
        for w in caught
    )


def test_paint_segments_mixed_good_and_degenerate(pipeline):
    # A degenerate box alongside a valid one: the good box still paints, the
    # bad one is skipped — one bad span never sinks the rest of the page.
    good = Box(left=20, top=10, width=100, height=14)
    bad = Box(left=180, top=30, width=-30, height=14)
    img = Image.new("RGB", (220, 60), "white")
    ImageDraw.Draw(img).rectangle(
        (good.left, good.top, good.right, good.bottom), fill=RED
    )
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = paint_segments(img, [Segment("PERSON_1", [good, bad])])
    assert RED not in _colors(out, good)  # good box covered
    assert (0, 0, 0) in _colors(out, good)  # label ink drawn


def test_grow_clamps_to_image_bounds():
    img = Image.new("RGB", (100, 50))
    grown = _grow(Box(0, 0, 10, 10), 2, img)
    assert grown == Box(left=0, top=0, width=12, height=12)
    grown = _grow(Box(95, 45, 5, 5), 2, img)
    assert grown == Box(left=93, top=43, width=7, height=7)


# --- the whole page reaches the recognizer as one string --------------


def test_context_promotes_across_lines_on_the_whole_page(pipeline):
    # 'BSB' one line above the digits it promotes. The page is fed to the
    # recognizer whole, so the promotion fires — this is what the retired
    # per-block feed gave up, and why it is not coming back without evidence.
    img = Image.new("RGB", (400, 200), "white")
    page = _page([
        [("BSB", Box(10, 10, 60, 20), 90.0)],
        [("014-936", Box(10, 110, 120, 20), 90.0)],
    ])
    result = _strip(img, page, pipeline, PseudonymMap())
    assert [r.entity_type for r in result.spans] == ["AU_BSB"]


def test_spans_address_the_page_text(pipeline):
    img = Image.new("RGB", (400, 200), "white")
    page = _page([
        [("Hello", Box(10, 10, 60, 20), 90.0)],
        [("Contact", Box(10, 110, 80, 20), 90.0),
         ("olga@example.com", Box(100, 110, 170, 20), 90.0)],
    ])
    result = _strip(img, page, pipeline, PseudonymMap())
    assert [r.entity_type for r in result.spans] == ["EMAIL_ADDRESS"]
    span = result.spans[0]
    assert result.ocr.text[span.start : span.end] == "olga@example.com"
    assert result.ocr.text == "Hello\nContact olga@example.com"


def test_placeholders_are_numbered_in_document_order(pipeline):
    img = Image.new("RGB", (400, 200), "white")
    page = _page([
        [("first@example.com", Box(10, 10, 170, 20), 90.0)],
        [("second@example.com", Box(10, 110, 180, 20), 90.0)],
    ])
    pmap = PseudonymMap()
    result = _strip(img, page, pipeline, pmap)
    assert len(result.spans) == 2
    assert pmap.placeholder_for("EMAIL_ADDRESS", "first@example.com") == "EMAIL_1"
    assert pmap.placeholder_for("EMAIL_ADDRESS", "second@example.com") == "EMAIL_2"


def test_strip_layer1_paints_only_the_detected_pixels(pipeline):
    email_box = Box(left=100, top=110, width=170, height=20)
    img = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(img).rectangle(
        (email_box.left, email_box.top, email_box.right, email_box.bottom),
        fill=RED,
    )
    page = _page([
        [("Hello", Box(10, 10, 60, 20), 90.0)],
        [("Contact", Box(10, 110, 80, 20), 90.0),
         ("olga@example.com", email_box, 90.0)],
    ])
    result = _strip(img, page, pipeline, PseudonymMap())
    assert RED not in _colors(result.image, email_box)
    assert _colors(result.image, Box(10, 10, 60, 20)) == {(255, 255, 255)}


# --- the group vote reaches the strip plan --------------------------------


def test_the_group_vote_relabels_the_pages_that_disagreed(pipeline):
    """The elected class replaces every member's own, in BOTH directions.

    Deliberate (Sergei, 2026-08-11): a majority for a kept class keeps the
    value even on the reading that called it PII. That makes this the one
    mechanism in the tool that can un-redact, which is why the tally is
    carried on the result and printed by the CLI."""
    from pii.core.vlm import VlmFinding

    page = _page(
        [
            [
                ("Paid", Box(10, 20, 40, 14), 90.0),
                ("Budget", Box(60, 20, 60, 14), 90.0),
                ("Direct", Box(130, 20, 60, 14), 90.0),
            ]
        ],
        width=300, height=60,
    )
    image = Image.new("RGB", (300, 60), "white")

    def strip(*types):
        return strip_from_vlm(
            image,
            [VlmFinding(text="Budget Direct", entity_type=t) for t in types],
            pipeline, PseudonymMap(), ocr=linearize(page),
        )

    # Two-to-one for the kept class: the merchant name survives everywhere.
    assert strip("ORGANIZATION", "ORGANIZATION", "PERSON").spans == []
    # Two-to-one the other way: it strips everywhere.
    assert [
        s.entity_type for s in strip("PERSON", "PERSON", "ORGANIZATION").spans
    ] == ["PERSON"]


def test_a_value_named_once_is_painted_at_every_occurrence(pipeline):
    """Layer 0 places one span per finding, so a value printed twice and named
    once used to leave the second printing legible."""
    from pii.core.vlm import VlmFinding

    page = _page(
        [
            [
                ("SERGEI", Box(10, 20, 60, 14), 90.0),
                ("KULIK", Box(80, 20, 50, 14), 90.0),
                ("and", Box(140, 20, 30, 14), 90.0),
                ("SERGEI", Box(180, 20, 60, 14), 90.0),
                ("KULIK", Box(250, 20, 50, 14), 90.0),
            ]
        ],
        width=400, height=60,
    )
    result = strip_from_vlm(
        Image.new("RGB", (400, 60), "white"),
        [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")],
        pipeline, PseudonymMap(), ocr=linearize(page),
    )
    assert [
        result.ocr.text[s.start : s.end] for s in result.spans
    ] == ["SERGEI KULIK", "SERGEI KULIK"]
    assert len(result.borrowed) == 1

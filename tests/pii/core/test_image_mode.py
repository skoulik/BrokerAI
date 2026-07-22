"""Image stripping: painting and placeholder consistency.

Painting tests run on constructed OcrResults (no OCR engine); real-engine
OCR round-trips live in the paddle worker tests (test_ocr_worker.py)."""

from PIL import Image, ImageDraw

from pii.core.image_mode import Segment, _grow, paint_segments, strip_from_ocr
from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box, assemble

RED = (255, 0, 0)


def _colors(image, box):
    region = image.crop((box.left, box.top, box.right, box.bottom))
    return {color for _, color in region.getcolors(box.width * box.height)}


def test_strip_from_ocr_paints_over_pii_pixels(pipeline):
    email_box = Box(left=60, top=20, width=120, height=14)
    img = Image.new("RGB", (300, 60), "white")
    ImageDraw.Draw(img).rectangle(
        (email_box.left, email_box.top, email_box.right, email_box.bottom),
        fill=RED,
    )
    ocr = assemble(
        [
            [
                ("Pay", Box(10, 20, 30, 14), 90.0),
                ("olga@example.com", email_box, 90.0),
                ("now", Box(200, 20, 30, 14), 90.0),
            ]
        ]
    )
    pmap = PseudonymMap()
    result = strip_from_ocr(img, ocr, pipeline, pmap)

    assert [r.entity_type for r in result.spans] == ["EMAIL_ADDRESS"]
    # The email's pixels are gone...
    assert RED not in _colors(result.image, email_box)
    # ...non-PII regions are untouched...
    assert _colors(result.image, Box(200, 20, 30, 14)) == {(255, 255, 255)}
    # ...the input image was not mutated, and the mapping was allocated.
    assert RED in _colors(img, email_box)
    assert pmap.placeholder_for("EMAIL_ADDRESS", "olga@example.com") == "EMAIL_1"


def test_strip_from_ocr_consistent_placeholder_across_lines(pipeline):
    boxes = [Box(10, 10, 120, 12), Box(10, 40, 120, 12)]
    img = Image.new("RGB", (200, 70), "white")
    for b in boxes:
        ImageDraw.Draw(img).rectangle((b.left, b.top, b.right, b.bottom), fill=RED)
    ocr = assemble(
        [
            [("olga@example.com", boxes[0], 90.0)],
            [("olga@example.com", boxes[1], 90.0)],
        ]
    )
    pmap = PseudonymMap()
    result = strip_from_ocr(img, ocr, pipeline, pmap)

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

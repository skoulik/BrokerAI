"""OCR repair from a PDF's own text layer, and the font traceback that rides
the same pairing.

Model-free and OCR-free: the alignment takes an `OcrPage` and a list of
`TextWord`s, so a page's OCR can be written by hand with exactly the damage
under test, and the text layer comes from a real pymupdf document built by
`insert_text`. Both halves are therefore real — the extraction runs against an
actual text layer, the repair against an actual `OcrPage` — with nothing
stochastic in between.

Each gate has its own test, because every one of them rejects something no
other one catches (see `_same_reading` / `_repairable` and the module docstring
in pii.core.text_layer)."""

import pymupdf
import pytest

from pii.core.linearization import linearize
from pii.core.ocr import Box
from pii.core.ocr_page import FontSpec, OcrFrame, OcrLine, OcrWord, OcrPage
from pii.core.text_layer import (
    TextWord,
    _overlaps,
    page_text_words,
    repair_page,
)


def _page(rows, rotations=None) -> OcrPage:
    """An OcrPage from (text, box) rows — one OcrLine per row. `rotations`
    gives the rows' rotations, defaulting to upright."""
    lines = []
    for index, row in enumerate(rows):
        rotation = rotations[index] if rotations else 0
        words = tuple(
            OcrWord(text=text, box=box, rotation=rotation) for text, box in row
        )
        left = min(w.box.left for w in words)
        top = min(w.box.top for w in words)
        lines.append(
            OcrLine(
                text=" ".join(w.text for w in words),
                box=Box(
                    left, top,
                    max(w.box.right for w in words) - left,
                    max(w.box.bottom for w in words) - top,
                ),
                words=words,
                rotation=rotation,
            )
        )
    return OcrPage(frame=OcrFrame(width=800, height=800, page=1),
                   lines=tuple(lines))


def _words(*items: tuple[str, Box]) -> tuple[TextWord, ...]:
    return tuple(TextWord(text=text, box=box) for text, box in items)


def _rotated_words(rotation, *items: tuple[str, Box]) -> tuple[TextWord, ...]:
    return tuple(
        TextWord(text=text, box=box, rotation=rotation) for text, box in items
    )


ACCOUNT = Box(100, 40, 120, 20)


# --- extraction ---


def _text_pdf(path, rotation=0, text="Account Number 018057571"):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((50, 100), text, fontsize=12)
    if rotation:
        page.set_rotation(rotation)
    doc.save(path)
    doc.close()
    return pymupdf.open(path)


def test_page_text_words_reads_the_layer_in_raster_pixels(tmp_path):
    doc = _text_pdf(tmp_path / "t.pdf")
    page = doc[0]
    words = page_text_words(page, dpi=144)  # 2x the 72pt page coordinates

    assert [w.text for w in words] == ["Account", "Number", "018057571"]
    # Page coordinates were (50, ~90-100) at 12pt; at 144 dpi that doubles.
    assert words[0].box.left == pytest.approx(100, abs=4)
    assert all(w.box.width > 0 and w.box.height > 0 for w in words)
    doc.close()


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_page_text_words_lands_on_the_ink_at_any_rotation(tmp_path, rotation):
    """The transform is the render matrix, not a scalar.

    `get_pixmap` applies /Rotate itself while `get_text` returns UNROTATED page
    coordinates, so a naive `x * dpi/72` puts a word hundreds of pixels from
    its own ink on a rotated page. Checked against the actual dark pixels of
    the actual render, which is the only ground truth that cannot drift."""
    from PIL import Image

    doc = _text_pdf(tmp_path / "t.pdf", rotation=rotation)
    page = doc[0]
    pix = page.get_pixmap(dpi=144, alpha=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    ink = image.convert("L").point(lambda v: 255 if v < 128 else 0).getbbox()

    boxes = [w.box for w in page_text_words(page, dpi=144)]
    left = min(b.left for b in boxes)
    top = min(b.top for b in boxes)
    right = max(b.right for b in boxes)
    bottom = max(b.bottom for b in boxes)
    # Glyph boxes are looser than the ink they contain, never tighter.
    assert left <= ink[0] and top <= ink[1]
    assert right >= ink[2] and bottom >= ink[3]
    doc.close()


def test_page_text_words_carries_the_writing_direction(tmp_path):
    """A page-edge stripe reads bottom-to-top on the left margin and
    top-to-bottom on the right; both occur in the reference corpus. The
    direction is read off the span's own `dir`, and checked here against where
    the words actually land: a bottom-to-top line puts its SECOND word higher
    up the page."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=400)
    page.insert_text((200, 300), "UPWARD one", fontsize=12, rotate=90)
    page.insert_text((100, 100), "DOWNWARD two", fontsize=12, rotate=270)
    page.insert_text((20, 380), "PLAIN three", fontsize=12)
    doc.save(tmp_path / "r.pdf")
    doc.close()
    doc = pymupdf.open(tmp_path / "r.pdf")

    words = {w.text: w for w in page_text_words(doc[0], dpi=72)}
    assert words["UPWARD"].rotation == 90
    assert words["one"].box.top < words["UPWARD"].box.top
    assert words["DOWNWARD"].rotation == 270
    assert words["two"].box.top > words["DOWNWARD"].box.top
    assert words["PLAIN"].rotation == 0
    doc.close()


def test_the_writing_direction_goes_through_the_render_matrix(tmp_path):
    """`dir` is in the same unrotated page coordinates as the boxes, so on a
    /Rotate 90 page every ordinary line reports `(1, 0)` while its ink runs
    down the raster. Un-composed, a whole page of body text would look rotated
    and would be pulled out of its rows."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=400)
    page.insert_text((20, 380), "PLAIN three", fontsize=12)
    doc.save(tmp_path / "p.pdf")
    doc.close()
    doc = pymupdf.open(tmp_path / "p.pdf")
    doc[0].set_rotation(90)

    words = {w.text: w for w in page_text_words(doc[0], dpi=72)}
    assert words["PLAIN"].rotation == 270
    assert words["three"].box.top > words["PLAIN"].box.top
    doc.close()


def test_page_text_words_carries_the_font(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((50, 60), "plain words", fontsize=11)
    page.insert_text((50, 120), "bold words", fontsize=11, fontname="hebo")
    doc.save(tmp_path / "f.pdf")
    doc.close()
    doc = pymupdf.open(tmp_path / "f.pdf")

    fonts = {w.text: w.font for w in page_text_words(doc[0], dpi=144)}
    assert fonts["plain"].bold is False
    assert fonts["bold"].bold is True
    # Size is raster pixels, not points: 11pt at 144 dpi is 22px.
    assert fonts["plain"].size == pytest.approx(22, abs=0.5)
    doc.close()


# --- the repair itself ---


def test_repairs_a_letter_read_for_a_digit():
    """The case the feature exists for: `\\b\\d{5,10}\\b` cannot start after a
    letter, so an account number OCR'd as `O18057571` matched no rule at all
    and survived a run unredacted."""
    page = _page([[("Account", Box(20, 40, 70, 20)), ("O18057571", ACCOUNT)]])
    repaired, report = repair_page(
        page, _words(("Account", Box(20, 40, 70, 20)),
                     ("018057571", ACCOUNT)),
    )

    assert repaired.lines[0].words[1].text == "018057571"
    assert repaired.lines[0].text == "Account 018057571"
    assert report.repaired == 1
    assert report.agreed == 2


def test_a_repair_keeps_the_same_words_in_the_same_order():
    """The word COUNT, the order and the REGION box are what painting, the
    source map and the pseudonym map are built from. Repair runs before
    `linearize` precisely so none of them has to be remapped — which only holds
    if it never touches them. (The word's own box may move; that is
    `_lendable`, tested below.)"""
    page = _page([[("O18057571", ACCOUNT), ("paid", Box(240, 40, 50, 20))]])
    repaired, _ = repair_page(
        page, _words(("018057571", ACCOUNT), ("paid", Box(240, 40, 50, 20))),
    )

    before = [w for line in page.lines for w in line.words]
    after = [w for line in repaired.lines for w in line.words]
    assert len(before) == len(after)
    assert [w.text for w in after] == ["018057571", "paid"]
    assert [w.region_box for w in before] == [w.region_box for w in after]


def test_repaired_text_reaches_the_linearized_page():
    page = _page([[("O18057571", ACCOUNT)]])
    repaired, _ = repair_page(page, _words(("018057571", ACCOUNT)))
    assert linearize(repaired).text == "018057571"


def test_marks_where_each_reading_came_from():
    page = _page([
        [("O18057571", ACCOUNT), ("Total", Box(240, 40, 50, 20))],
        [("logo", Box(20, 90, 40, 20))],
    ])
    repaired, _ = repair_page(
        page, _words(("018057571", ACCOUNT), ("Total", Box(240, 40, 50, 20))),
    )

    sources = {w.text: w.source for line in repaired.lines for w in line.words}
    assert sources == {"018057571": "text", "Total": "agreed", "logo": "ocr"}


# --- the gates, one test each ---


def test_declines_a_word_the_text_layer_covers_more_of():
    """A table-of-contents leader: the text layer puts thirty dots inside its
    word and OCR reads only the number. Squashing drops separators, so the two
    are at distance ZERO and only the extent gate can see it. Every such pair
    in the reference corpus is rejected here and by nothing else."""
    page = _page([[("185871", Box(100, 40, 60, 20))]])
    repaired, report = repair_page(
        page, _words(("185871" + "." * 30, Box(100, 40, 300, 20))),
    )

    assert repaired.lines[0].words[0].text == "185871"
    assert report.repaired == 0


def test_declines_a_reading_that_would_introduce_a_format_character():
    """A text layer can be WORSE than the OCR — which is why PDFs are treated
    as images at all. One reference statement renders a BSB with U+00AD (soft
    hyphen) where the page shows a hyphen; preferring it would delete the
    separator from `[ -]` and unmatch the rule."""
    page = _page([[("014-936", Box(100, 40, 70, 20))]])
    repaired, report = repair_page(
        page, _words(("014­936", Box(100, 40, 70, 20))),
    )

    assert repaired.lines[0].words[0].text == "014-936"
    assert report.repaired == 0


def test_declines_a_pair_whose_boxes_do_not_agree():
    """Alignment can propose a pair the geometry refuses: on the reference
    corpus it offered `O3`->`03` and `it's`->`it` between boxes that do not
    overlap at all."""
    page = _page([[("O3", Box(100, 40, 30, 20))]])
    repaired, report = repair_page(page, _words(("03", Box(600, 40, 30, 20))))

    assert repaired.lines[0].words[0].text == "O3"
    assert report.repaired == 0


def test_declines_an_unrelated_word_in_the_same_place():
    """The similarity gate. Position alone once offered OCR `944600` the
    text-layer word `000731114,` — swapping on position would replace a
    correct BSB with a different account number, a repair that MANUFACTURES a
    value."""
    page = _page([[("944600", ACCOUNT)]])
    repaired, report = repair_page(page, _words(("000731114,", ACCOUNT)))

    assert repaired.lines[0].words[0].text == "944600"
    assert report.repaired == 0


def test_does_not_repair_a_word_the_text_layer_splits():
    """OCR read one token where the text layer has three. The correspondence
    is real and keeps the rest of the line in step, but which characters belong
    where is exactly what is not established, so the reading stands."""
    page = _page([[("944600,000731114,RCPT:", Box(100, 40, 220, 20))]])
    repaired, report = repair_page(
        page,
        _words(("944600,", Box(100, 40, 70, 20)),
               ("000731114,", Box(175, 40, 95, 20)),
               ("RCPT:", Box(275, 40, 45, 20))),
    )

    assert repaired.lines[0].words[0].text == "944600,000731114,RCPT:"
    assert report.repaired == 0


def test_alignment_survives_boxes_that_drift_along_a_line():
    """Why the correspondence is an ALIGNMENT and not a nearest-box pairing.

    OCR word boxes are interpolated when the engine's fragments disagree with
    its line string, and on the first reference page measured they drifted by
    one word across a whole line: nearest-box paired `AND`->`ADVISE`,
    `ADVISE`->`US`, `US`->`PROMPTLY`. Every one of those is a wrong
    correspondence between two IDENTICAL sequences of words."""
    said = ["AND", "ADVISE", "US", "PROMPTLY"]
    page = _page([[(w, Box(100 + 90 * i, 40, 80, 20))
                   for i, w in enumerate(said)]])
    # Same words, boxes shifted most of a word to the right.
    repaired, report = repair_page(
        page,
        _words(*[(w, Box(160 + 90 * i, 40, 80, 20))
                 for i, w in enumerate(said)]),
    )

    assert [w.text for w in repaired.lines[0].words] == said
    assert report.agreed == 4
    assert report.repaired == 0


def test_refuses_a_text_layer_that_does_not_describe_the_page():
    """The page-level guard: a different revision of the document, or another
    tool's OCR baked in. Disabling is per PAGE and leaves the page exactly as
    OCR read it — no repair, and no font traceback either, since the same
    layer supplied both."""
    boxes = [Box(20 + 60 * i, 40, 50, 20) for i in range(30)]
    page = _page([[(f"word{i}", box) for i, box in enumerate(boxes)]])
    repaired, report = repair_page(
        page, _words(*[(f"zqx{i}vbn", box) for i, box in enumerate(boxes)]),
    )

    assert report.disabled is True
    assert repaired is page
    assert all(w.source == "ocr" for line in repaired.lines
               for w in line.words)


def test_no_text_layer_leaves_the_page_untouched():
    page = _page([[("O18057571", ACCOUNT)]])
    repaired, report = repair_page(page, ())
    assert repaired is page
    assert report.words == 1
    assert not report


# --- fonts ---


def test_font_rides_the_pairing_onto_words_and_lines():
    serif = FontSpec(name="Times", size=18.0, serif=True)
    page = _page([[("Total", Box(20, 40, 50, 20)), ("due", ACCOUNT)]])
    repaired, _ = repair_page(
        page,
        (TextWord("Total", Box(20, 40, 50, 20), serif),
         TextWord("due", ACCOUNT, serif)),
    )

    assert [w.font for w in repaired.lines[0].words] == [serif, serif]
    assert repaired.lines[0].font == serif


def test_line_font_is_the_one_most_of_its_words_carry():
    body = FontSpec(name="Arial", size=12.0)
    heading = FontSpec(name="Arial", size=24.0, bold=True)
    boxes = [Box(20 + 60 * i, 40, 50, 20) for i in range(3)]
    page = _page([[(f"w{i}", box) for i, box in enumerate(boxes)]])
    repaired, _ = repair_page(
        page,
        (TextWord("w0", boxes[0], heading),
         TextWord("w1", boxes[1], body),
         TextWord("w2", boxes[2], body)),
    )

    assert repaired.lines[0].font == body
    # ...while each word keeps its own.
    assert repaired.lines[0].words[0].font == heading


def test_font_for_span_takes_the_face_of_most_of_its_characters():
    body = FontSpec(name="Arial", size=12.0)
    heading = FontSpec(name="Arial", size=24.0, bold=True)
    page = OcrPage(
        frame=OcrFrame(width=800, height=200, page=1),
        lines=(
            OcrLine(
                text="A Wilhelmina Rutherford",
                box=Box(20, 40, 300, 20),
                words=(
                    OcrWord("A", Box(20, 40, 20, 20), font=heading),
                    OcrWord("Wilhelmina", Box(50, 40, 120, 20), font=body),
                    OcrWord("Rutherford", Box(180, 40, 120, 20), font=body),
                ),
            ),
        ),
    )
    ocr = linearize(page)
    assert ocr.font_for_span(0, len(ocr.text)) == body
    assert ocr.font_for_span(0, 1) == heading


# --- end to end, through strip_pdf ---


def test_strip_pdf_repairs_from_the_documents_own_text_layer(
    tmp_path, pipeline, monkeypatch, no_findings
):
    """The whole path: a real text layer in the source PDF, an OCR engine that
    misreads the leading digit of an account number, and a run that redacts it
    anyway.

    The damage is the measured one — a five-to-ten digit run cannot start after
    a letter, so with `O18057571` on the page layer 1 matches nothing at all and
    the value survives. `no_findings` keeps layer 0 silent, so what this
    asserts is exactly that: the repair is what put the value within reach of
    the recognizers."""
    import pii.core.pdf_mode as pdf_mode
    from pii.core.mapping import PseudonymMap
    from pii.core.ocr_page import OcrFrame, build_page
    from pii.core.pdf_mode import strip_pdf

    src = tmp_path / "acct.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((50, 100), "Account Number 018057571", fontsize=12)
    doc.save(src)
    doc.close()

    # dpi=72 makes pixel coordinates equal point coordinates. The boxes sit
    # where pymupdf laid the glyphs out, so the text layer and this "OCR"
    # describe the same pixels.
    def damaged_ocr(image, lang="eng"):
        return build_page(
            [[
                ("Account", Box(50, 90, 44, 12), 90.0),
                ("Number", Box(96, 90, 42, 12), 90.0),
                ("O18057571", Box(140, 90, 56, 12), 90.0),
            ]],
            OcrFrame(width=image.width, height=image.height, page=1),
        )

    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: damaged_ocr)
    result = strip_pdf(src, pipeline, PseudonymMap(), tmp_path / "out.pdf",
                       dpi=72, detector=no_findings)

    page_result = result.pages[0]
    assert "018057571" in page_result.ocr.text
    assert page_result.repair.repaired == 1
    assert [r.entity_type for r in page_result.spans] == ["AU_BANK_ACCOUNT"]


def test_strip_pdf_text_repair_off_leaves_the_damage(
    tmp_path, pipeline, monkeypatch, no_findings
):
    """The OCR-only baseline, and the leak it carries: with the repair off the
    same page reports NO detection at all."""
    import pii.core.pdf_mode as pdf_mode
    from pii.core.mapping import PseudonymMap
    from pii.core.ocr_page import OcrFrame, build_page
    from pii.core.pdf_mode import strip_pdf

    src = tmp_path / "acct.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=200)
    page.insert_text((50, 100), "Account Number 018057571", fontsize=12)
    doc.save(src)
    doc.close()

    def damaged_ocr(image, lang="eng"):
        return build_page(
            [[
                ("Account", Box(50, 90, 44, 12), 90.0),
                ("Number", Box(96, 90, 42, 12), 90.0),
                ("O18057571", Box(140, 90, 56, 12), 90.0),
            ]],
            OcrFrame(width=image.width, height=image.height, page=1),
        )

    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: damaged_ocr)
    result = strip_pdf(src, pipeline, PseudonymMap(), tmp_path / "out.pdf",
                       dpi=72, detector=no_findings, text_repair=False)

    page_result = result.pages[0]
    assert "O18057571" in page_result.ocr.text
    assert page_result.repair.words == 0
    assert page_result.spans == []


# --- lending the box, not only the characters ---

# A drifted line: the OCR read every word correctly but boxed each one further
# right than the last, all inside ONE detection region. The measured shape on
# `ServletRetrieve (6).pdf` p1, scaled down — shifts there ran 10, 22, ..., 158
# along the footer and reset to 12 at the next region.
REGION = Box(100, 40, 500, 20)


def _drifted_page(texts, shifts):
    words = tuple(
        OcrWord(text=text, box=Box(100 + 80 * i + shift, 40, 70, 20),
                region_box=REGION)
        for i, (text, shift) in enumerate(zip(texts, shifts))
    )
    line = OcrLine(text=" ".join(texts), box=REGION, words=words)
    return OcrPage(frame=OcrFrame(width=800, height=200, page=1),
                   lines=(line,))


def _true_words(texts):
    return tuple(
        TextWord(text=text, box=Box(100 + 80 * i, 40, 70, 20))
        for i, text in enumerate(texts)
    )


def test_a_confirmed_pair_lends_its_box():
    """The case Sergei found: OCR reads the credit licence number correctly and
    boxes it a word to the right, so the painted box covers 24.5% of its digits
    and destroys the address beside it instead. A paddle word box is an
    estimate; a text-layer box is where the renderer drew the glyphs."""
    texts = ["AUSTRALIAN", "CREDIT", "LICENCE", "244616."]
    page = _drifted_page(texts, [10, 40, 70, 95])
    repaired, report = repair_page(page, _true_words(texts))

    assert [w.box for w in repaired.lines[0].words] == [
        t.box for t in _true_words(texts)
    ]
    assert report.relocated == 4
    assert report.repaired == 0  # every reading was already right


def test_the_horizontal_gate_is_not_applied_to_lending():
    """A word drifted by MORE than its own width overlaps its true box by less
    than the character gate allows — 0.25 for `244616.`. Geometry cannot be both
    the evidence for identity and the thing being corrected, so identity comes
    from the alignment and the reading."""
    page = _drifted_page(["ABN", "244616."], [0, 95])
    repaired, _ = repair_page(page, _true_words(["ABN", "244616."]))

    moved = repaired.lines[0].words[1]
    assert moved.box.left == 180  # the true position, not 275
    assert _overlaps(Box(275, 40, 70, 20), moved.box) is False


def test_a_box_may_not_leave_its_own_detection_region():
    """The bound on how far a correction may travel. The drift is a stretch
    INSIDE a paddle detection region — shifts reset at the next one — so the
    region is exactly the distance a lent box may move, and nothing can fly
    across the page."""
    page = _drifted_page(["ABN", "244616."], [0, 95])
    far = (TextWord("ABN", Box(100, 40, 70, 20)),
           TextWord("244616.", Box(1400, 40, 70, 20)))
    repaired, report = repair_page(page, far)

    assert repaired.lines[0].words[1].box.left == 275  # unchanged
    assert report.relocated == 0  # and 'ABN' was already where it belongs


def test_a_partner_on_another_row_lends_nothing():
    """Vertical agreement is the identity check the drift does not break: the
    error being corrected is horizontal, so a partner on a different printed row
    is a mis-assignment rather than a drift."""
    page = _drifted_page(["ABN", "244616."], [0, 0])
    other_row = (TextWord("ABN", Box(100, 40, 70, 20)),
                 TextWord("244616.", Box(180, 140, 70, 20)))
    repaired, _ = repair_page(page, other_row)

    assert repaired.lines[0].words[1].box.top == 40


def test_a_merge_lends_its_union_but_not_its_characters():
    """OCR read one token where the text layer has two. Which characters belong
    where is not established, so the reading stands — but the EXTENT is exactly
    established, and leaving the merge on drifted coordinates while its
    neighbours move puts two coordinate systems on one line: a lent box then
    starts inside an unlent one, which over-painted a neighbouring word by
    217 px on a real page."""
    region = Box(100, 40, 800, 20)
    words = (
        OcrWord("Repayment(from", Box(300, 40, 360, 20), region),
        OcrWord("944600", Box(700, 40, 140, 20), region),
    )
    page = OcrPage(
        frame=OcrFrame(width=1000, height=200, page=1),
        lines=(OcrLine(text="Repayment(from 944600",
                       box=Box(100, 40, 800, 20), words=words),),
    )
    repaired, report = repair_page(page, (
        TextWord("Repayment", Box(280, 40, 200, 20)),
        TextWord("(from", Box(490, 40, 100, 20)),
        TextWord("944600", Box(600, 40, 140, 20)),
    ))

    merged = repaired.lines[0].words[0]
    assert merged.text == "Repayment(from"  # characters untouched
    assert (merged.box.left, merged.box.right) == (280, 590)  # union extent
    assert repaired.lines[0].words[1].box.left == 600
    assert report.repaired == 0
    assert report.relocated == 2


def test_a_word_the_text_layer_never_saw_keeps_its_own_box():
    page = _drifted_page(["ABN", "logo"], [0, 95])
    repaired, _ = repair_page(page, (TextWord("ABN", Box(100, 40, 70, 20)),))
    assert repaired.lines[0].words[1].box.left == 275


# --- rotated lines: a stripe pairs with the stripe, and with nothing else ---


def _stripe_page():
    """A page-edge stripe, and a footer line that RUNS THROUGH its column.

    The footer's words sit inside the stripe's y-range and across its x-range,
    so each is a candidate for the other's bucket on whichever axis is
    measured, and the stripe is offered first. Only the rotation match keeps
    them apart."""
    return _page(
        [
            [("XPRCAPOO22", Box(40, 100, 30, 500))],
            [("LNDSTMN7", Box(20, 500, 110, 20)),
             ("RTBLP14O", Box(140, 500, 110, 20))],
        ],
        rotations=[90, 0],
    )


def test_a_stripe_does_not_collect_the_text_words_it_crosses():
    """The footer word that runs through the stripe's column is the one at
    risk: bucketed onto the stripe it leaves its OWN line unpaired, and the
    damage that repair exists to fix survives on the footer."""
    page = _stripe_page()
    repaired, report = repair_page(
        page,
        _words(("LNDSTMNT", Box(20, 500, 110, 20)),
               ("RTBLP140", Box(140, 500, 110, 20))),
    )
    assert [w.text for w in repaired.lines[1].words] == ["LNDSTMNT", "RTBLP140"]
    # ...and neither reached the stripe, whose reading stands.
    assert repaired.lines[0].words[0].text == "XPRCAPOO22"
    assert report.words == 3 and report.paired == 2 and report.repaired == 2


def test_a_stripe_is_repaired_from_a_stripe():
    """The rotation match is a gate, not a refusal: a text word printed the
    same way up pairs normally, and repairs the reading. `1.pdf`'s own text
    layer carries the exact stripe string its OCR misreads."""
    page = _stripe_page()
    repaired, report = repair_page(
        page, _rotated_words(90, ("XPRCAP0022", Box(40, 100, 30, 500))),
    )
    assert repaired.lines[0].words[0].text == "XPRCAP0022"
    assert report.repaired == 1


def test_a_horizontal_line_is_not_repaired_from_a_stripe():
    """The gate runs both ways: a word the text layer says is printed sideways
    never reaches a horizontal line, however exactly it sits on it. The same
    pair repairs when the text word is upright (the test above)."""
    page = _stripe_page()
    repaired, report = repair_page(
        page, _rotated_words(90, ("RTBLP140", Box(140, 500, 110, 20))),
    )
    assert repaired.lines[1].words[1].text == "RTBLP14O"
    assert report.paired == 0 and report.repaired == 0


def test_a_stripe_pairs_in_reading_order_up_the_page():
    """A bucket is sorted along the LINE, which for a bottom-to-top stripe runs
    up the page: sorted by `left` — every word of a stripe shares one — the
    alignment would see the text layer's words in an arbitrary order."""
    page = _page(
        [[("1584.3694", Box(40, 300, 30, 100)),
          ("ZZ258R3", Box(40, 150, 30, 120))]],
        rotations=[90],
    )
    repaired, report = repair_page(
        page,
        # Offered in PAGE order, down the raster — the order the stripe reads
        # in is the opposite one.
        _rotated_words(
            90,
            ("ZZ258R4", Box(40, 150, 30, 120)),
            ("1584.3694", Box(40, 300, 30, 100)),
        ),
    )
    assert [w.text for w in repaired.lines[0].words] == ["1584.3694", "ZZ258R4"]
    assert report.paired == 2 and report.repaired == 1

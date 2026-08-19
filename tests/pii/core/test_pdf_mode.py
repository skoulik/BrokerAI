"""PDF page rendering and reassembly.

Rendering tests are pure geometry (no OCR). The strip_pdf tests are
model-free like the image-mode suite: the real layer-1 pipeline (stubbed
NER) + a fake OCR engine injected at the pii.core.pdf_mode.get_ocr_page
seam, so what is asserted is the reassembly contract — page count/size, no
text layer, painted pixels, clean metadata, per-page results — and, for the
VLM detector, that the OCR seam is used for GEOMETRY while the injected
detector supplies the values."""

from pathlib import Path

import pymupdf
import pytest

import pii.core.pdf_mode as pdf_mode
from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box
from pii.core.ocr_page import OcrFrame, build_page
from pii.core.pdf_mode import pdf_page_count, pdf_to_images, strip_pdf
from pii.core.vlm import DetectorResult

A4 = (595, 842)  # points


def _make_pdf(path, pages=2):
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=A4[0], height=A4[1])
        page.insert_text((72, 72), f"page {i + 1}", fontsize=11)
    doc.save(path)
    doc.close()


def test_pdf_to_images_renders_all_pages_at_dpi(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_pdf(pdf, pages=3)
    images = list(pdf_to_images(pdf, dpi=144))
    assert len(images) == 3
    # 144 DPI = 2x the 72pt/inch page coordinates
    assert images[0].size == (A4[0] * 2, A4[1] * 2)
    assert images[0].mode == "RGB"


def test_pdf_to_images_pages_carry_content(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_pdf(pdf, pages=2)
    for img in pdf_to_images(pdf, dpi=96):
        colors = {c for _, c in img.getcolors(img.width * img.height)}
        assert (255, 255, 255) in colors  # page background
        assert len(colors) > 1  # ...and drawn text


def test_pdf_page_count(tmp_path):
    pdf = tmp_path / "doc.pdf"
    _make_pdf(pdf, pages=4)
    assert pdf_page_count(pdf) == 4


# --- strip_pdf ---

RED = (255, 0, 0)
EMAIL_BOX = Box(left=100, top=100, width=200, height=20)


def _make_marked_pdf(path, pages=2):
    """Pages with a red rectangle where the fake OCR will report an email
    (dpi=72 makes pixel coordinates == point coordinates)."""
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page(width=A4[0], height=A4[1])
        page.draw_rect(
            pymupdf.Rect(EMAIL_BOX.left, EMAIL_BOX.top,
                         EMAIL_BOX.right, EMAIL_BOX.bottom),
            color=(1, 0, 0), fill=(1, 0, 0),
        )
    doc.save(path)
    doc.close()


def _fake_ocr(image, lang="eng"):
    """Two rows: a greeting, then the line carrying the email."""
    return build_page(
        [
            [("Hello", Box(20, 20, 60, 20), 90.0)],
            [
                ("Contact", Box(20, EMAIL_BOX.top, 60, 20), 90.0),
                ("olga@example.com", EMAIL_BOX, 90.0),
            ],
        ],
        OcrFrame(width=image.width, height=image.height, page=1),
    )


def _colors(image, box):
    region = image.crop((box.left, box.top, box.right, box.bottom))
    return {color for _, color in region.getcolors(box.width * box.height)}


def test_strip_pdf_reassembles_clean_pdf(tmp_path, pipeline, monkeypatch,
                                         no_findings):
    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _fake_ocr)
    src = tmp_path / "doc.pdf"
    out = tmp_path / "doc.clean.pdf"
    _make_marked_pdf(src, pages=2)
    pmap = PseudonymMap()
    seen = []
    result = strip_pdf(src, pipeline, pmap, out, dpi=72,
                       detector=no_findings,
                       progress=lambda n, c, phase: seen.append((n, c, phase)))

    # Two sweeps: every page is read before any page is redacted, which is
    # what lets the document group its findings in between.
    assert seen == [(1, 2, "read"), (2, 2, "read"),
                    (1, 2, "redact"), (2, 2, "redact")]
    assert len(result.pages) == 2
    for page_result in result.pages:
        assert [r.entity_type for r in page_result.spans] == ["EMAIL_ADDRESS"]
    # One placeholder for both occurrences — document-wide consistency.
    assert len(pmap) == 1

    with pymupdf.open(out) as doc:
        assert doc.page_count == 2
        for page in doc:
            # Physical page size preserved...
            assert (round(page.rect.width), round(page.rect.height)) == A4
            # ...and no text layer in the output — pixels only.
            assert page.get_text().strip() == ""
            # The page content is a single embedded JPEG (the final-embed
            # encoding decision).
            images = page.get_images()
            assert len(images) == 1
            assert doc.extract_image(images[0][0])["ext"] in ("jpeg", "jpg")
        # Nothing from the source document (or the library) in the
        # document info — 'format'/'encryption' are structural, not
        # source-derived.
        info = {k: v for k, v in doc.metadata.items()
                if k not in ("format", "encryption")}
        assert not any(info.values())


def test_strip_pdf_paints_over_pii_pixels(tmp_path, pipeline, monkeypatch,
                                          no_findings):
    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _fake_ocr)
    src = tmp_path / "doc.pdf"
    out = tmp_path / "doc.clean.pdf"
    _make_marked_pdf(src, pages=1)
    strip_pdf(src, pipeline, PseudonymMap(), out, dpi=72,
              detector=no_findings)

    page_image = next(pdf_to_images(out, dpi=72))
    # JPEG blurs edges; sample the box interior, which was solid red.
    inner = Box(EMAIL_BOX.left + 6, EMAIL_BOX.top + 6,
                EMAIL_BOX.width - 12, EMAIL_BOX.height - 12)
    assert RED not in _colors(page_image, inner)
    assert not _near_red(_colors(page_image, inner))


def _near_red(colors):
    return any(r > 200 and g < 100 and b < 100 for r, g, b in colors)


def test_strip_pdf_writes_one_companion_per_layer(
    tmp_path, pipeline, monkeypatch, no_findings
):
    """One file per requested layer, each the full document, each unredacted.

    Three properties, all load-bearing: a file per layer (combined, four layers
    on a statement page are unreadable), every page present in each (a
    companion that silently skipped a page would hide exactly the page a leak
    is on), and drawn on the ORIGINAL raster — the red block the strip painted
    over is still red here. The redacted output must be unaffected by asking."""
    from pii.core.debug_overlay import DebugSpec

    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _fake_ocr)
    src = tmp_path / "doc.pdf"
    out = tmp_path / "doc.clean.pdf"
    layers = ("ocr", "layer-0", "locate", "layer-1")
    spec = DebugSpec(layers=layers, path=tmp_path / "doc.clean.debug.pdf")
    _make_marked_pdf(src, pages=2)
    strip_pdf(src, pipeline, PseudonymMap(), out, dpi=72,
              detector=no_findings, debug=spec)

    written = spec.paths()
    assert [layer for layer, _ in written] == list(layers)
    assert [Path(p).name for _, p in written] == [
        "doc.clean.debug.ocr.pdf", "doc.clean.debug.layer-0.pdf",
        "doc.clean.debug.locate.pdf", "doc.clean.debug.layer-1.pdf",
    ]

    inner = Box(EMAIL_BOX.left + 6, EMAIL_BOX.top + 6,
                EMAIL_BOX.width - 12, EMAIL_BOX.height - 12)
    for _layer, path in written:
        with pymupdf.open(path) as doc:
            assert doc.page_count == 2
            for page in doc:
                assert (round(page.rect.width), round(page.rect.height)) == A4
                assert page.get_text().strip() == ""  # image-only, like strip
            info = {k: v for k, v in doc.metadata.items()
                    if k not in ("format", "encryption")}
            assert not any(info.values())
        # Unredacted: the pixels the clean output painted over are intact.
        assert _near_red(_colors(next(pdf_to_images(path, dpi=72)), inner))
    # ...and the clean output is still redacted.
    assert not _near_red(_colors(next(pdf_to_images(out, dpi=72)), inner))


def test_strip_pdf_writes_no_companion_without_a_debug_spec(
    tmp_path, pipeline, monkeypatch, no_findings
):
    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _fake_ocr)
    src = tmp_path / "doc.pdf"
    _make_marked_pdf(src, pages=1)
    strip_pdf(src, pipeline, PseudonymMap(), tmp_path / "doc.clean.pdf",
              dpi=72, detector=no_findings)
    assert list(tmp_path.glob("*.debug.*")) == []


def test_strip_pdf_reports_page_text_and_offsets(tmp_path, pipeline,
                                                 monkeypatch, no_findings):
    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _fake_ocr)
    src = tmp_path / "doc.pdf"
    _make_marked_pdf(src, pages=1)
    result = strip_pdf(src, pipeline, PseudonymMap(), tmp_path / "out.pdf",
                       dpi=72, detector=no_findings)
    (page_result,) = result.pages
    assert page_result.ocr.text == "Hello\nContact olga@example.com"
    span = page_result.spans[0]
    assert page_result.ocr.text[span.start : span.end] == "olga@example.com"


# --- the VLM detector: OCR runs for GEOMETRY, the model supplies values ---


class _FakeDetector:
    """Layer 0 without a model server: returns the same finding per page.

    `localize` is the second pass of the hybrid regime; this stand-in adds no
    boxes, which is also the real fallback when the model declines to place a
    value — the locator then searches the page string unconstrained."""

    def __init__(self, findings):
        self.findings = findings
        self.calls = 0
        self.localize_calls = 0

    def detect(self, image):
        self.calls += 1
        return DetectorResult(list(self.findings))

    def localize(self, image, findings):
        self.localize_calls += 1
        return DetectorResult(list(findings))


def test_strip_pdf_vlm_detector_uses_ocr_for_geometry(tmp_path, pipeline,
                                                      monkeypatch):
    from pii.core.vlm import VlmFinding

    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _fake_ocr)
    src = tmp_path / "doc.pdf"
    out = tmp_path / "doc.clean.pdf"
    _make_marked_pdf(src, pages=2)
    detector = _FakeDetector(
        [VlmFinding(text="olga@example.com", entity_type="IDENTIFIER_GENERIC")]
    )
    pmap = PseudonymMap()
    result = strip_pdf(src, pipeline, pmap, out, dpi=72, detector=detector)

    assert detector.calls == 2  # one detection call per page...
    assert detector.localize_calls == 2  # ...and one grounding call per page
    for page_result in result.pages:
        # Layer 1 refined the model's coarse class off the OCR text.
        assert [r.entity_type for r in page_result.spans] == ["EMAIL_ADDRESS"]
        span = page_result.spans[0]
        assert page_result.ocr.text[span.start : span.end] == "olga@example.com"
    assert len(pmap) == 1

    page_image = next(pdf_to_images(out, dpi=72))
    inner = Box(EMAIL_BOX.left + 6, EMAIL_BOX.top + 6,
                EMAIL_BOX.width - 12, EMAIL_BOX.height - 12)
    assert not _near_red(_colors(page_image, inner))


def test_strip_pdf_vlm_geometry_never_runs_ocr(tmp_path, pipeline, monkeypatch):
    from pii.core.vlm import VlmFinding

    monkeypatch.setattr(pdf_mode, "get_ocr_page", _unreachable)
    src = tmp_path / "doc.pdf"
    _make_marked_pdf(src, pages=1)
    detector = _FakeDetector(
        [VlmFinding(text="x", entity_type="PERSON", box=(10, 10, 200, 60))]
    )
    result = strip_pdf(src, pipeline, PseudonymMap(), tmp_path / "out.pdf",
                       dpi=72, detector=detector, geometry="vlm")
    # No OCR text, so no offsets — the painted segments are the only record.
    assert result.pages[0].ocr is None
    assert [s.label for s in result.pages[0].segments] == ["PERSON_1"]


def _unreachable(*args, **kwargs):
    raise AssertionError("OCR must not run on this path")


# --- two sweeps: what one page learns, every page uses -------------------


class _FirstPageOnlyDetector:
    """Names a value on page 1 and says nothing on any later page — the
    symptom the two-sweep pipeline exists for. The value is printed just as
    plainly on page 2, and a per-page pipeline had nothing that could notice."""

    def __init__(self, findings):
        self.findings = findings
        self.calls = 0

    def detect(self, image):
        self.calls += 1
        found = list(self.findings) if self.calls == 1 else []
        return DetectorResult(found)

    def localize(self, image, findings):
        return DetectorResult(list(findings))


def _name_ocr(image, lang="eng"):
    """One row, the same on every page: 'Client SERGEI KULIK'."""
    return build_page(
        [
            [
                ("Client", Box(20, 20, 60, 20), 90.0),
                ("SERGEI", Box(90, 20, 60, 20), 90.0),
                ("KULIK", Box(160, 20, 50, 20), 90.0),
            ]
        ],
        OcrFrame(width=image.width, height=image.height, page=1),
    )


def test_a_value_detected_on_one_page_is_redacted_on_all_of_them(
    tmp_path, pipeline, monkeypatch
):
    from pii.core.vlm import VlmFinding

    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _name_ocr)
    src = tmp_path / "doc.pdf"
    _make_pdf(src, pages=2)
    detector = _FirstPageOnlyDetector(
        [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")]
    )
    pmap = PseudonymMap()
    result = strip_pdf(src, pipeline, pmap, tmp_path / "out.pdf", dpi=72,
                       detector=detector)

    first, second = result.pages
    for page_result in (first, second):
        span = page_result.spans[0]
        assert page_result.ocr.text[span.start : span.end] == "SERGEI KULIK"
        assert span.entity_type == "PERSON"
    # Page 1 found it itself; page 2 owes it entirely to the document.
    assert first.borrowed == []
    assert len(second.borrowed) == 1
    # One value, one group, one placeholder across the document.
    (group,) = result.groups
    assert group.entity_type == "PERSON"
    assert group.pages == (1,)
    assert len(pmap) == 1


class _CoupleThenInitials:
    """Page 1 names the couple; page 2 carries only the initials form.

    The split is the whole point: nothing on page 2 says who E and J are, and
    nothing on page 1 carries the initials form, so neither page can do this
    alone. `locate_borrowed` cannot help either — its needles are values some
    layer READ, and `E & J MOORE` was read by nobody.
    """

    def __init__(self):
        self.calls = 0

    def __call__(self, image, lang="eng"):
        self.calls += 1
        frame = OcrFrame(width=image.width, height=image.height,
                         page=self.calls)
        if self.calls == 1:
            words = [("Holders", 20), ("Emily", 90), ("Moore", 150),
                     ("and", 210), ("John", 250), ("Moore", 300)]
        else:
            words = [("Rent", 20), ("E", 70), ("&", 95), ("J", 120),
                     ("MOORE", 150)]
        return build_page(
            [[(w, Box(x, 20, 45, 20), 90.0) for w, x in words]], frame
        )


def test_pass_two_derives_from_the_whole_document_not_one_page(
    tmp_path, pipeline, monkeypatch
):
    """Layer-1 pass 2 learns document-wide (2026-08-19).

    Until then it rebuilt its pool from ONE page's spans, so `E & J MOORE` on
    the transaction page derived nothing unless Emily and John happened to be
    named on that same page — the per-occurrence defect
    `image_mode.layer1_needles` was created for on 2026-08-18, left behind by
    that change because the architecture note of the day said sweep 2 was "the
    only place `derived.py` can run".
    """
    from pii.core.vlm import VlmFinding

    monkeypatch.setattr(pdf_mode, "get_ocr_page",
                        lambda backend: _CoupleThenInitials())
    src = tmp_path / "doc.pdf"
    _make_pdf(src, pages=2)
    detector = _FirstPageOnlyDetector(
        [VlmFinding(text="Emily Moore and John Moore", entity_type="PERSON")]
    )
    result = strip_pdf(src, pipeline, PseudonymMap(), tmp_path / "out.pdf",
                       dpi=72, detector=detector)

    first, second = result.pages
    assert [
        (s.entity_type, first.ocr.text[s.start:s.end]) for s in first.spans
    ] == [("PERSON_JOINT", "Emily Moore and John Moore")]
    # The page that knows nobody: the joint form is reachable only from the
    # other page's people, and the surname is NOT emitted separately because
    # the joint span already covers it and carries the better label.
    assert [
        (s.entity_type, second.ocr.text[s.start:s.end]) for s in second.spans
    ] == [("PERSON_JOINT", "E & J MOORE")]


def test_a_derived_span_is_not_owed_to_the_borrowed_pass(
    tmp_path, pipeline, monkeypatch
):
    """Pass 2 and `locate_borrowed` are different mechanisms and must stay
    countable apart: one finds another printing of a value somebody read, the
    other derives a value nobody read. Page 2's joint form is the second, so
    it must not be reported as borrowed."""
    from pii.core.vlm import VlmFinding

    monkeypatch.setattr(pdf_mode, "get_ocr_page",
                        lambda backend: _CoupleThenInitials())
    src = tmp_path / "doc.pdf"
    _make_pdf(src, pages=2)
    detector = _FirstPageOnlyDetector(
        [VlmFinding(text="Emily Moore and John Moore", entity_type="PERSON")]
    )
    result = strip_pdf(src, pipeline, PseudonymMap(), tmp_path / "out.pdf",
                       dpi=72, detector=detector)
    assert result.pages[1].borrowed == []


def test_an_unfinished_read_is_recorded_against_its_own_page(
    tmp_path, pipeline, monkeypatch
):
    """Per page, not per document: the count says how much is missing, the
    page number says where to look. Grouping is a partial mitigation and not a
    fix — a value unique to the cut-off page has nothing to borrow from."""
    from pii.core.vlm import Incomplete, VlmFinding

    class _CutOffOnPageTwo:
        def __init__(self):
            self.calls = 0

        def detect(self, image):
            self.calls += 1
            if self.calls == 2:
                return DetectorResult([], Incomplete(truncated=1))
            return DetectorResult(
                [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")]
            )

        def localize(self, image, findings):
            return DetectorResult(list(findings))

    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _name_ocr)
    src = tmp_path / "doc.pdf"
    _make_pdf(src, pages=2)
    with pytest.warns(RuntimeWarning, match="cut off at the token budget"):
        result = strip_pdf(src, pipeline, PseudonymMap(),
                           tmp_path / "out.pdf", dpi=72,
                           detector=_CutOffOnPageTwo())

    first, second = result.pages
    assert not first.incomplete
    assert second.incomplete == Incomplete(truncated=1)
    # The document-wide sweep still covers this page for what page 1 knew —
    # mitigation, not repair.
    assert len(second.borrowed) == 1


def test_the_page_cache_does_not_outlive_the_run(tmp_path, pipeline,
                                                 monkeypatch, no_findings):
    # The cache holds full UNREDACTED pages — near-PII of the strongest kind.
    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _fake_ocr)
    real_cache_path = pdf_mode._cache_path
    cached = []

    def spy(cache, number):
        path = real_cache_path(cache, number)
        cached.append(path)
        return path

    monkeypatch.setattr(pdf_mode, "_cache_path", spy)
    src = tmp_path / "doc.pdf"
    _make_marked_pdf(src, pages=2)
    strip_pdf(src, pipeline, PseudonymMap(), tmp_path / "out.pdf", dpi=72,
              detector=no_findings)

    assert cached  # the cache was used at all
    # Unlinked as each page is embedded, and the directory removed on the way
    # out — nothing survives the run.
    assert not any(path.exists() for path in cached)
    assert not any(path.parent.exists() for path in cached)


def test_strip_pdf_writes_a_findings_listing_beside_the_overlays(
    tmp_path, pipeline, monkeypatch
):
    """A finding the model returns with no box appears on no overlay — the
    layer-0 layer draws the model's own box and there is none. It still
    reaches the plan and, through grouping, every other page, which is how a
    hallucinated value once painted a heading's hyphen with a placeholder of
    its own (2026-08-13). The listing is where those are visible, so it is
    written for the whole document, not per layer."""
    import json

    from pii.core.debug_overlay import DebugSpec
    from pii.core.vlm import VlmFinding

    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _fake_ocr)
    src = tmp_path / "doc.pdf"
    out = tmp_path / "doc.clean.pdf"
    _make_marked_pdf(src, pages=2)
    spec = DebugSpec(layers=("layer-0",), path=str(out.with_suffix(".debug.pdf")))
    detector = _FakeDetector(
        [VlmFinding(text="olga@example.com", entity_type="IDENTIFIER_GENERIC")]
    )
    strip_pdf(src, pipeline, PseudonymMap(), out, dpi=72,
              detector=detector, debug=spec)

    path = Path(spec.findings_path())
    assert path.name == "doc.clean.debug.findings.json"
    payload = json.loads(path.read_text("utf-8"))
    assert payload["summary"]["pages"] == 2
    # _FakeDetector adds no boxes, which is also the real fallback when the
    # model declines to place a value.
    assert payload["summary"]["without_box"] == payload["summary"]["findings"] == 2
    assert payload["summary"]["unplaced"] == 0
    assert [p["page"] for p in payload["pages"]] == [1, 2]
    (finding,) = payload["pages"][0]["findings"]
    assert finding["text"] == "olga@example.com" and finding["box"] is None
    assert finding["spans"][0]["text"] == "olga@example.com"


# --- the same, for what LAYER 1 learns -----------------------------------


def _labelled_then_bare_ocr(image, lang="eng"):
    """A bare account number printed twice, labelled only the first time.

    This is the shape that made the gap visible on a real statement: layer 1
    scores a bare digit run below threshold and the context boost is granted
    from the neighbourhood the occurrence sits in, so the SAME string clears
    the bar at one printing and not at the next.
    """
    rows = [
        [
            ("Account", Box(20, 20, 70, 20), 90.0),
            ("432103", Box(100, 20, 60, 20), 90.0),
        ],
        # Far enough down that the label above is out of reach: the `above`
        # band is V_ABOVE line heights, so a couple of rows' gap is what makes
        # the second printing genuinely unlabelled rather than merely bare.
        [
            ("Pc", Box(20, 300, 20, 20), 90.0),
            ("432103", Box(100, 300, 60, 20), 90.0),
        ],
    ]
    return build_page(
        rows, OcrFrame(width=image.width, height=image.height, page=1)
    )


def _bare_only_ocr(image, lang="eng"):
    """The same value, printed with no label anywhere on the page."""
    return build_page(
        [
            [
                ("Pc", Box(20, 20, 20, 20), 90.0),
                ("432103", Box(100, 20, 60, 20), 90.0),
            ]
        ],
        OcrFrame(width=image.width, height=image.height, page=1),
    )


def test_a_value_layer_1_scores_once_is_redacted_at_every_occurrence(
    tmp_path, pipeline, monkeypatch, no_findings
):
    """One page, one value, two printings — only one of them labelled.

    Layer 1 detects the labelled printing and nothing else; the bare one is
    recovered because the value is a document-wide needle. Before 2026-08-18
    the page was redacted INCONSISTENTLY, which is worse than a clean miss
    because it looks stripped.
    """
    monkeypatch.setattr(
        pdf_mode, "get_ocr_page", lambda backend: _labelled_then_bare_ocr
    )
    src = tmp_path / "doc.pdf"
    _make_pdf(src, pages=1)
    pmap = PseudonymMap()
    result = strip_pdf(src, pipeline, pmap, tmp_path / "out.pdf", dpi=72,
                       detector=no_findings)

    (page,) = result.pages
    hits = [
        s for s in page.spans
        if page.ocr.text[s.start : s.end] == "432103"
    ]
    assert len(hits) == 2
    # ONE value, so one placeholder — not ACCOUNT_1 and ACCOUNT_2.
    assert len(pmap) == 1
    # The second printing is owed to the pattern needle, and is counted apart
    # from what a layer-0 needle would have recovered.
    assert len(page.pattern_borrowed) == 1
    assert page.borrowed == []


def test_a_value_layer_1_scores_on_one_page_is_redacted_on_all_of_them(
    tmp_path, pipeline, monkeypatch, no_findings
):
    """The cross-page half, and the reason the needles are collected in sweep 1.

    Page 1 carries the label, page 2 does not. The needle list has to be
    complete before ANY page is redacted — layer 1 used to run only inside
    sweep 2, per page, after the list was already frozen.
    """
    pages = iter((_labelled_then_bare_ocr, _bare_only_ocr))

    def _by_page(image, lang="eng"):
        return next(pages)(image, lang)

    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _by_page)
    src = tmp_path / "doc.pdf"
    _make_pdf(src, pages=2)
    pmap = PseudonymMap()
    result = strip_pdf(src, pipeline, pmap, tmp_path / "out.pdf", dpi=72,
                       detector=no_findings)

    first, second = result.pages
    assert len(second.pattern_borrowed) == 1
    assert second.ocr.text[second.spans[0].start : second.spans[0].end] == "432103"
    assert len(pmap) == 1


def test_a_layer_1_needle_never_re_types_what_layer_1_said_here(
    tmp_path, pipeline, monkeypatch, no_findings
):
    """A pattern needle adds coverage; it must never re-classify.

    One licence number printed twice, labelled `AFSL` at one printing and
    `Credit Licence` at the other — a real statement footer. A borrowed span
    scores 1.0 and `_merge_overlaps` takes the strongest member, so admitting
    one over a printing layer 1 has already typed collapses both onto whichever
    class the first needle happened to carry. That is the grouping vote by
    another route, and keeping these needles out of `grouping` was supposed to
    prevent exactly it.
    """

    def _two_licences(image, lang="eng"):
        return build_page(
            [
                [
                    ("AFSL", Box(20, 20, 40, 20), 90.0),
                    ("234527", Box(70, 20, 60, 20), 90.0),
                ],
                [
                    ("Credit", Box(20, 300, 50, 20), 90.0),
                    ("Licence", Box(80, 300, 60, 20), 90.0),
                    ("234527", Box(150, 300, 60, 20), 90.0),
                ],
            ],
            OcrFrame(width=image.width, height=image.height, page=1),
        )

    monkeypatch.setattr(pdf_mode, "get_ocr_page", lambda backend: _two_licences)
    src = tmp_path / "doc.pdf"
    _make_pdf(src, pages=1)
    result = strip_pdf(src, pipeline, PseudonymMap(), tmp_path / "out.pdf",
                       dpi=72, detector=no_findings)

    (page,) = result.pages
    types = {
        s.entity_type for s in page.spans
        if page.ocr.text[s.start : s.end] == "234527"
    }
    assert types == {"AU_AFSL", "AU_CREDIT_LICENCE"}
    assert page.pattern_borrowed == []

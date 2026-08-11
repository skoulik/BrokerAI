"""PDF page rendering and reassembly.

Rendering tests are pure geometry (no OCR). The strip_pdf tests are
model-free like the image-mode suite: the real layer-1 pipeline (stubbed
NER) + a fake OCR engine injected at the pii.core.pdf_mode.get_ocr_page
seam, so what is asserted is the reassembly contract — page count/size, no
text layer, painted pixels, clean metadata, per-page results — and, for the
VLM detector, that the OCR seam is used for GEOMETRY while the injected
detector supplies the values."""

import pymupdf

import pii.core.pdf_mode as pdf_mode
from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box
from pii.core.ocr_page import OcrFrame, build_page
from pii.core.pdf_mode import pdf_page_count, pdf_to_images, strip_pdf

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


def test_rebuild_pdf_reassembles_all_pages_clean(tmp_path):
    # The debug-overlay PDF path: render every page, run a per-page image
    # transform, reassemble a fresh image-only PDF (no text layer, no source
    # metadata) — same discipline as strip_pdf, exercised with an identity
    # transform (no OCR).
    src = tmp_path / "src.pdf"
    _make_pdf(src, pages=3)
    seen = []

    def transform(number, image):
        seen.append((number, image.size))
        return image

    out = tmp_path / "out.pdf"
    pdf_mode.rebuild_pdf(src, out, transform, dpi=96)
    assert [n for n, _ in seen] == [1, 2, 3]
    doc = pymupdf.open(out)
    assert doc.page_count == 3
    assert doc[0].get_text().strip() == ""  # image-only: no text layer
    assert not doc.metadata.get("title") and not doc.metadata.get("author")


def test_rebuild_pdf_page_filter(tmp_path):
    src = tmp_path / "src.pdf"
    _make_pdf(src, pages=4)
    seen = []
    pdf_mode.rebuild_pdf(
        src, tmp_path / "out.pdf",
        lambda n, im: (seen.append(n) or im), dpi=72, pages={2, 4},
    )
    assert seen == [2, 4]
    assert pymupdf.open(tmp_path / "out.pdf").page_count == 2


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
        return list(self.findings)

    def localize(self, image, findings):
        self.localize_calls += 1
        return list(findings)


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
        return list(self.findings) if self.calls == 1 else []

    def localize(self, image, findings):
        return list(findings)


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

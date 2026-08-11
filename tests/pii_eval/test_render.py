"""Image-tier renderer + value-survival matcher.

Rendering tests are model-free (Pillow only); the OCR round-trip test
drives the real PaddleOCR engine and carries the gpu marker.
"""

import json
from pathlib import Path

import pytest
from PIL import Image

from pii_eval.render import (
    MONO_FONTS,
    format_csv_table,
    render,
    render_page,
)


def _mini_corpus(root: Path, seed: int = 5) -> Path:
    corpus = root / "text" / f"s{seed}"
    corpus.mkdir(parents=True)
    docs = {
        "legacy_00.txt": "ACCOUNT STATEMENT\n01JAN24 OPENING BALANCE  10.00",
        "loan_01.txt": "Applicant 1\n  Name: Olga Moore\n  TFN: 123 456 782",
        "tx_02.csv": "Date,Description,Amount\n01/01/2024,RENT 5 MILES ST,9.50",
    }
    for name, text in docs.items():
        (corpus / name).write_text(text, encoding="utf-8")
    truth = {
        "seed": seed,
        "docs": [
            {"file": name, "kind": "csv" if name.endswith("csv") else "text",
             "entities": []}
            for name in docs
        ],
    }
    (corpus / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    return corpus


def test_render_writes_pages_and_manifest(tmp_path):
    corpus = _mini_corpus(tmp_path)
    out = render(str(corpus), str(tmp_path / "image" / "s5"))

    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    assert [d["source"] for d in manifest["docs"]] == [
        "legacy_00.txt", "loan_01.txt", "tx_02.csv",
    ]
    for doc in manifest["docs"]:
        for name in doc["pages"]:
            page = Image.open(out / name)
            assert page.width > 100 and page.height > 100
        # ...and the same pages, assembled, so --modality pdf runs the
        # two-sweep pipeline over identical pixels.
        assert (out / doc["pdf"]).exists()
    # The manifest's source pointer resolves back to the text corpus.
    assert (out / manifest["source"]).resolve() == corpus.resolve()


def test_form_feeds_in_the_source_become_pages(tmp_path):
    # Pagination is described ONCE, in the document text, so the text tier and
    # the image tier cannot disagree about where a page ends.
    corpus = _mini_corpus(tmp_path)
    (corpus / "legacy_00.txt").write_text(
        "PAGE ONE\nrows here\fPAGE TWO\nmore rows\fPAGE THREE",
        encoding="utf-8",
    )
    out = render(str(corpus), str(tmp_path / "image" / "s5"))
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    by_source = {d["source"]: d for d in manifest["docs"]}
    assert by_source["legacy_00.txt"]["pages"] == [
        "legacy_00.p1.png", "legacy_00.p2.png", "legacy_00.p3.png",
    ]
    # Pages of one document share a raster size, so the analysis DPI cannot
    # differ from page to page.
    sizes = {
        Image.open(out / name).size
        for name in by_source["legacy_00.txt"]["pages"]
    }
    assert len(sizes) == 1


def test_a_csv_is_paginated_by_rows_with_its_header_repeated(tmp_path):
    # A form feed inside a CSV would break the parse, so these are cut here —
    # and the repeated line carries column names, not PII.
    from pii_eval.render import _CSV_ROWS_PER_PAGE, paginate

    header = "Date,Description,Amount"
    rows = [f"01/01/2024,ROW {i},{i}.00" for i in range(_CSV_ROWS_PER_PAGE + 5)]
    pages = paginate("\n".join([header, *rows]), is_csv=True)
    assert len(pages) == 2
    assert all(page.splitlines()[0] == header for page in pages)
    assert pages[1].splitlines()[1:] == rows[_CSV_ROWS_PER_PAGE:]


def test_fixed_column_docs_render_monospace(tmp_path):
    corpus = _mini_corpus(tmp_path)
    out = render(str(corpus), str(tmp_path / "image" / "s5"))
    manifest = json.loads((out / "manifest.json").read_text("utf-8"))
    for doc in manifest["docs"]:
        if doc["source"].startswith(("legacy", "tx")):
            assert doc["font"] in MONO_FONTS


def test_render_is_deterministic_per_seed(tmp_path):
    corpus = _mini_corpus(tmp_path)
    out1 = render(str(corpus), str(tmp_path / "a"))
    out2 = render(str(corpus), str(tmp_path / "b"))
    m1 = json.loads((out1 / "manifest.json").read_text("utf-8"))
    m2 = json.loads((out2 / "manifest.json").read_text("utf-8"))
    assert [
        (d["pages"], d["font"], d["size"]) for d in m1["docs"]
    ] == [(d["pages"], d["font"], d["size"]) for d in m2["docs"]]


def test_format_csv_table_aligns_columns():
    table = format_csv_table(
        "Date,Description,Amount\n01/01/2024,RENT 5 MILES ST,9.50\n"
    )
    lines = table.splitlines()
    # Every column starts at the same x offset on every row.
    assert lines[0].index("Description") == lines[1].index("RENT")
    assert lines[0].index("Amount") == lines[1].index("9.50")
    # Quoted-comma cells stay one cell (csv parse, not str.split).
    assert format_csv_table('a,"1,000",c').count("  ") == 2


class TestFindValue:
    # Imported lazily: pii_eval.score_image pulls in pii.core (presidio).
    @staticmethod
    def _find(value, text):
        from pii_eval.score_image import find_value

        return find_value(value, text)

    def test_exact_normalized(self):
        assert self._find("Olga  Moore", "paid to olga moore today") == "exact"

    def test_absent(self):
        assert self._find("088 356 153", "no digits here") is None

    def test_ocr_confusion_is_fuzzy(self):
        # 0->O, 1->l: classic OCR confusions still count as a leak.
        assert self._find("088 356 153", "TFN O88 356 l53") == "fuzzy"

    def test_one_glyph_error_in_long_value_is_fuzzy(self):
        assert self._find("6514 84651 7 5", "card 6514 84671 7 5") == "fuzzy"

    def test_short_values_match_exactly_only(self):
        # 3-letter suburbs would false-leak everywhere at edit distance 1.
        assert self._find("Kew", "the key is lost") is None
        assert self._find("Kew", "moved to Kew in May") == "exact"


@pytest.mark.gpu
@pytest.mark.slow
@pytest.mark.parametrize("font", ["consola.ttf", "times.ttf"])
def test_rendered_page_is_ocr_readable(font):
    from pii.core.linearization import linearize
    from pii.core.ocr import get_ocr_page

    page = render_page(
        "ACCOUNT STATEMENT\nTFN: 123 456 782", font, 24
    )
    text = linearize(get_ocr_page("paddle:v6_medium")(page)).text
    assert "STATEMENT" in text
    assert "456" in text

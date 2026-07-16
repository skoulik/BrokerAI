"""Image-tier renderer + value-survival matcher.

Rendering tests are model-free (Pillow only); the OCR round-trip test
needs the system Tesseract binary and self-skips (same pattern as
tests/pii/core/test_image_mode.py).
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

_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")


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
        page = Image.open(out / doc["file"])
        assert page.width > 100 and page.height > 100
    # The manifest's source pointer resolves back to the text corpus.
    assert (out / manifest["source"]).resolve() == corpus.resolve()


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
        (d["file"], d["font"], d["size"]) for d in m1["docs"]
    ] == [(d["file"], d["font"], d["size"]) for d in m2["docs"]]


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
        # 0->O, 1->l: classic Tesseract confusions still count as a leak.
        assert self._find("088 356 153", "TFN O88 356 l53") == "fuzzy"

    def test_one_glyph_error_in_long_value_is_fuzzy(self):
        assert self._find("6514 84651 7 5", "card 6514 84671 7 5") == "fuzzy"

    def test_short_values_match_exactly_only(self):
        # 3-letter suburbs would false-leak everywhere at edit distance 1.
        assert self._find("Kew", "the key is lost") is None
        assert self._find("Kew", "moved to Kew in May") == "exact"


@pytest.mark.skipif(not _TESSERACT.exists(), reason="needs system Tesseract")
@pytest.mark.parametrize("font", ["consola.ttf", "times.ttf"])
def test_rendered_page_is_ocr_readable(font):
    from pii.core.ocr import ocr_image

    page = render_page(
        "ACCOUNT STATEMENT\nTFN: 123 456 782", font, 24
    )
    text = ocr_image(page).text
    assert "STATEMENT" in text
    assert "456" in text

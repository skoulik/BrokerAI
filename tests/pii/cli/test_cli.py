"""CLI argument plumbing: per-document map default and mode guards.

All failure-path tests assert SystemExit from parser.error, which fires
before any pipeline construction — nothing heavyweight loads here."""

from pathlib import Path

import pytest

from pii.cli import _derive_map, main


def test_derive_map_sits_next_to_input():
    assert _derive_map("docs/statement.pdf") == str(
        Path("docs/statement.pii_map.json")
    )


def test_derive_map_without_extension():
    assert _derive_map("statement") == "statement.pii_map.json"


def test_strip_stdin_requires_map():
    with pytest.raises(SystemExit):
        main(["strip", "-"])


def test_rehydrate_requires_map(tmp_path):
    with pytest.raises(SystemExit):
        main(["rehydrate", str(tmp_path / "answer.txt")])


def test_pdf_requires_output():
    with pytest.raises(SystemExit):
        main(["strip", "doc.pdf", "--pdf"])


def test_debug_ocr_overlay_requires_output():
    # parser.error fires before any OCR engine loads.
    with pytest.raises(SystemExit):
        main(["debug", "ocr", "page.png", "--format", "overlay"])


def test_debug_requires_subcommand():
    with pytest.raises(SystemExit):
        main(["debug"])


@pytest.mark.parametrize(
    "flags",
    [["--pdf", "--csv"], ["--pdf", "--image"], ["--image", "--csv"]],
)
def test_modes_mutually_exclusive(flags):
    with pytest.raises(SystemExit):
        main(["strip", "doc.bin", "-o", "out.bin", *flags])

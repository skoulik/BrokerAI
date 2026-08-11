"""CLI argument plumbing: per-document map default and mode guards.

All failure-path tests assert SystemExit from parser.error, which fires
before any pipeline construction — nothing heavyweight loads here."""

from pathlib import Path

import pytest

from pii.cli import _derive_debug_out, _derive_map, main


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


def test_derive_debug_out_sits_beside_the_output():
    assert _derive_debug_out("out/statement.clean.pdf") == str(
        Path("out/statement.clean.debug.pdf")
    )
    assert _derive_debug_out("page.png") == "page.debug.png"


def test_debug_rejects_text_input():
    # There is no page to draw on; guarded before the model server is touched.
    with pytest.raises(SystemExit):
        main(["strip", "doc.txt", "--debug", "all", "--map", "m.json"])


def test_debug_rejects_an_unknown_layer():
    # The 'level-0' spelling is the plausible typo — it must fail loudly
    # rather than produce an overlay missing the layer that was asked for.
    with pytest.raises(SystemExit):
        main(["strip", "doc.pdf", "--pdf", "-o", "out.pdf",
              "--debug", "ocr,level-0"])


@pytest.mark.parametrize(
    "flags",
    [["--pdf", "--csv"], ["--pdf", "--image"], ["--image", "--csv"]],
)
def test_modes_mutually_exclusive(flags):
    with pytest.raises(SystemExit):
        main(["strip", "doc.bin", "-o", "out.bin", *flags])

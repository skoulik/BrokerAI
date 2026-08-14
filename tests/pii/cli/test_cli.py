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


# ------------------------------------------------- layer 0 turned off

def test_layer0_off_rejects_geometry_vlm():
    """--geometry vlm never runs OCR, so with no semantic detector there is no
    text for layer 1 either: the run would write an unredacted copy of the
    input and look like it worked. Refused rather than silently re-geometried."""
    with pytest.raises(SystemExit):
        main(["strip", "page.png", "--image", "-o", "out.png",
              "--layer0", "off", "--geometry", "vlm"])


def test_layer0_off_needs_no_model_server(tmp_path, capsys):
    """The point of the flag. No fixture stubs the detector here — if this run
    contacted a server it would fail, so passing IS the assertion."""
    doc = tmp_path / "doc.txt"
    doc.write_text("Olga Petrova, TFN 123 456 782", encoding="utf-8")
    out = tmp_path / "out.txt"
    assert main([
        "strip", str(doc), "-o", str(out),
        "--map", str(tmp_path / "m.json"), "--layer0", "off",
    ]) == 0
    text = out.read_text(encoding="utf-8")
    assert "123 456 782" not in text
    # ...and what it costs, which the warning below is there to announce.
    assert "Olga Petrova" in text


def test_layer0_off_says_so_without_being_asked(tmp_path, capsys):
    """Not gated behind --report: what a run did not look for is not a
    reporting detail. An operator reading a plausible list of redacted
    identifiers must already have been told what is missing from it."""
    doc = tmp_path / "doc.txt"
    doc.write_text("nothing here", encoding="utf-8")
    main([
        "strip", str(doc), "-o", str(tmp_path / "out.txt"),
        "--map", str(tmp_path / "m.json"), "--layer0", "off",
    ])
    err = capsys.readouterr().err
    assert "--layer0 off" in err
    for entity in ("PERSON", "ADDRESS", "ORGANIZATION", "DATE_OF_BIRTH"):
        assert entity in err


def test_analyze_honours_layer0_off(tmp_path, capsys):
    doc = tmp_path / "doc.txt"
    doc.write_text("TFN 123 456 782", encoding="utf-8")
    assert main(["analyze", str(doc), "--layer0", "off"]) == 0
    assert "--layer0 off" in capsys.readouterr().err


def test_layer0_defaults_to_running(tmp_path, cli_no_model, capsys):
    """The regime must not be reachable by forgetting a flag — omitting
    --layer0 builds a real detector and prints no degradation warning."""
    doc = tmp_path / "doc.txt"
    doc.write_text("TFN 123 456 782", encoding="utf-8")
    main([
        "strip", str(doc), "-o", str(tmp_path / "out.txt"),
        "--map", str(tmp_path / "m.json"),
    ])
    assert "--layer0 off" not in capsys.readouterr().err


def _debug_args(layers, out="page.clean.png"):
    from argparse import Namespace

    return Namespace(debug=layers, debug_out=out)


def test_layer0_off_drops_the_overlays_that_would_be_blank(capsys):
    """layer-0 and locate are drawn from placements, so with no semantic
    detector they render as unannotated copies of the ORIGINAL page — extra
    near-PII files carrying no diagnostics. Dropped, not rendered blank."""
    from pii.cli import _debug_spec
    from pii.core.debug_overlay import DEBUG_LAYERS
    from pii.core.vlm import NullDetector

    spec = _debug_spec(_debug_args(DEBUG_LAYERS), NullDetector())
    assert spec.layers == ("ocr", "layer-1")
    assert spec.findings is False
    assert "layer-0, locate" in capsys.readouterr().err


def test_dropping_a_debug_layer_is_never_silent(capsys):
    """A shorter file list than asked for must be explained; the operator
    otherwise has to guess whether the run or the flag went wrong."""
    from pii.cli import _debug_spec
    from pii.core.vlm import NullDetector

    _debug_spec(_debug_args(("ocr", "locate")), NullDetector())
    assert "not written" in capsys.readouterr().err


def test_asking_only_for_layer_0_overlays_writes_nothing(capsys):
    """Nothing left to draw: writing no file beats writing a blank render of
    an unredacted page, and the note says why."""
    from pii.cli import _debug_spec
    from pii.core.vlm import NullDetector

    assert _debug_spec(_debug_args(("layer-0", "locate")), NullDetector()) is None
    assert "not written" in capsys.readouterr().err


def test_a_real_detector_keeps_every_requested_layer(capsys):
    """The suppression must not leak into an ordinary run — layer-0 is empty
    under --geometry ocr too, but there layer 0 RAN and locate is populated."""
    from pii.cli import _debug_spec
    from pii.core.debug_overlay import DEBUG_LAYERS
    from pii.core.vlm import VlmDetector

    spec = _debug_spec(_debug_args(DEBUG_LAYERS), VlmDetector())
    assert spec.layers == DEBUG_LAYERS
    assert spec.findings is True
    assert capsys.readouterr().err == ""


def test_the_debug_note_lists_no_findings_file_when_none_is_written(capsys):
    from pii.cli import _debug_note
    from pii.core.debug_overlay import DebugSpec

    _debug_note(DebugSpec(layers=("ocr",), path="p.png", findings=False))
    err = capsys.readouterr().err
    assert "findings" not in err
    assert "1 debug overlay(s) —" in err

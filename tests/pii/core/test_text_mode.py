"""Layer-0 text path: value location and the strip pipeline around it.

Model-free — a stub detector stands in for the LLM, so the suite never needs a
model server. The layer-1 half of every merge assertion is real: the pipeline
under test is the ordinary one, so the refinement and recall-floor claims are
exercised against genuine pattern recognizers.
"""

from __future__ import annotations

import pytest

from pii.core.locator import locate_in_text
from pii.core.mapping import PseudonymMap
from pii.core.text_mode import detect_text, strip_text
from pii.core.vlm import VlmFinding
from tests.conftest import StubDetector

VALID_TFN = "123 456 782"


def _spans(placements):
    return [span for p in placements for span in p.spans]


# --------------------------------------------------------------- location


def test_locates_every_occurrence_not_just_the_first():
    text = "Sergei Kulik paid rent. Later, Sergei Kulik paid again."
    placed = locate_in_text([VlmFinding("Sergei Kulik", "PERSON")], text)
    assert len(_spans(placed.located)) == 2
    assert all(text[s:e] == "Sergei Kulik" for s, e in _spans(placed.located))


def test_location_is_case_insensitive_and_keeps_original_offsets():
    text = "holder SERGEI KULIK; signed Sergei Kulik"
    placed = locate_in_text([VlmFinding("Sergei Kulik", "PERSON")], text)
    found = [text[s:e] for s, e in _spans(placed.located)]
    assert found == ["SERGEI KULIK", "Sergei Kulik"]


def test_squash_fallback_recovers_a_respaced_value():
    """The model re-spaced the value as it copied it; the digits are the
    same, so the value is still located."""
    text = "Account 162-097111-4 held at ANZ"
    placed = locate_in_text([VlmFinding("162 097111 4", "IDENTIFIER_GENERIC")], text)
    assert [text[s:e] for s, e in _spans(placed.located)] == ["162-097111-4"]
    assert placed.located[0].kind == "squash"


def test_exact_match_is_preferred_over_squash():
    text = "ref 162-097111-4 and 162 097111 4"
    placed = locate_in_text([VlmFinding("162 097111 4", "IDENTIFIER_GENERIC")], text)
    # The exact form exists, so the squash tier never runs and the
    # hyphenated occurrence is not claimed by this finding.
    assert placed.located[0].kind == "exact"
    assert [text[s:e] for s, e in _spans(placed.located)] == ["162 097111 4"]


def test_short_values_do_not_squash_match():
    """Squash ignores separators, so a short needle would match across word
    boundaries anywhere in the document — with no box to constrain it."""
    text = "the ANZ branch"
    placed = locate_in_text([VlmFinding("A N Z", "ORGANIZATION")], text)
    assert placed.located == []
    assert len(placed.unlocated) == 1


def test_no_length_floor_on_exact_matches():
    """Real 2-char surnames and 3-char organizations exist — the no-floor
    decision is recorded in core/ARCHITECTURE.md."""
    text = "paid to NAB by Ng"
    placed = locate_in_text(
        [VlmFinding("NAB", "ORGANIZATION"), VlmFinding("Ng", "PERSON")], text
    )
    assert len(placed.located) == 2


def test_value_absent_from_the_text_is_surfaced_not_dropped():
    placed = locate_in_text([VlmFinding("Nobody Here", "PERSON")], "clean text")
    assert placed.located == []
    assert [p.finding.text for p in placed.unlocated] == ["Nobody Here"]
    assert placed.unlocated[0].kind is None


def test_nested_findings_both_produce_spans():
    """Unlike the image path, a nested value is not suppressed: the overlap is
    unioned later by _merge_overlaps, and a 'John' elsewhere must still be
    marked."""
    text = "John Smith and his brother John"
    placed = locate_in_text(
        [VlmFinding("John Smith", "PERSON"), VlmFinding("John", "PERSON")],
        text,
    )
    assert len(_spans(placed.located)) == 3  # "John Smith", both "John"s


# ------------------------------------------------------------ strip path


def test_detector_is_required(pipeline):
    """Stripping a document without a semantic detector is the patterns-only
    regime retired 2026-07-15 as unsafe; no entry point may offer it."""
    with pytest.raises(TypeError):
        strip_text("text", pipeline, PseudonymMap())


def test_layer1_alone_when_the_detector_finds_nothing(pipeline):
    """Layer 1 is the deterministic floor: with layer 0 silent, the plan is
    still exactly what layer 1 sees."""
    text = f"TFN {VALID_TFN} for Olga"
    spans, invalid, unlocated, _ = detect_text(text, pipeline, StubDetector())
    plan, plan_invalid = pipeline.detect(text)
    assert [(s.entity_type, s.start, s.end) for s in spans] == [
        (s.entity_type, s.start, s.end) for s in plan
    ]
    assert invalid == plan_invalid
    assert unlocated == []


def test_strip_replaces_every_occurrence_with_one_placeholder(pipeline):
    text = "Sergei Kulik paid. Sergei Kulik paid again."
    result = strip_text(
        text, pipeline, PseudonymMap(),
        detector=StubDetector(("Sergei Kulik", "PERSON")),
    )
    assert "Sergei Kulik" not in result.text
    assert result.text.count("PERSON_1") == 2


def test_layer1_refines_a_generic_identifier(pipeline):
    """The detector emits the coarse class on purpose; layer 1 is what turns
    the digits into a checksum-validated AU_TFN."""
    text = f"Tax file number {VALID_TFN}"
    result = strip_text(
        text, pipeline, PseudonymMap(),
        detector=StubDetector((VALID_TFN, "IDENTIFIER_GENERIC")),
    )
    assert VALID_TFN not in result.text
    assert "TFN_1" in result.text
    assert "ID_1" not in result.text


def test_layer1_adds_what_the_detector_missed(pipeline):
    """The deterministic recall floor under a stochastic detector."""
    text = f"Contact olga@example.com about TFN {VALID_TFN}"
    result = strip_text(
        text, pipeline, PseudonymMap(), detector=StubDetector(),
    )
    assert "olga@example.com" not in result.text
    assert VALID_TFN not in result.text


def test_kept_organizations_survive_a_layer0_detection(pipeline):
    """The prompt carries no institutional carve-outs by design, so the model
    reports merchant names — the kept-ORGANIZATION policy is what leaves them
    alone, and it must reach layer-0 findings too."""
    text = "EFTPOS WOOLWORTHS NEWTOWN"
    result = strip_text(
        text, pipeline, PseudonymMap(),
        detector=StubDetector(("WOOLWORTHS", "ORGANIZATION")),
    )
    assert "WOOLWORTHS" in result.text


def test_private_organizations_still_strip(pipeline):
    text = "SK MANAGEMENT VICTORIA PTY LTD"
    result = strip_text(
        text, pipeline, PseudonymMap(),
        detector=StubDetector(("SK MANAGEMENT VICTORIA PTY LTD", "ORGANIZATION")),
    )
    assert "SK MANAGEMENT VICTORIA PTY LTD" not in result.text


def test_unlocated_findings_are_counted_and_warned(pipeline):
    """A detection we cannot place is a detection we cannot redact — the
    count must reach the caller, because Python's default warning filter
    deduplicates the message itself."""
    with pytest.warns(RuntimeWarning, match="NOT redacted"):
        result = strip_text(
            "clean text", pipeline, PseudonymMap(),
            detector=StubDetector(("Invented Name", "PERSON")),
        )
    assert [f.text for f in result.unlocated] == ["Invented Name"]


def test_detector_sees_the_whole_text(pipeline):
    detector = StubDetector()
    text = "line one\nline two\n"
    strip_text(text, pipeline, PseudonymMap(), detector=detector)
    assert detector.seen == [text]

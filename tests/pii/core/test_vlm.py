"""Layer-0 VLM detector: parsing, value location, and both geometry paths.

Model-free throughout — the transport is injected, so the suite never needs a
model server. Dual coverage per the project rule: the corpus probe is the other
half.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from PIL import Image

from pii.core.mapping import PseudonymMap
from pii.core.ocr import Box
from pii.core.vlm import (
    TYPE_MAP,
    VlmDetector,
    VlmError,
    VlmFinding,
    attach_boxes,
    fold_digits,
    parse_findings,
)


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _transport(content: str):
    seen = {}

    def send(url, payload, timeout):
        seen["url"] = url
        seen["payload"] = payload
        return _reply(content)

    send.seen = seen
    return send


# --------------------------------------------------------------- parsing


def test_parses_plain_array():
    found = parse_findings(
        '[{"text": "Sergei Kulik", "type": "PII_NAME"},'
        ' {"text": "162-097111-4", "type": "PII_IDENTIFIER"}]'
    )
    assert [f.text for f in found] == ["Sergei Kulik", "162-097111-4"]
    assert [f.entity_type for f in found] == ["PERSON", "IDENTIFIER_GENERIC"]
    assert all(f.box is None for f in found)


def test_parses_through_code_fence_and_prose():
    found = parse_findings(
        'Here you go:\n```json\n[{"text": "ANZ", "type": "PII_COMPANY"}]\n```'
    )
    assert [(f.text, f.entity_type) for f in found] == [("ANZ", "ORGANIZATION")]


def test_strips_thinking_block_containing_a_bracket():
    # A reasoning trace over a page of numbers contains '[', which would
    # otherwise capture the JSON scanner.
    raw = (
        "<think>the account [sic] looks like 162-0</think>"
        '[{"text": "162-097111-4", "type": "PII_IDENTIFIER"}]'
    )
    assert [f.text for f in parse_findings(raw)] == ["162-097111-4"]


def test_unknown_type_falls_back_to_generic():
    (found,) = parse_findings('[{"text": "X1", "type": "PII_WHATEVER"}]')
    assert found.entity_type == "IDENTIFIER_GENERIC"


def test_box_is_normalized_and_ordered():
    (found,) = parse_findings(
        '[{"text": "a", "type": "PII_NAME", "bbox_2d": [90, 80, 10, 20]}]'
    )
    assert found.box == (10, 20, 90, 80)


def test_malformed_box_is_dropped_but_finding_survives():
    # A bad box must not lose the detection — it can still be located via OCR.
    (found,) = parse_findings(
        '[{"text": "a", "type": "PII_NAME", "bbox_2d": ["x", 1, 2, 3]}]'
    )
    assert found.text == "a" and found.box is None


def test_unparseable_output_yields_nothing():
    assert parse_findings("I could not read this page.") == []


def test_non_ascii_digits_fold_to_ascii():
    # A clean render once decoded U+06F5 for '5': visually identical, breaks
    # value matching and checksums by string identity.
    assert fold_digits("162-09711۵") == "162-097115"
    (found,) = parse_findings('[{"text": "۵۵۵", "type": "PII_IDENTIFIER"}]')
    assert found.text == "555"


def test_every_mapped_type_is_a_real_placeholder_prefix():
    from pii.core.mapping import PLACEHOLDER_PREFIXES

    for entity in TYPE_MAP.values():
        assert entity in PLACEHOLDER_PREFIXES, entity


# --------------------------------------------------------------- transport


def test_detector_sends_image_and_prompt():
    send = _transport('[{"text": "A", "type": "PII_NAME"}]')
    det = VlmDetector(url="http://x:1", transport=send)
    found = det.detect(Image.new("RGB", (8, 8), "white"))

    assert [f.text for f in found] == ["A"]
    content = send.seen["payload"]["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "PII_IDENTIFIER" in content[1]["text"]
    # Determinism is a gate requirement, not a preference.
    assert send.seen["payload"]["temperature"] == 0.0
    assert send.seen["payload"]["top_k"] == 1


def test_boxes_are_only_requested_when_they_will_be_used():
    # Asking for coordinates measurably costs recall, so the OCR-geometry
    # path must not pay for boxes it will throw away.
    assert "bbox_2d" not in VlmDetector(want_boxes=False).prompt
    assert "bbox_2d" in VlmDetector(want_boxes=True).prompt


def test_unreachable_server_gives_an_actionable_message():
    # The model server usually runs on another host, so "wrong --vlm-url" is the
    # most likely failure; it must not surface as a urllib traceback.
    import urllib.error

    from pii.core.vlm import VlmUnavailable, http_transport

    def boom(*a, **kw):
        raise urllib.error.URLError("refused")

    with mock.patch("urllib.request.urlopen", boom):
        with pytest.raises(VlmUnavailable) as caught:
            http_transport("http://127.0.0.1:9", {}, 5)
    message = str(caught.value)
    assert "http://127.0.0.1:9" in message
    assert "--vlm-url" in message


def test_bad_response_shape_raises():
    det = VlmDetector(transport=lambda url, payload, timeout: {"nope": 1})
    with pytest.raises(VlmError):
        det.detect(Image.new("RGB", (4, 4), "white"))


# ------------------------------------------------------------ two-pass boxes
#
# Detection and grounding are separate calls because asking for both at once
# costs recall (350 -> 324 distinct values over 31 pages). Value location
# itself is tests/pii/core/test_locator.py.


def test_localize_asks_only_where_and_lists_the_values():
    send = _transport('[{"text": "A. Person", "bbox_2d": [10, 20, 30, 40]}]')
    det = VlmDetector(transport=send)
    findings = [VlmFinding(text="A. Person", entity_type="PERSON")]

    (out,) = det.localize(Image.new("RGB", (8, 8), "white"), findings)

    assert out.box == (10, 20, 30, 40)
    prompt = send.seen["payload"]["messages"][0]["content"][1]["text"]
    assert "A. Person" in prompt
    assert "WHERE" in prompt


def test_pass_one_never_asks_for_boxes_in_the_two_pass_regime():
    # The recall cost is paid by asking for coordinates ALONGSIDE detection;
    # the split exists precisely to avoid it, so pass 1 must stay clean.
    assert "bbox_2d" not in VlmDetector(want_boxes=False).prompt


def test_localize_makes_no_call_for_an_empty_page():
    def explode(*a, **kw):  # pragma: no cover - must not run
        raise AssertionError("no second pass without findings")

    assert VlmDetector(transport=explode).localize(None, []) == []


def test_attach_boxes_pairs_repeats_in_order():
    findings = [
        VlmFinding(text="24 Stacey Dr", entity_type="ADDRESS"),
        VlmFinding(text="24 Stacey Dr", entity_type="ADDRESS"),
    ]
    hints = [
        VlmFinding(text="24 Stacey Dr", entity_type="ADDRESS", box=(1, 1, 2, 2)),
        VlmFinding(text="24 Stacey Dr", entity_type="ADDRESS", box=(3, 3, 4, 4)),
    ]
    assert [f.box for f in attach_boxes(findings, hints)] == [
        (1, 1, 2, 2),
        (3, 3, 4, 4),
    ]


def test_attach_boxes_tolerates_a_mismatched_hint_count():
    # The model routinely returns a different number of boxes than there were
    # findings; pairing by position would silently mis-attach. A finding that
    # draws no hint keeps box=None and falls back to unconstrained search.
    findings = [
        VlmFinding(text="first", entity_type="PERSON"),
        VlmFinding(text="second", entity_type="PERSON"),
    ]
    hints = [
        VlmFinding(text="second", entity_type="PERSON", box=(3, 3, 4, 4)),
        VlmFinding(text="not asked for", entity_type="PERSON", box=(9, 9, 9, 9)),
    ]
    out = attach_boxes(findings, hints)
    assert out[0].box is None
    assert out[1].box == (3, 3, 4, 4)


def test_attach_boxes_matches_through_reformatting():
    # Pass 2 re-transcribes the value as it copies it back, so the two passes
    # need not agree on separators.
    findings = [VlmFinding(text="083-064", entity_type="IDENTIFIER_GENERIC")]
    hints = [
        VlmFinding(text="083 064", entity_type="IDENTIFIER_GENERIC",
                   box=(1, 2, 3, 4))
    ]
    assert attach_boxes(findings, hints)[0].box == (1, 2, 3, 4)


# --------------------------------------------------------------- geometry


def _ocr(text: str):
    """One word per token, laid out left to right on a single line, through
    the real perception -> linearization seam."""
    from pii.core.linearization import linearize
    from pii.core.ocr_page import OcrFrame, build_page

    row, x = [], 0
    for token in text.split(" "):
        row.append((token, Box(x, 0, 10 * len(token), 12), 99.0))
        x += 10 * len(token) + 10
    return linearize(build_page([row], OcrFrame(width=x, height=12, page=1)))


def _has_non_background(image, box) -> bool:
    """Whether anything was drawn inside `box`.

    paint_segments fills with the page BACKGROUND colour and draws the
    placeholder into it, so on a plain white test page the fill is invisible
    and only the label ink differs."""
    left, top, right, bottom = box
    crop = image.crop((left, top, right, bottom))
    return any(px != (255, 255, 255) for px in crop.getdata())


def test_ocr_geometry_paints_ocr_word_boxes(pipeline):
    from pii.core.image_mode import strip_from_vlm

    image = Image.new("RGB", (400, 40), "white")
    ocr = _ocr("pay SERGEI KULIK now")
    result = strip_from_vlm(
        image,
        [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")],
        pipeline,
        PseudonymMap(),
        ocr=ocr,
    )
    assert len(result.spans) == 1
    span = result.spans[0]
    assert ocr.text[span.start : span.end] == "SERGEI KULIK"
    assert result.ocr is ocr


def test_unlocatable_value_warns_and_is_not_silently_dropped(pipeline):
    from pii.core.image_mode import strip_from_vlm

    with pytest.warns(RuntimeWarning, match="could not be located"):
        result = strip_from_vlm(
            Image.new("RGB", (200, 40), "white"),
            [VlmFinding(text="NOT ON THE PAGE", entity_type="PERSON")],
            pipeline,
            PseudonymMap(),
            ocr=_ocr("something else entirely"),
        )
    assert result.spans == []
    # A count that reaches the caller, not only a warning that may be
    # deduplicated by the default filter on the next page.
    assert [f.text for f in result.unlocated] == ["NOT ON THE PAGE"]


def test_value_with_no_ocr_text_is_painted_from_the_model_box(pipeline):
    # Tier 3 — the logo/barcode case. --strip-orgs so the kept-ORGANIZATION
    # policy is not what decides the outcome here.
    from pii.core.image_mode import strip_from_vlm
    from pii.core.pipeline import DEFAULT_STRIP_ENTITIES

    image = Image.new("RGB", (1000, 1000), "white")
    with pytest.warns(RuntimeWarning, match="MODEL's own box"):
        result = strip_from_vlm(
            image,
            [VlmFinding("Budget Direct", "IDENTIFIER_GENERIC",
                        box=(700, 700, 900, 800))],
            pipeline,
            PseudonymMap(),
            ocr=_ocr("statement of account"),
        )
    assert [f.text for f in result.box_geometry] == ["Budget Direct"]
    assert result.unlocated == []
    assert _has_non_background(result.image, (700, 700, 900, 800))
    assert DEFAULT_STRIP_ENTITIES  # sanity: the strip list is non-empty


def test_a_kept_organization_is_not_painted_from_its_box(pipeline):
    # The prompt carries no institutional carve-outs by design, so the model
    # boxes merchant logos. The kept-ORGANIZATION policy has to reach tier 3
    # too, or the default run paints over every bank logo it sees.
    from pii.core.image_mode import strip_from_vlm

    result = strip_from_vlm(
        Image.new("RGB", (1000, 1000), "white"),
        [VlmFinding("Budget Direct", "ORGANIZATION", box=(700, 700, 900, 800))],
        pipeline,
        PseudonymMap(),
        ocr=_ocr("statement of account"),
    )
    assert result.box_geometry == []
    assert not _has_non_background(result.image, (700, 700, 900, 800))


def test_hybrid_geometry_runs_a_second_pass_and_uses_it(pipeline):
    # The dispatch: detect -> localize -> locate, with the box constraining
    # which of two identical values is claimed.
    from pii.core.image_mode import strip_rendered_page
    from pii.core.ocr_page import OcrFrame, build_page
    from pii.core.ocr import Box

    calls = []

    class FakeDetector:
        def detect(self, image):
            calls.append("detect")
            return [VlmFinding("SERGEI KULIK", "PERSON")]

        def localize(self, image, findings):
            calls.append("localize")
            return [VlmFinding("SERGEI KULIK", "PERSON", box=(0, 0, 300, 100))]

    def fake_ocr(image):
        # Two identical values, one inside the box and one outside it.
        row = [
            ("SERGEI", Box(0, 0, 60, 12), 99.0),
            ("KULIK", Box(70, 0, 50, 12), 99.0),
            ("SERGEI", Box(500, 0, 60, 12), 99.0),
            ("KULIK", Box(570, 0, 50, 12), 99.0),
        ]
        return build_page([row], OcrFrame(width=1000, height=120, page=1))

    result = strip_rendered_page(
        Image.new("RGB", (1000, 120), "white"),
        pipeline,
        PseudonymMap(),
        ocr_engine=fake_ocr,
        detector=FakeDetector(),
        geometry="hybrid",
    )
    assert calls == ["detect", "localize"]
    (span,) = result.spans
    assert result.ocr.text[span.start : span.end] == "SERGEI KULIK"
    assert span.start == 0  # the occurrence the box pointed at


def test_ocr_geometry_skips_the_second_pass(pipeline):
    # The pre-box baseline stays reachable, and must not pay for a pass whose
    # boxes it would ignore.
    from pii.core.image_mode import strip_rendered_page
    from pii.core.ocr_page import OcrFrame, build_page

    calls = []

    class FakeDetector:
        def detect(self, image):
            calls.append("detect")
            return []

        def localize(self, image, findings):  # pragma: no cover - must not run
            raise AssertionError("--geometry ocr must not run pass 2")

    strip_rendered_page(
        Image.new("RGB", (100, 40), "white"),
        pipeline,
        PseudonymMap(),
        ocr_engine=lambda im: build_page(
            [], OcrFrame(width=100, height=40, page=1)
        ),
        detector=FakeDetector(),
        geometry="ocr",
    )
    assert calls == ["detect"]


def test_unknown_geometry_is_rejected(pipeline):
    from pii.core.image_mode import strip_rendered_page

    class FakeDetector:
        def detect(self, image):  # pragma: no cover - never reached
            return []

    with pytest.raises(ValueError, match="unknown geometry"):
        strip_rendered_page(
            Image.new("RGB", (10, 10), "white"),
            pipeline,
            PseudonymMap(),
            detector=FakeDetector(),
            geometry="nonsense",
        )


def test_vlm_geometry_needs_no_ocr_and_scales_boxes(pipeline):
    from pii.core.image_mode import strip_from_vlm

    image = Image.new("RGB", (1000, 1000), "white")
    result = strip_from_vlm(
        image,
        [VlmFinding(text="X", entity_type="PERSON", box=(100, 200, 300, 260))],
        pipeline,
        PseudonymMap(),
        ocr=None,
        pad=0,
    )
    # No OCR ran, so there is no text and no offsets to report.
    assert result.ocr is None and result.spans == []
    # The model's 0-1000 box maps onto the right pixels, and only those.
    assert _has_non_background(result.image, (100, 200, 300, 260))
    assert not _has_non_background(result.image, (400, 400, 900, 900))


def test_vlm_geometry_skips_findings_without_a_box(pipeline):
    from pii.core.image_mode import strip_from_vlm

    result = strip_from_vlm(
        Image.new("RGB", (100, 100), "white"),
        [VlmFinding(text="X", entity_type="PERSON", box=None)],
        pipeline,
        PseudonymMap(),
        ocr=None,
    )
    assert not _has_non_background(result.image, (0, 0, 100, 100))


def test_identifier_generic_is_stripped_and_has_a_placeholder():
    from pii.core.mapping import PLACEHOLDER_PREFIXES
    from pii.core.pipeline import DEFAULT_STRIP_ENTITIES

    assert "IDENTIFIER_GENERIC" in DEFAULT_STRIP_ENTITIES
    assert PLACEHOLDER_PREFIXES["IDENTIFIER_GENERIC"] == "ID"
    assert PseudonymMap().placeholder_for("IDENTIFIER_GENERIC", "1938563911") == "ID_1"


# ------------------------------------------------ layer-1 refinement (step 2)
#
# The VLM emits ONE coarse identifier class on purpose; layer 1 is what turns
# a digit run into TFN/Medicare/ABN/BSB/account/card, restores the checksum
# shadows, and backstops what the model missed. All model-free: the stubbed
# GLiNER2 emits nothing, so what these assert is layer 1 alone.

VALID_TFN = "291 417 774"      # passes TFN mod-11 (mirrors test_invalid.py)
INVALID_TFN = "291 417 775"    # single-digit typo


def _strip(findings, text, pipeline, pmap=None):
    from pii.core.image_mode import strip_from_vlm

    ocr = _ocr(text)
    result = strip_from_vlm(
        Image.new("RGB", (900, 40), "white"), findings, pipeline,
        pmap or PseudonymMap(), ocr=ocr,
    )
    return result, ocr


def test_layer1_refines_identifier_generic_into_its_checksummed_class(pipeline):
    # The whole point of the coarse class: the model says "this is an
    # identifier", layer 1 says WHICH — so the placeholder is TFN_1, not ID_1.
    pmap = PseudonymMap()
    result, _ = _strip(
        [VlmFinding(text=VALID_TFN, entity_type="IDENTIFIER_GENERIC")],
        f"TFN {VALID_TFN} on file", pipeline, pmap,
    )
    assert [r.entity_type for r in result.spans] == ["AU_TFN"]
    assert pmap.placeholder_for("AU_TFN", VALID_TFN) == "TFN_1"


def test_layer1_restores_the_checksum_invalid_shadow(pipeline):
    # A signal the VLM structurally cannot produce: it can read a TFN, but
    # not verify its mod-11 arithmetic.
    result, _ = _strip(
        [VlmFinding(text=INVALID_TFN, entity_type="IDENTIFIER_GENERIC")],
        f"TFN {INVALID_TFN} on file", pipeline,
    )
    assert "AU_TFN_INVALID" in {f.entity_type for f in result.invalid}
    # ...and the value still strips, under the generic class it came in as.
    assert [r.entity_type for r in result.spans] == ["IDENTIFIER_GENERIC"]


def test_layer1_adds_what_the_model_missed(pipeline):
    # The deterministic recall floor under a stochastic detector: the VLM
    # reported only the name, but the email still gets stripped.
    result, ocr = _strip(
        [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")],
        "SERGEI KULIK olga@example.com", pipeline,
    )
    types = {r.entity_type for r in result.spans}
    assert types == {"PERSON", "EMAIL_ADDRESS"}
    for r in result.spans:
        assert ocr.text[r.start : r.end] in ("SERGEI KULIK", "olga@example.com")


def test_kept_organization_from_the_model_is_not_stripped(pipeline):
    # The prompt carries no institutional carve-outs on purpose, so the model
    # reports merchant names by design. The kept-ORGANIZATION policy is what
    # keeps them — applied to layer-0 findings exactly as to layer-1 ones.
    result, _ = _strip(
        [VlmFinding(text="WOOLWORTHS", entity_type="ORGANIZATION")],
        "paid WOOLWORTHS today", pipeline,
    )
    assert result.spans == []


def test_strip_orgs_still_reaches_model_findings(make_pipeline):
    # ...and the operator override still works through the same filter.
    from pii.core.pipeline import DEFAULT_STRIP_ENTITIES

    p = make_pipeline(strip_entities=DEFAULT_STRIP_ENTITIES | {"ORGANIZATION"})
    result, _ = _strip(
        [VlmFinding(text="WOOLWORTHS", entity_type="ORGANIZATION")],
        "paid WOOLWORTHS today", p,
    )
    assert [r.entity_type for r in result.spans] == ["ORGANIZATION"]

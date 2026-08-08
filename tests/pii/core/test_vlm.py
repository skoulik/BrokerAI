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
from pii.core.ocr import Box, OcrResult, OcrWord
from pii.core.vlm import (
    TYPE_MAP,
    VlmDetector,
    VlmError,
    VlmFinding,
    fold_digits,
    locate,
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


# --------------------------------------------------------------- locate


def test_locate_exact():
    assert locate("name: Sergei Kulik here", "Sergei Kulik") == (6, 18)


def test_locate_tolerates_formatting_differences():
    # The two transcriptions of the same pixels need not agree on separators.
    start, end = locate("BSB 083-064 acct", "083 064")
    assert "083-064" == "BSB 083-064 acct"[start:end]


def test_locate_returns_none_rather_than_guessing():
    assert locate("nothing relevant", "Sergei Kulik") is None


def test_repeated_value_maps_to_successive_occurrences():
    text = "24 Stacey Dr ... 24 Stacey Dr"
    first = locate(text, "24 Stacey Dr")
    second = locate(text, "24 Stacey Dr", taken=[first])
    assert first != second
    assert text[second[0] : second[1]] == "24 Stacey Dr"


# --------------------------------------------------------------- geometry


def _ocr(text: str) -> OcrResult:
    """One word per token, laid out left to right on a single line."""
    words, cursor, x = [], 0, 0
    for token in text.split(" "):
        words.append(
            OcrWord(
                text=token,
                box=Box(x, 0, 10 * len(token), 12),
                conf=99.0,
                line=0,
                char_start=cursor,
                char_end=cursor + len(token),
            )
        )
        cursor += len(token) + 1
        x += 10 * len(token) + 10
    return OcrResult(text=text, words=words)


def _has_non_background(image, box) -> bool:
    """Whether anything was drawn inside `box`.

    paint_segments fills with the page BACKGROUND colour and draws the
    placeholder into it, so on a plain white test page the fill is invisible
    and only the label ink differs."""
    left, top, right, bottom = box
    crop = image.crop((left, top, right, bottom))
    return any(px != (255, 255, 255) for px in crop.getdata())


def test_ocr_geometry_paints_ocr_word_boxes():
    from pii.core.image_mode import strip_from_vlm

    image = Image.new("RGB", (400, 40), "white")
    ocr = _ocr("pay SERGEI KULIK now")
    result = strip_from_vlm(
        image,
        [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")],
        PseudonymMap(),
        ocr=ocr,
    )
    assert len(result.spans) == 1
    span = result.spans[0]
    assert ocr.text[span.start : span.end] == "SERGEI KULIK"
    assert result.ocr is ocr


def test_unlocatable_value_warns_and_is_not_silently_dropped():
    from pii.core.image_mode import strip_from_vlm

    with pytest.warns(RuntimeWarning, match="could not be located"):
        result = strip_from_vlm(
            Image.new("RGB", (200, 40), "white"),
            [VlmFinding(text="NOT ON THE PAGE", entity_type="PERSON")],
            PseudonymMap(),
            ocr=_ocr("something else entirely"),
        )
    assert result.spans == []


def test_vlm_geometry_needs_no_ocr_and_scales_boxes():
    from pii.core.image_mode import strip_from_vlm

    image = Image.new("RGB", (1000, 1000), "white")
    result = strip_from_vlm(
        image,
        [VlmFinding(text="X", entity_type="PERSON", box=(100, 200, 300, 260))],
        PseudonymMap(),
        ocr=None,
        pad=0,
    )
    # No OCR ran, so there is no text and no offsets to report.
    assert result.ocr is None and result.spans == []
    # The model's 0-1000 box maps onto the right pixels, and only those.
    assert _has_non_background(result.image, (100, 200, 300, 260))
    assert not _has_non_background(result.image, (400, 400, 900, 900))


def test_vlm_geometry_skips_findings_without_a_box():
    from pii.core.image_mode import strip_from_vlm

    result = strip_from_vlm(
        Image.new("RGB", (100, 100), "white"),
        [VlmFinding(text="X", entity_type="PERSON", box=None)],
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

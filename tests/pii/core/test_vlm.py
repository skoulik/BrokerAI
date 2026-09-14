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
    ModelFamilyUnknown,
    GRAMMAR_LOCATE,
    GRAMMAR_VALUES,
    GRAMMAR_VALUES_BOXES,
    TYPE_MAP,
    DetectorResult,
    Incomplete,
    VlmDetector,
    VlmError,
    VlmFinding,
    attach_boxes,
    family_for_model,
    fold_digits,
    parse_findings,
    read_response,
)


# What llama-server reports as `model`: the path it loaded, by default. A reply
# always carries one, and box order is read from it (see BOX_ORDERS).
QWEN = "/Users/claude/models/qwen3.8-27b/Qwen3.8-27B-Q8_0.gguf"
GEMMA = "/Users/claude/models/gemma-4-26b-a4b/gemma-4-26B-A4B-it-Q8_0.gguf"


def _qwen(url, timeout):
    """A `served_model` stub: the server says it is serving Qwen."""
    return QWEN


def _reply(content: str, finish_reason: str = "stop", model: str | None = QWEN) -> dict:
    reply = {
        "choices": [
            {"message": {"content": content}, "finish_reason": finish_reason}
        ]
    }
    if model is not None:
        reply["model"] = model
    return reply


def _transport(content: str, model: str | None = QWEN):
    seen = {}

    def send(url, payload, timeout):
        seen["url"] = url
        seen["payload"] = payload
        return _reply(content, model=model)

    send.seen = seen
    return send


# --------------------------------------------------------------- parsing


def test_parses_plain_array():
    found = parse_findings(
        '[{"text": "Sergei Kulik", "type": "NAME"},'
        ' {"text": "162-097111-4", "type": "IDENTIFIER"}]'
    )
    assert [f.text for f in found] == ["Sergei Kulik", "162-097111-4"]
    assert [f.entity_type for f in found] == ["PERSON", "IDENTIFIER_GENERIC"]
    assert all(f.box is None for f in found)


def test_parses_through_code_fence_and_prose():
    found = parse_findings(
        'Here you go:\n```json\n[{"text": "ANZ", "type": "COMPANY"}]\n```'
    )
    assert [(f.text, f.entity_type) for f in found] == [("ANZ", "ORGANIZATION")]


def test_strips_thinking_block_containing_a_bracket():
    # A reasoning trace over a page of numbers contains '[', which would
    # otherwise capture the JSON scanner.
    raw = (
        "<think>the account [sic] looks like 162-0</think>"
        '[{"text": "162-097111-4", "type": "IDENTIFIER"}]'
    )
    assert [f.text for f in parse_findings(raw)] == ["162-097111-4"]


def test_unknown_type_falls_back_to_generic():
    (found,) = parse_findings('[{"text": "X1", "type": "PII_WHATEVER"}]')
    assert found.entity_type == "IDENTIFIER_GENERIC"


def test_box_is_normalized_and_ordered():
    (found,) = parse_findings(
        '[{"text": "a", "type": "NAME", "bbox_2d": [90, 80, 10, 20]}]'
    )
    assert found.box == (10, 20, 90, 80)


def test_malformed_box_is_dropped_but_finding_survives():
    # A bad box must not lose the detection — it can still be located via OCR.
    (found,) = parse_findings(
        '[{"text": "a", "type": "NAME", "bbox_2d": ["x", 1, 2, 3]}]'
    )
    assert found.text == "a" and found.box is None


def test_unparseable_output_yields_nothing():
    assert parse_findings("I could not read this page.") == []


def test_non_ascii_digits_fold_to_ascii():
    # A clean render once decoded U+06F5 for '5': visually identical, breaks
    # value matching and checksums by string identity.
    assert fold_digits("162-09711۵") == "162-097115"
    (found,) = parse_findings('[{"text": "۵۵۵", "type": "IDENTIFIER"}]')
    assert found.text == "555"


def test_every_mapped_type_is_a_real_placeholder_prefix():
    from pii.core.mapping import PLACEHOLDER_PREFIXES

    for entity in TYPE_MAP.values():
        assert entity in PLACEHOLDER_PREFIXES, entity


# ---------------------------------------------- truncation, and its salvage
#
# A repetition loop under greedy decode emits the same entry until the token
# budget runs out. The array never closes, so before finish_reason was read the
# result was an empty finding list — indistinguishable from a clean page, on a
# layer that is the ONLY detector for PERSON/ADDRESS/ORGANIZATION.


def test_a_clean_page_is_not_an_incomplete_one():
    result = read_response(_reply("[]"))
    assert result.findings == []
    assert not result.incomplete
    assert result.incomplete.truncated == 0


def test_a_cut_off_array_is_counted_as_truncated_not_read_as_empty():
    body = (
        '[{"text": "A", "type": "NAME"}, '
        '{"text": "B", "type": "NAME"}, {"text": "C'
    )
    result = read_response(_reply(body, finish_reason="length"))
    assert result.incomplete == Incomplete(truncated=1)


def test_a_cut_off_array_keeps_the_entries_that_completed():
    # The whole point of salvaging: a dense page that hit the budget after N
    # findings used to contribute none of them.
    body = (
        '[{"text": "A", "type": "NAME"}, '
        '{"text": "B", "type": "COMPANY"}, {"text": "C'
    )
    result = read_response(_reply(body, finish_reason="length"))
    assert [(f.text, f.entity_type) for f in result.findings] == [
        ("A", "PERSON"),
        ("B", "ORGANIZATION"),
    ]


def test_a_repetition_loop_collapses_to_one_finding():
    # Hundreds of copies of one value would otherwise arrive as hundreds of
    # separate "unredacted detection" warnings and bury the report.
    entry = '{"text": "AT06667873802666", "type": "IDENTIFIER"}'
    body = "[" + ", ".join([entry] * 200) + ', {"text": "AT066'
    result = read_response(_reply(body, finish_reason="length"))
    assert [f.text for f in result.findings] == ["AT06667873802666"]
    assert result.incomplete.truncated == 1


def test_repeats_that_differ_by_box_are_kept_apart_when_salvaging():
    # Two printings of one value are two occurrences, not a loop; only
    # byte-identical entries collapse.
    body = (
        '[{"text": "A", "bbox_2d": [1, 2, 3, 4]}, '
        '{"text": "A", "bbox_2d": [5, 6, 7, 8]}, {"text": "A'
    )
    result = read_response(_reply(body, finish_reason="length"))
    assert [f.box for f in result.findings] == [(1, 2, 3, 4), (5, 6, 7, 8)]


def test_commas_inside_an_entry_are_not_salvage_cut_points():
    # Cutting at the comma before "type" would truncate the object itself.
    body = '[{"text": "A", "type": "NAME"}, {"text": "B", "type'
    result = read_response(_reply(body, finish_reason="length"))
    assert [f.text for f in result.findings] == ["A"]


def test_a_single_incomplete_entry_salvages_nothing_but_still_counts():
    result = read_response(_reply('[{"text": "A", "ty', finish_reason="length"))
    assert result.findings == []
    assert result.incomplete.truncated == 1


def test_a_complete_answer_at_the_budget_is_not_called_truncated():
    # The array closed, so everything meaningful arrived; whatever the budget
    # cut was trailing.
    result = read_response(
        _reply('[{"text": "A", "type": "NAME"}]', finish_reason="length")
    )
    assert not result.incomplete
    assert len(result.findings) == 1


def test_a_finished_answer_that_is_not_json_is_counted_as_malformed():
    result = read_response(_reply("I could not read this page."))
    assert result.incomplete == Incomplete(malformed=1)


def test_truncated_and_malformed_are_different_counters():
    # They have different causes and only one has an operator-actionable fix,
    # so the report must not merge them.
    assert Incomplete(truncated=1) != Incomplete(malformed=1)
    assert (Incomplete(truncated=1) + Incomplete(malformed=2)) == Incomplete(1, 2)
    assert sum([Incomplete(truncated=1)] * 3) == Incomplete(truncated=3)


# ------------------------------------------- the trace, and its budget
#
# A trace that reached `reasoning_budget_tokens` is closed by REASONING_CUTOFF
# and the model answers in full. That is counted, because it is what a larger
# budget would change, but it is NOT a hole in the redaction.


def _thinking_reply(content: str, reasoning: str, **kwargs) -> dict:
    reply = _reply(content, **kwargs)
    reply["choices"][0]["message"]["reasoning_content"] = reasoning
    return reply


def test_a_trace_that_reached_the_budget_is_counted_but_is_not_a_hole():
    from pii.core.vlm import REASONING_CUTOFF

    result = read_response(_thinking_reply(
        '[{"text": "A", "type": "NAME"}]',
        "Looking at the page, row by row" + REASONING_CUTOFF,
    ))
    assert result.incomplete.reasoning_budget_hit == 1
    # Outside `total` and truthiness, which is what every "this page is
    # missing names" warning reads.
    assert not result.incomplete
    assert result.incomplete.total == 0
    (trace,) = result.reasoning
    assert trace.budget_hit and trace.stage == "detection"


def test_the_cut_off_is_recognised_when_the_server_trims_its_newlines():
    from pii.core.vlm import REASONING_CUTOFF

    result = read_response(_thinking_reply("[]", "thinking " + REASONING_CUTOFF.strip()))
    assert result.incomplete.reasoning_budget_hit == 1


def test_a_trace_under_the_budget_is_kept_and_not_counted():
    result = read_response(_thinking_reply("[]", "  Nothing private here.\n"))
    assert result.incomplete == Incomplete()
    (trace,) = result.reasoning
    assert trace.text == "Nothing private here." and not trace.budget_hit


def test_a_reply_that_did_not_think_carries_no_trace():
    assert read_response(_reply("[]")).reasoning == ()
    # Thinking off: both templates write an empty, pre-closed trace.
    assert read_response(_reply("<think>\n\n</think>\n\n[]")).reasoning == ()


def test_an_inline_trace_is_read_when_the_server_does_not_split_it():
    from pii.core.vlm import REASONING_CUTOFF

    result = read_response(_reply(
        "<|channel>thought\nrows [1] and [2]" + REASONING_CUTOFF + "<channel|>"
        '[{"text": "A", "type": "NAME"}]'
    ))
    (trace,) = result.reasoning
    assert trace.text.startswith("rows [1] and [2]") and trace.budget_hit
    assert [f.text for f in result.findings] == ["A"]


def test_a_truncated_answer_after_a_cut_off_trace_counts_both():
    from pii.core.vlm import REASONING_CUTOFF

    result = read_response(_thinking_reply(
        '[{"text": "A", "type": "NAME"}, {"te', REASONING_CUTOFF,
        finish_reason="length",
    ))
    assert result.incomplete == Incomplete(truncated=1, reasoning_budget_hit=1)


def test_budget_hits_add_up_across_pages():
    pages = [Incomplete(reasoning_budget_hit=1), Incomplete(truncated=1)]
    assert sum(pages) == Incomplete(truncated=1, reasoning_budget_hit=1)


def test_each_pass_labels_its_own_trace():
    calls = []

    def transport(url, payload, timeout):
        calls.append(payload)
        return _thinking_reply(
            '[{"text": "A", "type": "NAME", "bbox_2d": [1, 2, 3, 4]}]',
            f"thinking, call {len(calls)}",
        )

    class Img:
        def save(self, buf, fmt):
            buf.write(b"png")

    detector = VlmDetector(
        transport=transport, served_model=_qwen, grounding_reasoning_effort="same"
    )
    detected = detector.detect(Img())
    located = detector.localize(Img(), detected.findings)
    assert [(t.stage, t.text) for t in detected.reasoning] == [
        ("detection", "thinking, call 1")
    ]
    assert [(t.stage, t.text) for t in located.reasoning] == [
        ("grounding", "thinking, call 2")
    ]


@pytest.mark.parametrize("budget", [0, -1, 1.5, True])
def test_a_budget_below_one_token_is_refused(budget):
    # -1 is llama.cpp's "unlimited", which would take away the answer's own
    # room on top of the budget.
    from pii.core.text_llm import TextDetector

    with pytest.raises(ValueError, match="reasoning budget"):
        VlmDetector(reasoning_budget=budget)
    with pytest.raises(ValueError, match="reasoning budget"):
        TextDetector(reasoning_budget=budget)


# ----------------------------------------------------------------- grammar
#
# The output shape is enforced at the sampler rather than parsed out of
# whatever comes back. It constrains FORM, not LENGTH — the truncation tests
# above stay relevant with it on.


def _rule(grammar: str, name: str) -> str:
    return [
        line for line in grammar.splitlines()
        if line.startswith(f"{name} ::=")
    ][0]


def test_the_class_enum_is_exactly_the_mapped_vocabulary():
    # Derived from TYPE_MAP, so a class the model could name and TYPE_MAP does
    # not know is unrepresentable rather than silently IDENTIFIER_GENERIC.
    quoted = _rule(GRAMMAR_VALUES, "type").split("::=", 1)[1]
    assert [alt.strip() for alt in quoted.split("|")] == [
        f'"\\"{name}\\""' for name in TYPE_MAP
    ]


def test_each_prompt_shape_gets_the_matching_grammar():
    values = _transport("[]")
    VlmDetector(transport=values, served_model=_qwen).detect(Image.new("RGB", (4, 4), "white"))
    assert values.seen["payload"]["grammar"] == GRAMMAR_VALUES

    boxes = _transport("[]")
    VlmDetector(transport=boxes, want_boxes=True, box_order="xyxy", served_model=_qwen).detect(
        Image.new("RGB", (4, 4), "white")
    )
    assert boxes.seen["payload"]["grammar"] == GRAMMAR_VALUES_BOXES

    locate = _transport('[{"text": "A", "bbox_2d": [1, 2, 3, 4]}]')
    VlmDetector(transport=locate, box_order="xyxy", served_model=_qwen).localize(
        Image.new("RGB", (4, 4), "white"),
        [VlmFinding(text="A", entity_type="PERSON")],
    )
    assert locate.seen["payload"]["grammar"] == GRAMMAR_LOCATE


def test_only_the_boxes_grammar_admits_a_bbox():
    assert "bbox_2d" not in GRAMMAR_VALUES
    assert "bbox_2d" in GRAMMAR_VALUES_BOXES
    assert "bbox_2d" in GRAMMAR_LOCATE
    # Pass 2 is told the values, so it must not re-type them.
    assert "type" not in GRAMMAR_LOCATE


def test_the_grammar_field_is_absent_when_switched_off():
    # Not empty — absent, so the A/B is exactly grammar on vs off.
    send = _transport("[]")
    VlmDetector(transport=send, grammar=False, served_model=_qwen).detect(
        Image.new("RGB", (4, 4), "white")
    )
    assert "grammar" not in send.seen["payload"]


def test_the_only_unbounded_repetitions_carry_content():
    # An unbounded repetition that emits nothing meaningful — free whitespace,
    # an open-ended digit run — is a legal place for a greedy decode to spin
    # forever. The two that ARE unbounded have to be: the entry list is the
    # answer's length, and the value is transcribed verbatim.
    for grammar in (GRAMMAR_VALUES, GRAMMAR_VALUES_BOXES, GRAMMAR_LOCATE):
        repeated = [
            line for line in grammar.splitlines()
            if "*" in line or "+" in line
        ]
        assert [line.split(" ::=")[0] for line in repeated] == [
            "root", "string"
        ], grammar
        # Whitespace is pinned into the literals rather than given a rule.
        assert "ws" not in grammar


def test_a_bbox_integer_cannot_run_away_and_is_not_clamped():
    # Bounded so a digit run cannot spin; NOT range-checked to 0..1000, because
    # clamping turns a visibly off-page box into a plausible wrong one.
    rule = _rule(GRAMMAR_VALUES_BOXES, "int")
    assert rule.count("[0-9]?") == 4  # at most five digits
    assert "1000" not in rule


def test_grammar_writes_a_backslash_as_a_hex_escape():
    # llama.cpp b10326 rejects `\\` inside a character class ("failed to parse
    # grammar") but accepts \x5C. Do not restore json.gbnf's spelling.
    assert "\\x5C" in GRAMMAR_VALUES
    assert "[^\"\\x5C" in _rule(GRAMMAR_VALUES, "char")


# --------------------------------------------------------------- transport


def test_detector_sends_image_and_prompt():
    send = _transport('[{"text": "A", "type": "NAME"}]')
    det = VlmDetector(url="http://x:1", transport=send, served_model=_qwen)
    found = det.detect(Image.new("RGB", (8, 8), "white")).findings

    assert [f.text for f in found] == ["A"]
    content = send.seen["payload"]["messages"][0]["content"]
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "* IDENTIFIER :" in content[1]["text"]
    # Determinism is a gate requirement, not a preference.
    assert send.seen["payload"]["temperature"] == 0.0
    assert send.seen["payload"]["top_k"] == 1


def test_boxes_are_only_requested_when_they_will_be_used():
    # Asking for coordinates measurably costs recall, so the OCR-geometry
    # path must not pay for boxes it will throw away.
    assert "bbox_2d" not in VlmDetector(want_boxes=False).prompt
    assert "bbox_2d" in VlmDetector(want_boxes=True, box_order="xyxy").prompt


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
    det = VlmDetector(transport=send, box_order="xyxy")
    findings = [VlmFinding(text="A. Person", entity_type="PERSON")]

    (out,) = det.localize(
        Image.new("RGB", (8, 8), "white"), findings
    ).findings

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

    assert VlmDetector(transport=explode).localize(None, []).findings == []


# ------------------------------------------------------------ box order
#
# Each model is ASKED for boxes in its own order and read back in that order.
# Asked against its native order a model does not reliably comply: Gemma 4, told
# x first, answered y first on 27 of 31 real pages, x first on three and a mix on
# one (2026-09-13). A box read the wrong way round is still a plausible rectangle, so
# nothing fails — which is why the order is chosen per model and never assumed.

_WHITE = Image.new("RGB", (8, 8), "white")
_LOCATED = '[{"text": "A. Person", "bbox_2d": [20, 10, 40, 30]}]'
_PERSON = [VlmFinding(text="A. Person", entity_type="PERSON")]


def _explode(*a, **kw):  # pragma: no cover - must not run
    raise AssertionError("the server must not be asked what it serves")


def _located_by(model: str, **kwargs):
    send = _transport(_LOCATED, model)
    kwargs.setdefault("served_model", lambda url, timeout: model)
    det = VlmDetector(transport=send, **kwargs)
    (out,) = det.localize(_WHITE, _PERSON).findings
    prompt = send.seen["payload"]["messages"][0]["content"][1]["text"]
    return out, prompt


def test_the_x_first_prompts_are_sent_unchanged():
    # The measured wording is what an x-first model is sent, byte for byte.
    from pii.core import vlm

    for prompt in (vlm._OUTPUT_BOXES, vlm._LOCATE_PROMPT):
        assert vlm.in_box_order(prompt, "xyxy") is prompt


def test_the_y_first_prompts_swap_every_coordinate_name_and_nothing_else():
    from pii.core import vlm

    for prompt in (vlm._OUTPUT_BOXES, vlm._LOCATE_PROMPT):
        y_first = vlm.in_box_order(prompt, "yxyx")
        # Every phrase is present to swap — else y-first silently asks x first.
        for x_phrase, y_phrase in vlm._XY_PHRASES:
            assert x_phrase in prompt and y_phrase in y_first
        assert "x1, y1" not in y_first and "(x1,y1)" not in y_first
        back = y_first
        for x_phrase, y_phrase in vlm._XY_PHRASES:
            back = back.replace(y_phrase, x_phrase)
        assert back == prompt


def test_an_unresolved_order_never_reaches_a_prompt():
    from pii.core import vlm

    with pytest.raises(ValueError):
        vlm.in_box_order(vlm._LOCATE_PROMPT, "auto")


def test_a_y_first_box_leaves_the_parser_x_first():
    (found,) = parse_findings(
        '[{"text": "a", "type": "NAME", "bbox_2d": [20, 10, 40, 30]}]',
        box_order="yxyx",
    )
    assert found.box == (10, 20, 30, 40)


def test_a_y_first_box_is_normalized_after_the_swap():
    # Swap first, then order the corners — not the other way round.
    (found,) = parse_findings(
        '[{"text": "a", "type": "NAME", "bbox_2d": [40, 30, 20, 10]}]',
        box_order="yxyx",
    )
    assert found.box == (10, 20, 30, 40)


def test_auto_asks_gemma_y_first_and_reads_it_y_first():
    out, prompt = _located_by(GEMMA)
    assert "[y1, x1, y2, x2]" in prompt and "[x1, y1, x2, y2]" not in prompt
    assert out.box == (10, 20, 30, 40)


def test_auto_asks_qwen_x_first_and_reads_it_x_first():
    out, prompt = _located_by(QWEN)
    assert "[x1, y1, x2, y2]" in prompt
    assert out.box == (20, 10, 40, 30)


def test_an_explicit_order_is_what_is_asked_and_read_whatever_the_model():
    out, prompt = _located_by(GEMMA, box_order="xyxy", served_model=_explode)
    assert "[x1, y1, x2, y2]" in prompt
    assert out.box == (20, 10, 40, 30)


def test_hybrid_learns_the_model_from_pass_one_without_asking_the_server():
    # Pass 1 carries no boxes, but its reply names the model: pass 2's prompt is
    # chosen from that, at no extra request.
    detect = _transport('[{"text": "A. Person", "type": "NAME"}]', GEMMA)
    # Thinking off on pass 1: a thinking request needs the family BEFORE it goes
    # out (see test_thinking_asks_the_server_before_pass_one).
    det = VlmDetector(transport=detect, served_model=_explode, reasoning_effort="off")
    findings = det.detect(_WHITE).findings
    det.transport = locate = _transport(_LOCATED, GEMMA)
    (out,) = det.localize(_WHITE, findings).findings
    assert "[y1, x1, y2, x2]" in locate.seen["payload"]["messages"][0]["content"][1]["text"]
    assert out.box == (10, 20, 30, 40)


def test_a_boxed_first_request_asks_the_server_once():
    asked = []

    def served(url, timeout):
        asked.append(url)
        return GEMMA

    send = _transport(_LOCATED, GEMMA)
    det = VlmDetector(url="http://mac:8080", transport=send, served_model=served)
    det.localize(_WHITE, _PERSON)
    det.localize(_WHITE, _PERSON)
    assert asked == ["http://mac:8080"]


def test_an_unplaceable_model_is_refused_before_a_boxed_request_is_sent():
    det = VlmDetector(
        transport=_explode, served_model=lambda url, timeout: "/models/mystery-7b.gguf"
    )
    with pytest.raises(ModelFamilyUnknown) as caught:
        det.localize(_WHITE, _PERSON)
    assert "mystery-7b" in str(caught.value)
    assert "--box-order" in str(caught.value)
    # A VlmError, so every front-end reports it as a message, not a traceback.
    assert isinstance(caught.value, VlmError)


def test_an_unplaceable_model_named_by_pass_one_is_refused_without_asking_again():
    detect = _transport('[{"text": "A", "type": "NAME"}]', "/models/mystery.gguf")
    det = VlmDetector(transport=detect, served_model=_explode, reasoning_effort="off")
    # A boxless pass runs against any model: there is nothing to misread.
    (found,) = det.detect(_WHITE).findings
    assert found.text == "A"
    with pytest.raises(ModelFamilyUnknown) as caught:
        det.localize(_WHITE, [found])
    assert "mystery.gguf" in str(caught.value)


def test_a_reply_from_a_different_model_than_the_prompt_was_chosen_for_is_refused():
    # The server changed model between choosing the prompt and answering it.
    send = _transport(_LOCATED, QWEN)
    det = VlmDetector(transport=send, served_model=lambda url, timeout: GEMMA)
    with pytest.raises(ModelFamilyUnknown) as caught:
        det.localize(_WHITE, _PERSON)
    assert "gemma family" in str(caught.value) and "Qwen3.8" in str(caught.value)


def test_the_chosen_order_is_announced_once_per_model(capsys):
    send = _transport(_LOCATED, GEMMA)
    det = VlmDetector(transport=send, served_model=lambda url, timeout: GEMMA)
    det.localize(_WHITE, _PERSON)
    det.localize(_WHITE, _PERSON)
    err = capsys.readouterr().err
    assert err.count("-> gemma family") == 1
    # The file name, not the server's full path.
    assert "gemma-4-26B-A4B-it-Q8_0.gguf" in err and "/Users/" not in err


def test_served_model_name_reads_the_openai_listing():
    from pii.core.vlm import served_model_name

    body = json.dumps({"data": [{"id": GEMMA}], "models": [{"name": "other"}]})
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = body.encode()
    with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
        assert served_model_name("http://mac:8080", 5) == GEMMA
    assert urlopen.call_args[0][0] == "http://mac:8080/v1/models"


def test_served_model_name_failure_is_actionable():
    import urllib.error

    from pii.core.vlm import VlmUnavailable, served_model_name

    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        with pytest.raises(VlmUnavailable) as caught:
            served_model_name("http://127.0.0.1:9", 5)
    assert "http://127.0.0.1:9" in str(caught.value) and "--vlm-url" in str(caught.value)


@pytest.mark.parametrize(
    "name, order",
    [
        (GEMMA, "yxyx"),
        (QWEN, "xyxy"),
        ("Qwen3-VL-8B-Instruct-Q8_0.gguf", "xyxy"),
        (r"C:\models\GEMMA-4-31B-it-Q4_0.gguf", "yxyx"),
        # The directory must not decide it.
        ("/models/qwen-vs-gemma/mystery.gguf", None),
        # A name claiming both families is evidence for neither.
        ("qwen-distilled-gemma.gguf", None),
        ("", None),
        (None, None),
    ],
)
def test_family_for_model(name, order):
    family = family_for_model(name)
    assert (family.box_order if family else None) == order


def test_an_unresolved_order_never_reaches_the_parser():
    with pytest.raises(ValueError):
        parse_findings("[]", box_order="auto")


def test_an_unknown_box_order_is_refused_up_front():
    with pytest.raises(ValueError):
        VlmDetector(box_order="yx")


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


def test_a_surplus_box_for_a_named_value_becomes_its_own_finding():
    # Pass 1 names each value once; pass 2 boxes every printing. Each extra box
    # is a printing the box-free document-wide search may not reach.
    findings = [
        VlmFinding(text="LOGO CO", entity_type="ORGANIZATION"),
        VlmFinding(text="JANE CITIZEN", entity_type="PERSON"),
    ]
    hints = [
        VlmFinding(text="LOGO CO", entity_type="PERSON", box=(1, 1, 2, 2)),
        VlmFinding(text="logo co", entity_type="PERSON", box=(5, 5, 6, 6)),
        VlmFinding(text="JANE CITIZEN", entity_type="PERSON", box=(3, 3, 4, 4)),
        VlmFinding(text="not asked for", entity_type="PERSON", box=(9, 9, 9, 9)),
    ]
    out = attach_boxes(findings, hints)
    # Right after the value's own finding, carrying pass 1's text and class.
    assert [(f.text, f.entity_type, f.box) for f in out] == [
        ("LOGO CO", "ORGANIZATION", (1, 1, 2, 2)),
        ("LOGO CO", "ORGANIZATION", (5, 5, 6, 6)),
        ("JANE CITIZEN", "PERSON", (3, 3, 4, 4)),
    ]


def test_surplus_boxes_follow_the_last_finding_of_a_value_named_twice():
    findings = [
        VlmFinding(text="A", entity_type="PERSON"),
        VlmFinding(text="B", entity_type="PERSON"),
        VlmFinding(text="A", entity_type="PERSON"),
    ]
    hints = [VlmFinding(text="A", entity_type="PERSON", box=(i, i, i, i)) for i in range(1, 4)]
    assert [(f.text, f.box) for f in attach_boxes(findings, hints)] == [
        ("A", (1, 1, 1, 1)), ("B", None), ("A", (2, 2, 2, 2)), ("A", (3, 3, 3, 3)),
    ]


def test_the_values_prompt_asks_for_each_value_once_and_the_box_prompt_for_every_printing():
    # Without boxes, every printing is found after pass 1 (pass 2 and the
    # document-wide search); with them, pass 1 is the only source of boxes.
    from pii.core.vlm import PROMPT, _OUTPUT_BOXES, _OUTPUT_VALUES

    assert "each distinct value once" in _OUTPUT_VALUES
    assert "every place a value is printed" in _OUTPUT_BOXES
    assert "occurrences" not in PROMPT


def test_the_detection_prompt_leaves_the_model_no_keep_decision():
    """Over-strip is recoverable, under-strip is a breach: an unsure value is
    included, organizations and their web addresses too, and the keep list
    filters later. A label is evidence, not part of the value."""
    from pii.core.vlm import PROMPT

    assert "When unsure whether to include something, include it" in PROMPT
    # The word carries the model's own, narrower idea of personal information.
    assert "PII" not in PROMPT
    assert "web addresses" in PROMPT
    assert "never the label" in PROMPT
    lowered = PROMPT.lower()
    assert not any(bank in lowered for bank in ("anz", "westpac", "nab "))


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


def _ocr_page(text: str):
    """One word per token, laid out left to right on a single line — what an
    OCR engine returns, before linearization."""
    from pii.core.ocr_page import OcrFrame, build_page

    row, x = [], 0
    for token in text.split(" "):
        row.append((token, Box(x, 0, 10 * len(token), 12), 99.0))
        x += 10 * len(token) + 10
    return build_page([row], OcrFrame(width=x, height=12, page=1))


def _ocr(text: str):
    """The same page through the real perception -> linearization seam."""
    from pii.core.linearization import linearize

    return linearize(_ocr_page(text))


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


def test_a_truncated_read_warns_and_reaches_the_result(pipeline):
    # The leak this closes: layer 0 is the only detector for PERSON / ADDRESS /
    # ORGANIZATION, so a page whose answer was cut off gets none of them while
    # layer 1 still redacts the checksummed identifiers — plausible-looking
    # output over a page nobody finished reading.
    from pii.core.image_mode import read_page, strip_from_vlm

    class CutOff:
        def detect(self, image):
            return DetectorResult(
                [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")],
                Incomplete(truncated=1),
            )

        def localize(self, image, findings):
            return DetectorResult(list(findings))

    with pytest.warns(RuntimeWarning, match="cut off at the token budget"):
        read = read_page(
            Image.new("RGB", (200, 40), "white"),
            lambda im: _ocr_page("SERGEI KULIK"),
            detector=CutOff(),
            pipeline=pipeline,
            geometry="hybrid",
        )
    assert read.incomplete == Incomplete(truncated=1)
    # Carried to the caller, not only warned about: the default warning filter
    # shows one instance per location, so page 2 of the same run is silent.
    result = strip_from_vlm(
        Image.new("RGB", (200, 40), "white"), read.findings, pipeline,
        PseudonymMap(), ocr=_ocr("SERGEI KULIK"),
        incomplete=read.incomplete,
    )
    assert result.incomplete == Incomplete(truncated=1)
    # What DID arrive is still stripped — salvage is not quarantine.
    assert result.spans


def test_a_malformed_read_is_reported_as_its_own_kind(pipeline):
    from pii.core.image_mode import read_page

    class Garbage:
        def detect(self, image):
            return DetectorResult([], Incomplete(malformed=1))

        def localize(self, image, findings):  # pragma: no cover - no findings
            return DetectorResult(list(findings))

    with pytest.warns(RuntimeWarning, match="no usable JSON array"):
        read = read_page(
            Image.new("RGB", (200, 40), "white"),
            lambda im: _ocr_page("nothing here"),
            detector=Garbage(),
            pipeline=pipeline,
            geometry="hybrid",
        )
    assert read.incomplete == Incomplete(malformed=1)


def test_a_truncated_second_pass_is_counted_too(pipeline):
    # Milder — the values are known and only lose their search constraint —
    # but nothing downstream can tell a box the model declined to give from
    # one it never got to.
    from pii.core.image_mode import read_page

    class CutOffBoxes:
        def detect(self, image):
            return DetectorResult(
                [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")]
            )

        def localize(self, image, findings):
            return DetectorResult(list(findings), Incomplete(truncated=1))

    with pytest.warns(RuntimeWarning, match="cut off"):
        read = read_page(
            Image.new("RGB", (200, 40), "white"),
            lambda im: _ocr_page("SERGEI KULIK"),
            detector=CutOffBoxes(),
            pipeline=pipeline,
            geometry="hybrid",
        )
    assert read.incomplete == Incomplete(truncated=1)


def test_a_clean_read_carries_no_incomplete_count(pipeline):
    from pii.core.image_mode import strip_rendered_page

    class Fine:
        def detect(self, image):
            return DetectorResult(
                [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")]
            )

        def localize(self, image, findings):
            return DetectorResult(list(findings))

    result = strip_rendered_page(
        Image.new("RGB", (200, 40), "white"),
        pipeline,
        PseudonymMap(),
        ocr_engine=lambda im: _ocr_page("SERGEI KULIK"),
        detector=Fine(),
    )
    assert not result.incomplete


def test_both_passes_traces_reach_the_page_result(pipeline):
    # For the debug output, which is written after the page is redacted.
    from pii.core.image_mode import strip_rendered_page
    from pii.core.vlm import ReasoningTrace

    detect = ReasoningTrace("detection", "which values", budget_hit=True)
    ground = ReasoningTrace("grounding", "where they are")

    class Thinking:
        def detect(self, image):
            return DetectorResult(
                [VlmFinding(text="SERGEI KULIK", entity_type="PERSON")],
                Incomplete(reasoning_budget_hit=1), (detect,),
            )

        def localize(self, image, findings):
            return DetectorResult(list(findings), reasoning=(ground,))

    result = strip_rendered_page(
        Image.new("RGB", (200, 40), "white"),
        pipeline,
        PseudonymMap(),
        ocr_engine=lambda im: _ocr_page("SERGEI KULIK"),
        detector=Thinking(),
    )
    assert result.reasoning == (detect, ground)
    assert result.incomplete.reasoning_budget_hit == 1


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
            return DetectorResult([VlmFinding("SERGEI KULIK", "PERSON")])

        def localize(self, image, findings):
            calls.append("localize")
            return DetectorResult(
                [VlmFinding("SERGEI KULIK", "PERSON", box=(0, 0, 300, 100))]
            )

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
    # BOTH occurrences strip. The box still decides which one the model's own
    # finding claims — that is what this test is about — but since 2026-08-11
    # the document-wide pass covers the repeat the model never mentioned,
    # which used to be painted on neither page nor position.
    assert [
        result.ocr.text[s.start : s.end] for s in result.spans
    ] == ["SERGEI KULIK", "SERGEI KULIK"]
    assert result.spans[0].start == 0  # the occurrence the box pointed at
    (repeat,) = result.borrowed
    assert repeat.start == result.spans[1].start


def test_ocr_geometry_skips_the_second_pass(pipeline):
    # The pre-box baseline stays reachable, and must not pay for a pass whose
    # boxes it would ignore.
    from pii.core.image_mode import strip_rendered_page
    from pii.core.ocr_page import OcrFrame, build_page

    calls = []

    class FakeDetector:
        def detect(self, image):
            calls.append("detect")
            return DetectorResult([])

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
            return DetectorResult([])

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
# Layer 0 is stubbed to emit nothing, so what these assert is layer 1 alone.

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


def test_a_truncated_private_entity_is_stripped(pipeline):
    """The 2026-08-11 leak, end to end.

    A statement's fixed-width narrative printed 'SK BUSINESS TRUST' as
    'SK BUSINESS TRUS' three times on one page. The value was detected every
    time — and then discarded, because the old policy stripped an organization
    only on a legal-form marker and the page had truncated exactly that. Under
    the keep list an unrecognized name has no way to be kept."""
    result, _ = _strip(
        [VlmFinding(text="SK BUSINESS TRUS", entity_type="ORGANIZATION")],
        "FROM SK BUSINESS TRUS HIGHETT LOAN", pipeline,
    )
    assert [r.entity_type for r in result.spans] == ["ORGANIZATION"]
    assert result.skipped == []


def test_a_kept_merchant_is_reported_as_skipped(pipeline):
    """The other side of the same decision: a keep-listed merchant is NOT
    painted, and says so on the result so a debug overlay can draw it. Silence
    here is what made the leak above invisible."""
    result, _ = _strip(
        [VlmFinding(text="WOOLWORTHS", entity_type="ORGANIZATION")],
        "paid WOOLWORTHS today", pipeline,
    )
    assert result.spans == []
    assert [d.entity_type for d in result.skipped] == ["ORGANIZATION"]


def test_strip_orgs_still_reaches_model_findings(make_pipeline):
    # ...and the operator override still works through the same filter:
    # --strip-orgs drops the keep list's ORGANIZATION section.
    from pii.core.entity_keep import load_keep

    p = make_pipeline(entity_keep=load_keep().without("ORGANIZATION"))
    result, _ = _strip(
        [VlmFinding(text="WOOLWORTHS", entity_type="ORGANIZATION")],
        "paid WOOLWORTHS today", p,
    )
    assert [r.entity_type for r in result.spans] == ["ORGANIZATION"]


# ------------------------------------------------- layer 0 turned off

def test_null_detector_finds_nothing_and_asks_nobody():
    """--layer0 off is a detector that answers nothing, not a missing
    argument: the strip entry points still require one, so the patterns-only
    regime stays unreachable by omission."""
    from pii.core.vlm import NullDetector

    detector = NullDetector()
    result = detector.detect(object())
    assert result.findings == []
    assert detector.layer0 == "off"


def test_null_detector_reports_nothing_incomplete():
    """Nothing was asked, so nothing was cut off. A page whose answer was LOST
    is a different fact and must not report the same way — an operator reading
    `incomplete` is asking what went missing, not what was never requested."""
    from pii.core.vlm import NullDetector

    assert not NullDetector().detect(object()).incomplete


def test_null_detector_localizes_without_a_server():
    """Pass 2 runs unconditionally under the default hybrid geometry, so it
    must be reachable with no transport at all."""
    from pii.core.vlm import NullDetector

    assert NullDetector().localize(object(), []).findings == []


def test_the_detectors_name_their_own_modality():
    """The run describes itself from the detector rather than from a flag the
    front-end has to remember — a plain string, so the vision/text switches
    planned in core/TODO.md extend it without touching its readers."""
    from pii.core.text_llm import TextDetector
    from pii.core.vlm import NullDetector, VlmDetector

    assert VlmDetector.layer0 == "vision"
    assert TextDetector.layer0 == "text"
    assert NullDetector.layer0 == "off"


# --- thinking: the reasoning payload, and the reply shapes it produces -------
# Layer 0 became a reasoning model 2026-08-19. These cover the two halves that
# can fail silently: the request (a dropped field means the model does not
# think, and the reply still parses) and the reply (an unstripped trace makes
# a page with findings look CLEAN).


def _payload(**kwargs) -> dict:
    """The payload one detect() call puts on the wire."""
    sent = []

    def transport(url, payload, timeout):
        sent.append(payload)
        return {"choices": [{"message": {"content": "[]"},
                             "finish_reason": "stop"}]}

    class Img:
        def save(self, buf, fmt):
            buf.write(b"png")

    kwargs.setdefault("served_model", _qwen)
    VlmDetector(transport=transport, **kwargs).detect(Img())
    return sent[0]


def test_thinking_is_on_by_default_with_a_budget_and_a_cut_off():
    from pii.core.vlm import REASONING_CUTOFF

    payload = _payload()
    assert payload["chat_template_kwargs"] == {"reasoning_effort": "medium"}
    assert payload["reasoning_budget_tokens"] == 4096
    assert payload["reasoning_budget_message"] == REASONING_CUTOFF


def test_the_grammar_is_lazy_so_it_never_constrains_the_thinking():
    from pii.core.vlm import QWEN

    payload = _payload()
    assert payload["grammar_lazy"] is True
    # An int on the wire: llama.cpp reads `.at("type").get<int>()`, so a string
    # is an HTTP 400 rather than a fallback.
    assert payload["grammar_triggers"] == [{"type": 2, "value": QWEN.trigger}]


def test_the_trigger_captures_the_bracket_and_not_the_think_tag():
    # llama.cpp replays into the grammar from the first non-empty capture
    # group, so the group must contain "[" alone: a trigger that fed "</think>"
    # to a grammar whose root starts with "[" would reject every continuation.
    import re

    from pii.core.vlm import GEMMA, QWEN

    qwen = re.search(QWEN.trigger, "thinking about [things]</think>\n\n[{}]")
    assert qwen.group(1) == "[" and qwen.start(1) > qwen.string.index("</think>")
    gemma = re.search(GEMMA.trigger, "<|channel>thought\nabout [x]<channel|>[{}]")
    assert gemma.group(1) == "[" and gemma.start(1) > gemma.string.index("<channel|>")


def test_no_lazy_grammar_when_thinking_is_off():
    # "off" must reproduce the pre-2026-08-19 request exactly, or it is not a
    # baseline. The template pre-closes the think block, so the grammar
    # applying from token 0 is correct there.
    payload = _payload(reasoning_effort="off")
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert "grammar_lazy" not in payload
    assert "reasoning_budget_tokens" not in payload


def test_max_tokens_leaves_the_answer_room_on_top_of_the_budget():
    # The reasoning budget must be able to bite BEFORE max_tokens does:
    # exhausting the budget closes the trace and still answers, while
    # exhausting max_tokens truncates the array mid-entry.
    assert _payload()["max_tokens"] == 4096 + 4096
    assert _payload(reasoning_budget=1024)["max_tokens"] == 4096 + 1024
    assert _payload(reasoning_effort="off")["max_tokens"] == 4096


def test_an_unknown_reasoning_effort_is_rejected():
    with pytest.raises(ValueError, match="unknown reasoning effort"):
        VlmDetector(reasoning_effort="ludicrous")


def test_forced_open_thinking_is_stripped_from_a_body():
    # The failure this exists for: a template that opens <think> ITSELF means
    # the reply carries only the CLOSING tag, so the old matched-pair regex
    # stripped nothing, the JSON scanner latched onto a "[" inside the
    # reasoning, and parse_findings returned [] -- a CLEAN PAGE.
    raw = ('Reading the page. I see [account] numbers.</think>\n\n'
           '[{"text": "SERGEI KULIK", "type": "NAME"}]')
    assert [f.text for f in parse_findings(raw)] == ["SERGEI KULIK"]


def test_a_matched_think_pair_is_still_stripped():
    raw = ('<think>reasoning with [brackets]</think>\n'
           '[{"text": "SERGEI KULIK", "type": "NAME"}]')
    assert [f.text for f in parse_findings(raw)] == ["SERGEI KULIK"]


def test_a_reply_with_no_thinking_at_all_is_untouched():
    raw = '[{"text": "SERGEI KULIK", "type": "NAME"}]'
    assert [f.text for f in parse_findings(raw)] == ["SERGEI KULIK"]


# --- thinking per model family, and per pass --------------------------------
# How thinking is switched on and where its trace ends is the model FAMILY's
# (2026-09-13): a Qwen request sent to Gemma never thinks and its grammar never
# engages, and the reply still parses. So the family is resolved before a
# thinking request, and the grounding pass has its own effort, off by default.


def _gemma(url, timeout):
    return GEMMA


def _payloads(calls: int = 2, **kwargs) -> list[dict]:
    """The payloads of one detect() and one localize() call, in order."""
    sent = []
    reply_model = kwargs.pop("_reply_model", QWEN)

    def transport(url, payload, timeout):
        sent.append(payload)
        body = '[{"text": "A", "type": "NAME"}]' if len(sent) == 1 else '[]'
        return _reply(body, model=reply_model)

    kwargs.setdefault("served_model", _qwen)
    det = VlmDetector(transport=transport, box_order="xyxy", **kwargs)
    found = det.detect(_WHITE).findings
    if calls > 1:
        det.localize(_WHITE, found)
    return sent


@pytest.mark.parametrize("served, name, cached", [(_gemma, GEMMA, False), (_qwen, QWEN, True)])
def test_the_prompt_cache_follows_the_model_family(served, name, cached):
    """A cached prompt re-evaluates differently, so greedy output reproduces only
    with the cache off (2026-09-15); Qwen trades that for restoring its post-image
    checkpoint on pass 2. Both passes."""
    for payload in _payloads(served_model=served, _reply_model=name):
        assert payload["cache_prompt"] is cached


def test_the_prompt_cache_is_off_while_the_family_is_unknown():
    # Thinking off with a box order given needs no family, so none is resolved -
    # and such a request must still reproduce against any model.
    unknown = lambda url, timeout: "/models/mystery-7B.gguf"  # noqa: E731
    for payload in _payloads(reasoning_effort="off", served_model=unknown, _reply_model=None):
        assert payload["cache_prompt"] is False


def test_gemma_thinking_sends_its_switch_budget_and_trigger():
    from pii.core.vlm import GEMMA as GEMMA_FAMILY, REASONING_CUTOFF

    detect, _ = _payloads(served_model=_gemma, _reply_model=GEMMA)
    assert detect["chat_template_kwargs"] == {"enable_thinking": True}
    assert detect["reasoning_budget_tokens"] == 4096
    assert detect["reasoning_budget_message"] == REASONING_CUTOFF
    assert detect["grammar_triggers"] == [{"type": 2, "value": GEMMA_FAMILY.trigger}]


def test_qwen_thinking_is_unchanged():
    from pii.core.vlm import QWEN as QWEN_FAMILY

    detect, _ = _payloads(reasoning_effort="xhigh")
    assert detect["chat_template_kwargs"] == {"reasoning_effort": "xhigh"}
    assert detect["grammar_triggers"] == [{"type": 2, "value": QWEN_FAMILY.trigger}]


def test_thinking_asks_the_server_once_before_pass_one():
    asked = []

    def served(url, timeout):
        asked.append(url)
        return QWEN

    sent = _payloads(served_model=served)
    # Once, for pass 1; pass 2 needs nothing more.
    assert len(asked) == 1
    assert len(sent) == 2


def test_thinking_off_needs_no_family():
    (detect,) = _payloads(calls=1, reasoning_effort="off", served_model=_explode)
    assert detect["chat_template_kwargs"] == {"enable_thinking": False}
    assert "grammar_lazy" not in detect


def test_an_unplaceable_model_is_refused_before_a_thinking_request():
    det = VlmDetector(
        transport=_explode, served_model=lambda url, timeout: "/models/mystery-7b.gguf"
    )
    with pytest.raises(ModelFamilyUnknown) as caught:
        det.detect(_WHITE)
    assert "mystery-7b" in str(caught.value)
    assert "--reasoning-effort off" in str(caught.value)


@pytest.mark.parametrize("effort", ["low", "xhigh"])
def test_gemma_refuses_an_effort_level_its_template_does_not_read(effort):
    from pii.core.vlm import ReasoningEffortUnsupported

    det = VlmDetector(transport=_explode, served_model=_gemma, reasoning_effort=effort)
    with pytest.raises(ReasoningEffortUnsupported) as caught:
        det.detect(_WHITE)
    assert "gemma" in str(caught.value) and effort in str(caught.value)
    assert isinstance(caught.value, VlmError)


def test_the_grounding_pass_does_not_think_by_default():
    detect, localize = _payloads()
    assert detect["chat_template_kwargs"] == {"reasoning_effort": "medium"}
    assert localize["chat_template_kwargs"] == {"enable_thinking": False}
    assert "reasoning_budget_tokens" not in localize and "grammar_lazy" not in localize
    assert localize["max_tokens"] == 4096


def test_grounding_same_copies_the_detection_effort():
    _, localize = _payloads(reasoning_effort="xhigh", grounding_reasoning_effort="same")
    assert localize["chat_template_kwargs"] == {"reasoning_effort": "xhigh"}
    assert localize["grammar_lazy"] is True


def test_grounding_can_think_while_detection_does_not():
    detect, localize = _payloads(reasoning_effort="off", grounding_reasoning_effort="medium")
    assert detect["chat_template_kwargs"] == {"enable_thinking": False}
    assert localize["chat_template_kwargs"] == {"reasoning_effort": "medium"}


def test_an_unknown_grounding_effort_is_rejected():
    with pytest.raises(ValueError, match="grounding reasoning effort"):
        VlmDetector(grounding_reasoning_effort="ludicrous")


def test_a_reply_from_another_family_than_a_thinking_request_is_refused():
    # Built for Qwen (its effort kwarg, its trigger); answered by Gemma, which
    # would have ignored both.
    det = VlmDetector(transport=_transport("[]", GEMMA), served_model=_qwen)
    with pytest.raises(ModelFamilyUnknown) as caught:
        det.detect(_WHITE)
    assert "qwen family" in str(caught.value)


def test_a_gemma_thought_channel_is_stripped_from_a_body():
    raw = ('<|channel>thought\nReading [account] numbers.<channel|>'
           '[{"text": "SERGEI KULIK", "type": "NAME"}]')
    assert [f.text for f in parse_findings(raw)] == ["SERGEI KULIK"]
    # The opening half can be missing from a body a caller cut, as with Qwen.
    raw = ('Reading [account] numbers.<channel|>'
           '[{"text": "SERGEI KULIK", "type": "NAME"}]')
    assert [f.text for f in parse_findings(raw)] == ["SERGEI KULIK"]


def test_combined_geometry_asks_one_pass_for_boxes_and_skips_localize(pipeline):
    # The whole point of `combined`: one model call, and its boxes constrain
    # the search exactly as hybrid's second pass does -- NOT painted, which is
    # what separates it from `vlm`.
    from pii.core.image_mode import strip_rendered_page
    from pii.core.ocr import Box
    from pii.core.ocr_page import OcrFrame, build_page

    calls = []

    class FakeDetector:
        def detect(self, image):
            calls.append("detect")
            return DetectorResult(
                [VlmFinding("SERGEI KULIK", "PERSON", box=(0, 0, 300, 100))]
            )

        def localize(self, image, findings):  # pragma: no cover - must not run
            calls.append("localize")
            raise AssertionError("combined geometry must not run a second pass")

    def fake_ocr(image):
        row = [
            ("SERGEI", Box(0, 0, 60, 12), 99.0),
            ("KULIK", Box(70, 0, 50, 12), 99.0),
        ]
        return build_page([row], OcrFrame(width=1000, height=120, page=1))

    result = strip_rendered_page(
        Image.new("RGB", (1000, 120), "white"),
        pipeline,
        PseudonymMap(),
        ocr_engine=fake_ocr,
        detector=FakeDetector(),
        geometry="combined",
    )
    assert calls == ["detect"]
    # OCR ran (unlike `vlm`) and the value was located in its text.
    assert result.ocr is not None
    assert [
        result.ocr.text[s.start : s.end] for s in result.spans
    ] == ["SERGEI KULIK"]


# --- transport retry: a dropped connection must not destroy a long run ------


def test_a_dropped_connection_is_retried_and_recovers(monkeypatch, capsys):
    # The 2026-08-19 failure: the server had already generated the reply and
    # the pipe reset on the way back. One blip used to kill a 56-minute run.
    import urllib.request

    from pii.core import vlm

    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)
    calls = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"choices": [{"message": {"content": "[]"}}]}'

    def flaky(req, timeout=None):
        calls.append(req)
        if len(calls) == 1:
            raise ConnectionResetError(10054, "forcibly closed")
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", flaky)
    assert vlm.http_transport("http://host:8080", {"a": 1}, 60)["choices"]
    assert len(calls) == 2
    assert "retrying" in capsys.readouterr().err


def test_an_http_status_is_never_retried(monkeypatch):
    # A status code is an ANSWER: the server read the request and rejected it.
    # Retrying would hide a bad request behind a delay. HTTPError subclasses
    # URLError, so this also guards the except-clause ORDER.
    import io
    import urllib.error
    import urllib.request

    from pii.core import vlm

    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)
    calls = []

    def refuse(req, timeout=None):
        calls.append(req)
        raise urllib.error.HTTPError(
            "http://host:8080", 400, "Bad Request", {}, io.BytesIO(b"nope")
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(vlm.VlmUnavailable, match="HTTP 400"):
        vlm.http_transport("http://host:8080", {"a": 1}, 60)
    assert len(calls) == 1


def test_a_server_that_is_down_gives_up_and_says_so(monkeypatch):
    import urllib.request

    from pii.core import vlm

    monkeypatch.setattr(vlm.time, "sleep", lambda _s: None)
    calls = []

    def dead(req, timeout=None):
        calls.append(req)
        raise ConnectionRefusedError("nobody home")

    monkeypatch.setattr(urllib.request, "urlopen", dead)
    with pytest.raises(vlm.VlmUnavailable, match="after 3 attempts"):
        vlm.http_transport("http://host:8080", {"a": 1}, 60)
    assert len(calls) == vlm.TRANSPORT_ATTEMPTS

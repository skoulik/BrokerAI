"""SpacyRecognizer emission policy (2026-07-14 image-demo wart debug).

With the NER layer on, GLiNER2 owns PERSON/ORG/dates; en_core_web_sm's
PERSON and DATE_TIME emissions added glue spans crossing OCR line breaks
('Emily Watson\\nAddress') and date-as-name false positives
('03/06/2026 Transfer'). SpacyRecognizer is therefore restricted to
LOCATION when use_ner=True — kept because bare city names in prose are
the only partial coverage of contextual identifiers ("a teacher in
Cairns") until the layer-3 LLM audit lands (tier-1 ablation: dropping
spacy entirely turns CONTEXTUAL_ID 3x partial into 3x leaked).
Patterns-only mode keeps the full recognizer: it is the only name
detector there, and its leaks are already documented.

The registry policy is tested with a stubbed GLiNER2 (no model load);
the marked test at the bottom checks the same nuances with the real one.
"""

import sys
import types

import pytest
from presidio_analyzer import EntityRecognizer

TRANSFER_LINE = "03/06/2026 Transfer from PayID emily.w87@gmail.com +$250.00"
HOLDER_LINES = (
    "Account holder: Emily Watson\n"
    "Address: Unit 3, 42 Wattle Street, Newtown NSW 2042"
)


class _NoopGliner2(EntityRecognizer):
    """Stands in for Gliner2Recognizer so the NER-on registry composition
    can be tested without loading the model."""

    def __init__(self):
        super().__init__(supported_entities=["PERSON"], name="Gliner2Recognizer")

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        return []


@pytest.fixture(scope="module")
def ner_on_stub_pipeline():
    from pii.pipeline import PiiPipeline

    stub = types.ModuleType("pii.gliner2_recognizer")
    stub.Gliner2Recognizer = _NoopGliner2
    saved = sys.modules.get("pii.gliner2_recognizer")
    sys.modules["pii.gliner2_recognizer"] = stub
    try:
        yield PiiPipeline(use_ner=True)
    finally:
        if saved is None:
            del sys.modules["pii.gliner2_recognizer"]
        else:
            sys.modules["pii.gliner2_recognizer"] = saved


def test_ner_on_spacy_emits_no_person(ner_on_stub_pipeline):
    results = ner_on_stub_pipeline.analyze(TRANSFER_LINE)
    assert not [r for r in results if r.entity_type == "PERSON"]
    # the line is still processed — the email is caught by patterns
    assert [r for r in results if r.entity_type == "EMAIL_ADDRESS"]


def test_ner_on_no_span_crosses_line_break(ner_on_stub_pipeline):
    results = ner_on_stub_pipeline.analyze(HOLDER_LINES)
    assert all("\n" not in HOLDER_LINES[r.start : r.end] for r in results)


def test_ner_on_spacy_location_retained(ner_on_stub_pipeline):
    results = ner_on_stub_pipeline.analyze("Her partner is a teacher in Cairns.")
    locations = [r for r in results if r.entity_type == "LOCATION"]
    assert ["Cairns"] == [
        "Her partner is a teacher in Cairns."[r.start : r.end] for r in locations
    ]


def test_patterns_only_keeps_full_spacy(pipeline):
    # In --no-ner mode spacy is the only name detector; the restriction
    # must not apply there.
    results = pipeline.analyze("Account holder: John Smith")
    assert [r.entity_type for r in results if r.entity_type == "PERSON"]


@pytest.mark.model
def test_real_ner_demo_statement_nuances(make_pipeline):
    pipe = make_pipeline(use_ner=True)
    text = HOLDER_LINES + "\n" + TRANSFER_LINE
    plan = pipe.plan(text)
    persons = [
        text[r.start : r.end] for r in plan if r.entity_type == "PERSON"
    ]
    assert persons == ["Emily Watson"]  # exact span, no cross-line glue
    assert all("\n" not in text[r.start : r.end] for r in plan)
    assert not any(  # the transaction date is not painted over
        r.start <= text.index(TRANSFER_LINE) < r.end for r in plan
    )

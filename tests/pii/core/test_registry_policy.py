"""Registry composition policy after the spaCy detector retirement (2026-07-15).

spaCy stays as Presidio's NLP engine (tokens/lemmas → context enhancer) but is
no longer a detector: SpacyRecognizer is gone from the registry and GLiNER2
owns PERSON/ORG/dates. en_core_web_sm's own PERSON/DATE_TIME emissions were
glue spans crossing OCR line breaks ('Emily Watson\\nAddress') and date-as-name
false positives.

Standalone LOCATION detection was retired 2026-07-23: a bare city/town name is
acceptable verbatim in financial documents, so GLiNER2's location pass and its
char floor were removed. The ADDRESS passes still own full addresses and
suburb-state-postcode lines.

Registry-composition tests use a stubbed GLiNER2 (no model load); the marked
tests check the nuances on the real stack.
"""

import pytest

TRANSFER_LINE = "03/06/2026 Transfer from PayID emily.w87@gmail.com +$250.00"
HOLDER_LINES = (
    "Account holder: Emily Watson\n"
    "Address: Unit 3, 42 Wattle Street, Newtown NSW 2042"
)


def _recognizer(pipeline, name):
    return next(
        (r for r in pipeline.analyzer.registry.recognizers if r.name == name),
        None,
    )


def test_spacy_recognizer_retired(pipeline):
    assert _recognizer(pipeline, "SpacyRecognizer") is None


def test_url_ip_recognizers_removed(pipeline):
    # URL/IP dropped 2026-07-23: not relevant to financial documents. The
    # predefined recognizers are removed from the registry (not merely
    # unstripped), so they never detect and never clutter analyze()/reports.
    assert _recognizer(pipeline, "UrlRecognizer") is None
    assert _recognizer(pipeline, "IpRecognizer") is None


def test_url_ip_not_in_default_strip():
    from pii.core import DEFAULT_STRIP_ENTITIES

    assert "URL" not in DEFAULT_STRIP_ENTITIES
    assert "IP_ADDRESS" not in DEFAULT_STRIP_ENTITIES


def test_gliner2_present_and_location_retired(pipeline):
    # Standalone LOCATION detection retired 2026-07-23: the recognizer is
    # present but no longer advertises LOCATION as a supported entity.
    gliner2 = _recognizer(pipeline, "Gliner2Recognizer")
    assert gliner2 is not None
    assert "LOCATION" not in gliner2.supported_entities


def test_location_not_in_default_strip():
    from pii.core import DEFAULT_STRIP_ENTITIES

    assert "LOCATION" not in DEFAULT_STRIP_ENTITIES


@pytest.mark.model
def test_real_ner_demo_statement_nuances(make_pipeline):
    pipe = make_pipeline(stub_ner=False)
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


@pytest.mark.model
def test_real_ner_bare_town_not_detected_as_location(make_pipeline):
    # Standalone LOCATION detection retired 2026-07-23: a bare town name in
    # prose ('a teacher in Cairns') emits no LOCATION span and passes verbatim.
    # (The ADDRESS passes are untouched and still own address-shaped lines;
    # this asserts only that the dedicated location net is gone.)
    pipe = make_pipeline(stub_ner=False)
    text = "Her partner is a teacher in Cairns."
    results = pipe.analyze(text)
    assert not any(r.entity_type == "LOCATION" for r in results)
    cairns = text.index("Cairns")
    plan = pipe.plan(text)
    assert not any(r.start <= cairns < r.end for r in plan)  # kept verbatim


@pytest.mark.model
def test_real_ner_suburb_in_address_context_still_stripped(make_pipeline):
    # Standalone LOCATION detection is retired (2026-07-23), so a bare town in
    # plain prose passes verbatim (test_real_ner_bare_town_not_detected...).
    # The ADDRESS passes are deliberately untouched, though: a suburb in
    # address-flavored context ('resided in Kew') is still emitted by the
    # ADDRESS pass — verified 2026-07-15 at barely-above-threshold score (Kew
    # 0.433 vs threshold 0.4). This documents the residual, intended overlap;
    # if it starts failing the ADDRESS pass weakened, not a regression here.
    pipe = make_pipeline(stub_ner=False)
    text = "Applicant 1 previously resided in Kew."
    plan = pipe.plan(text)
    kew = text.index("Kew")
    assert any(r.start <= kew < r.end for r in plan)

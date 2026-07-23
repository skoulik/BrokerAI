"""Registry composition policy after the spaCy detector retirement (2026-07-15).

spaCy stays as Presidio's NLP engine (tokens/lemmas → context enhancer) but is
no longer a detector: SpacyRecognizer is gone from the registry and GLiNER2
owns PERSON/ORG/dates and LOCATION. en_core_web_sm's own PERSON/DATE_TIME
emissions were glue spans crossing OCR line breaks ('Emily Watson\\nAddress')
and date-as-name false positives, and its LOCATION recall was worse than
GLiNER2's (6/11 vs 11/11 contextual towns — DONE.md 2026-07-14).

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


def test_gliner2_present_and_owns_location(pipeline):
    gliner2 = _recognizer(pipeline, "Gliner2Recognizer")
    assert gliner2 is not None
    assert "LOCATION" in gliner2.supported_entities


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
def test_real_ner_bare_town_is_location(make_pipeline):
    # The GLiNER2 location pass is now the production contextual-identifier
    # net that replaced spaCy LOCATION: a bare town name in prose is caught.
    pipe = make_pipeline(stub_ner=False)
    text = "Her partner is a teacher in Cairns."
    results = pipe.analyze(text)
    locations = [
        text[r.start : r.end] for r in results if r.entity_type == "LOCATION"
    ]
    assert "Cairns" in locations


@pytest.mark.model
def test_real_ner_short_suburb_rescued_by_address_pass(make_pipeline):
    # 3-letter suburbs are dropped by the location pass's char floor
    # (test_gliner2_floors), but in sentence context the ADDRESS pass emits
    # them — verified 2026-07-15 at barely-above-threshold score (Kew 0.433
    # vs threshold 0.4). If this starts failing, the rescue is gone and the
    # short-suburb exposure widened from contextless mentions to in-context
    # ones too — raise the priority of the gazetteer task in pii/TODO.md.
    # Corpus counterpart: the LOCATION_SHORT probe rows.
    pipe = make_pipeline(stub_ner=False)
    text = "Applicant 1 previously resided in Kew."
    plan = pipe.plan(text)
    kew = text.index("Kew")
    assert any(r.start <= kew < r.end for r in plan)

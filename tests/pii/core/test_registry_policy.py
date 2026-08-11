"""Registry composition policy: what layer 1 does and does not contain.

`PiiPipeline` is layer 1 — patterns, checksums and the invalid-candidate
shadows. Two detectors were deliberately removed from it and must stay out:

- **SpacyRecognizer** (2026-07-15). spaCy stays as Presidio's NLP engine
  (tokens/lemmas → context enhancer) but is not a detector: en_core_web_sm's
  own PERSON/DATE_TIME emissions were glue spans crossing OCR line breaks
  ('Emily Watson\\nAddress') and date-as-name false positives.
- **Gliner2Recognizer** (2026-08-09). Layer 2 is retired outright — layer 0
  (a local LLM, `pii.core.vlm` / `pii.core.text_llm`) beat it on every
  semantic class and seed. The semantic classes it used to own are now
  measured through the eval corpus, not here: they need a model, so there is
  nothing model-free left to assert about them.

URL/IP are removed as irrelevant to financial documents, and standalone
LOCATION detection was retired 2026-07-23 (a bare city/town name is acceptable
verbatim; the layer-0 ADDRESS classes still own address-shaped content).
"""


def _recognizer(pipeline, name):
    return next(
        (r for r in pipeline.analyzer.registry.recognizers if r.name == name),
        None,
    )


def test_spacy_recognizer_retired(pipeline):
    assert _recognizer(pipeline, "SpacyRecognizer") is None


def test_gliner2_recognizer_retired(pipeline):
    """Layer 2 is gone. If this fails, something re-registered an NER model
    into layer 1 — which would also silently re-confound any layer-0 A/B."""
    assert _recognizer(pipeline, "Gliner2Recognizer") is None


def test_no_model_driven_semantic_detection(pipeline):
    """Stronger than the name check: nothing in layer 1 claims the classes
    only a model can decide.

    ADDRESS and DATE_OF_BIRTH are layer-0's outright. PERSON has exactly one
    layer-1 source, and it is a *mechanical* one — `JointNameRecognizer`, the
    initials form 'E & J Moore' that a lexical rule can own (ARCHITECTURE,
    "Mechanical joint-name forms are layer-1 patterns"). Any other PERSON
    source appearing here means an NER model crept back in."""
    for recognizer in pipeline.analyzer.registry.recognizers:
        supported = set(recognizer.supported_entities)
        assert not {"ADDRESS", "DATE_OF_BIRTH"} & supported, recognizer.name
        if "PERSON" in supported:
            assert recognizer.name == "JointNameRecognizer"


def test_layer1_recognizers_still_registered(pipeline):
    """The deletion must not have taken layer 1 with it."""
    for name in (
        "AuTfnRecognizer",
        "AuMedicareRecognizer",
        "AuAbnRecognizer",
        "AuAcnRecognizer",
        "AuBsbRecognizer",
        "AuAccountNumberRecognizer",
        "PayIdRecognizer",
        "JointNameRecognizer",
        "AtfTailRecognizer",
    ):
        assert _recognizer(pipeline, name) is not None, name


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


def test_location_not_in_default_strip():
    from pii.core import DEFAULT_STRIP_ENTITIES

    assert "LOCATION" not in DEFAULT_STRIP_ENTITIES


def test_layer1_still_detects_its_own_classes(pipeline):
    """The registry composition above is only meaningful if layer 1 works."""
    spans, _ = pipeline.detect("TFN 123 456 782 and olga@example.com")
    assert {s.entity_type for s in spans} == {"AU_TFN", "EMAIL_ADDRESS"}

"""Layer-1 composition policy: what the rule set does and does not contain.

`PiiPipeline` is layer 1 — the pattern/checksum rules in `pii.core.recognizers`,
run by `pii.core.engine`. Three detectors were deliberately removed over time
and must stay out:

- **spaCy's NER** (2026-07-15). On OCR text en_core_web_sm produced glue PERSON
  spans crossing line breaks ('Emily Watson\\nAddress') and date-as-name false
  positives. spaCy survived as Presidio's NLP engine until the chassis went.
- **GLiNER2** (2026-08-09). Layer 0 — a local LLM — beat it on every semantic
  class and seed. The classes it owned are measured through the eval corpus
  now: they need a model, so there is nothing model-free left to assert here.
- **Presidio itself** (2026-08-09), and with it a pile of recognizers we never
  stripped on: US SSN/bank/passport/ITIN/licence, NHS, crypto, MAC, medical
  licence, and a DATE_TIME detector. They ran on every call and produced
  nothing but report clutter.

URL/IP are absent as irrelevant to financial documents, and standalone LOCATION
detection was retired 2026-07-23 (a bare city/town name passes verbatim).
"""

import pytest

from pii.core.engine import Rule


def _rule(pipeline, name):
    return pipeline.analyzer.rule(name)


def test_building_a_pipeline_imports_no_presidio_spacy_or_torch():
    """The chassis is ours, and this is not hygiene.

    spaCy pulls in thinc, thinc imports real torch, and torch cannot share a
    Windows process with the GPU paddle wheel (ARCHITECTURE, 'Paddle
    worker-process isolation') — so a re-introduced import would silently put
    the OCR seam back on a worker subprocess it no longer needs.

    Run in a SUBPROCESS deliberately: `sys.modules` in a pytest session is
    polluted by whatever ran earlier, so an in-process assertion would pass or
    fail on test ordering rather than on the thing being claimed."""
    import subprocess
    import sys

    # The FRONT-ENDS too, not just PiiPipeline: image_mode and text_mode each
    # imported RecognizerResult from presidio after the engine swap, which was
    # by itself enough to drag torch back into the image path (2026-08-09).
    probe = (
        "import sys;"
        "import pii.core.image_mode, pii.core.text_mode,"
        " pii.core.csv_mode, pii.core.pdf_mode;"
        "from pii.core import PiiPipeline;"
        "PiiPipeline();"
        "print(','.join(m for m in "
        "('presidio_analyzer', 'spacy', 'thinc', 'torch') if m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "", out.stdout


def test_retired_detectors_absent(pipeline):
    for name in ("SpacyRecognizer", "Gliner2Recognizer", "DateRecognizer"):
        assert _rule(pipeline, name) is None, name


def test_no_model_driven_semantic_detection(pipeline):
    """Stronger than a name check: nothing in layer 1 claims a class only a
    model can decide.

    ADDRESS, DATE_OF_BIRTH and PERSON are layer 0's outright. PERSON lost its
    last pass-1 source when `JointNameRule` was deleted (2026-08-14): a joint
    name is now derived from people who are already known (pii.core.derived,
    layer 1 pass 2), which needs no lexical guess about who is a person. Any
    PERSON source in this registry means an NER model crept back in.

    Pass 2 is deliberately outside this check — it claims PERSON, PERSON_JOINT
    and (since 2026-08-19) ORGANIZATION_TRUSTEE by design, and it reads
    DETECTIONS rather than text, so it cannot be the thing this test guards
    against."""
    for rule in pipeline.analyzer.rules:
        claimed = set(rule.entities)
        assert not {"ADDRESS", "DATE_OF_BIRTH", "PERSON"} & claimed, rule.name


def test_layer1_rules_all_registered(pipeline):
    for name in (
        "AuTfnRule", "AuMedicareRule", "AuMedicareMalformedRule",
        "AuAbnRule", "AuAcnRule", "CreditCardRule",
        "AuBsbRule", "AuAccountNumberRule", "PayIdRule",
        "AuAfslRule", "AuCreditLicenceRule",
        "AtfTailRule",
        "EmailRule", "IbanRule", "PhoneRule",
    ):
        assert _rule(pipeline, name) is not None, name


def test_every_rule_declares_what_it_emits(pipeline):
    for rule in pipeline.analyzer.rules:
        assert isinstance(rule, Rule), rule
        assert rule.entities, rule.name
        assert rule.name


def test_no_url_ip_or_us_specific_classes(pipeline):
    """Presidio's default registry shipped ten recognizers we never stripped
    on. Nothing may claim their classes now."""
    unwanted = {
        "URL", "IP_ADDRESS", "US_SSN", "US_BANK_NUMBER", "US_PASSPORT",
        "US_DRIVER_LICENSE", "US_ITIN", "UK_NHS", "CRYPTO", "MEDICAL_LICENSE",
        "MAC_ADDRESS", "DATE_TIME",
    }
    claimed = {e for rule in pipeline.analyzer.rules for e in rule.entities}
    assert not unwanted & claimed


@pytest.mark.parametrize("entity", ["URL", "IP_ADDRESS", "LOCATION"])
def test_not_in_default_strip(entity):
    from pii.core import DEFAULT_STRIP_ENTITIES

    assert entity not in DEFAULT_STRIP_ENTITIES


def test_layer1_still_detects_its_own_classes(pipeline):
    """The composition above is only meaningful if layer 1 works."""
    spans, _ = pipeline.detect("TFN 123 456 782 and olga@example.com")
    assert {s.entity_type for s in spans} == {"AU_TFN", "EMAIL_ADDRESS"}

"""Shared testbench fixtures.

Heavyweight objects are session-scoped so they are built once per run
(testbench design, root ROADMAP Phase 2): every PiiPipeline loads the spacy
NLP engine.

The suite is model-free by construction since GLiNER2 was retired
(2026-08-09): `PiiPipeline` is layer 1 only — patterns, checksums and the
shadows — and the semantic detector is layer 0, which the tests inject as a
stub. There is no model to shim any more, so the old sys.modules GLiNER2 stub
and its `stub_ner` switch are gone with it.
"""

import pytest

from pii.core.vlm import DetectorResult, VlmFinding


@pytest.fixture(scope="session")
def make_pipeline():
    """Cached PiiPipeline factory — one instance per distinct configuration
    for the whole session."""
    from pii.core import PiiPipeline

    cache = {}

    def make(**kwargs):
        key = tuple(sorted((k, repr(v)) for k, v in kwargs.items()))
        if key not in cache:
            cache[key] = PiiPipeline(**kwargs)
        return cache[key]

    return make


@pytest.fixture(scope="session")
def pipeline(make_pipeline):
    """Default pipeline (layer 1) with default settings."""
    return make_pipeline()


class StubDetector:
    """Stands in for a layer-0 detector: returns a fixed finding list and
    ignores the text.

    Detection is what is being stubbed — location, layer-1 refinement and the
    merge all run for real against it, so a test using this still exercises
    everything below the model.
    """

    def __init__(self, *pairs):
        self.findings = [
            VlmFinding(text=text, entity_type=etype) for text, etype in pairs
        ]
        self.seen = []

    def detect(self, text):
        self.seen.append(text)
        return DetectorResult(list(self.findings))

    def localize(self, image, findings):
        return DetectorResult(list(findings))


@pytest.fixture
def stub_detector():
    """The layer-0 stub class, for tests that build their own findings."""
    return StubDetector


@pytest.fixture
def no_findings():
    """A layer-0 detector that finds nothing — isolates layer 1."""
    return StubDetector()


@pytest.fixture
def cli_no_model(monkeypatch):
    """Keep a `pii.cli.main` run model-free.

    The CLI always builds a layer-0 detector now (there is no layers path to
    fall back to), so a CLI test would otherwise demand a model server.
    Patched at the module attribute because `_build_detector` imports it
    lazily, inside the call."""
    monkeypatch.setattr(
        "pii.core.text_llm.TextDetector", lambda *a, **k: StubDetector()
    )

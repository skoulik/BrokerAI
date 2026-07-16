"""Shared testbench fixtures.

Heavyweight objects are session-scoped so they are built once per run
(testbench design, root ROADMAP Phase 2): every PiiPipeline loads the spacy
NLP engine, and a real one also loads GLiNER2. The default `make_pipeline`
builds under a GLiNER2 shim (sys.modules stub, no model) so the fast suite
stays model-free; pass stub_ner=False under the `model` marker for the real
stack.
"""

import contextlib
import sys
import types

import pytest
from presidio_analyzer import EntityRecognizer


class _NoopGliner2(EntityRecognizer):
    """Stands in for Gliner2Recognizer so the registry can be composed without
    loading the model. Mirrors the real recognizer's contract — it now owns
    LOCATION alongside PERSON — but emits nothing."""

    def __init__(self):
        super().__init__(
            supported_entities=["PERSON", "LOCATION"], name="Gliner2Recognizer"
        )

    def load(self):
        pass

    def analyze(self, text, entities, nlp_artifacts=None):
        return []


# The deferred-import target in PiiPipeline.__init__ (pii/core/pipeline.py) —
# the shim key below and that import must name the same module.
_GLINER2_MODULE = "pii.core.gliner2_recognizer"


@contextlib.contextmanager
def _gliner2_stub():
    """Shim the GLiNER2 recognizer module with the noop stub while a
    PiiPipeline is constructed (its Gliner2Recognizer import is deferred
    into __init__)."""
    stub = types.ModuleType(_GLINER2_MODULE)
    stub.Gliner2Recognizer = _NoopGliner2
    saved = sys.modules.get(_GLINER2_MODULE)
    sys.modules[_GLINER2_MODULE] = stub
    try:
        yield
    finally:
        if saved is None:
            del sys.modules[_GLINER2_MODULE]
        else:
            sys.modules[_GLINER2_MODULE] = saved


@pytest.fixture
def gliner2_stub():
    """The GLiNER2 stub context manager, for tests that build a pipeline
    indirectly (e.g. through the CLI, which constructs its own PiiPipeline)."""
    return _gliner2_stub


@pytest.fixture(scope="session")
def make_pipeline():
    """Cached PiiPipeline factory — one instance per distinct configuration
    for the whole session. stub_ner=True (default) builds under the GLiNER2
    shim → fast, model-free; pass stub_ner=False for the real stack and carry
    the `model` marker. stub_ner is part of the cache key but not forwarded to
    PiiPipeline."""
    from pii.core import PiiPipeline

    cache = {}

    def make(**kwargs):
        stub_ner = kwargs.pop("stub_ner", True)
        key = (stub_ner,) + tuple(
            sorted((k, repr(v)) for k, v in kwargs.items())
        )
        if key not in cache:
            if stub_ner:
                with _gliner2_stub():
                    cache[key] = PiiPipeline(**kwargs)
            else:
                cache[key] = PiiPipeline(**kwargs)
        return cache[key]

    return make


@pytest.fixture(scope="session")
def pipeline(make_pipeline):
    """Default pipeline (stubbed NER) with default settings."""
    return make_pipeline()

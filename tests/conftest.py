"""Shared testbench fixtures.

Heavyweight objects are session-scoped so they are built once per run
(testbench design, root ROADMAP Phase 2): every PiiPipeline construction
loads the spacy model, and NER-enabled ones load GLiNER2.
"""

import pytest


@pytest.fixture(scope="session")
def make_pipeline():
    """Cached PiiPipeline factory — one instance per distinct configuration
    for the whole session. NER defaults to off; tests that need GLiNER2 must
    pass use_ner=True and carry the `model` marker."""
    from pii.pipeline import PiiPipeline

    cache = {}

    def make(**kwargs):
        kwargs.setdefault("use_ner", False)
        key = tuple(sorted((k, repr(v)) for k, v in kwargs.items()))
        if key not in cache:
            cache[key] = PiiPipeline(**kwargs)
        return cache[key]

    return make


@pytest.fixture(scope="session")
def pipeline(make_pipeline):
    """Patterns-only pipeline with default settings."""
    return make_pipeline()

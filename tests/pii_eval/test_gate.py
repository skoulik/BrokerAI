"""Tier-1 acceptance gate as a testbench entry point.

The statistical corpus harness (python -m pii_eval generate/score) stays the
primary tool; this marked test makes "run everything" a single pytest
command (root ROADMAP Phase 2, testbench task).
"""

import pytest

from pii_eval.generate import generate
from pii_eval.score import score


@pytest.mark.slow
@pytest.mark.model
def test_tier1_zero_critical_miss_gate(tmp_path):
    corpus = tmp_path / "corpus"
    generate(str(corpus), seed=42, docs=9)
    assert score(str(corpus)) == 0, "critical PII leaked on the tier-1 corpus"

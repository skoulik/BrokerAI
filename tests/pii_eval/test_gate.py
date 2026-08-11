"""Tier-1 acceptance gate as a testbench entry point.

The statistical corpus harness (python -m pii_eval generate/score) stays the
primary tool; this marked test makes "run everything" a single pytest
command (root ROADMAP Phase 2, testbench task).

**This needs a running llama-server** (set `$PII_VLM_URL`, or it tries
http://localhost:8080). Since GLiNER2 was retired on 2026-08-09 the semantic
detector is layer 0, so there is no offline path left for the gate to take —
that is a deliberate consequence of the retirement, recorded in
core/ARCHITECTURE.md. An unreachable server FAILS rather than skips: a gate
that quietly excuses itself is not a gate.
"""

import pytest

from pii.core.vlm import VlmUnavailable
from pii_eval.generate import generate
from pii_eval.score import score


@pytest.mark.slow
@pytest.mark.model
def test_tier1_zero_critical_miss_gate(tmp_path):
    corpus = tmp_path / "corpus"
    generate(str(corpus), seed=42, docs=9)
    try:
        result = score(str(corpus))
    except VlmUnavailable as exc:
        pytest.fail(
            f"the tier-1 gate needs a llama-server for layer-0 detection "
            f"(set $PII_VLM_URL): {exc}"
        )
    assert result == 0, "critical PII leaked on the tier-1 corpus"

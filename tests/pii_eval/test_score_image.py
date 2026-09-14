"""The survival scorers' reporting of model replies, and the budget flag.

Model-free: the detector is built but never asked anything, and the scorer is
replaced where the command line is checked. A run at 8192 against one at 4096
is only a comparison if the budget really reaches the model and the count of
passes that hit it really reaches the output."""

import sys

import pytest

from pii.core.vlm import Incomplete
from pii_eval.score_image import budget_line, build_detector, incomplete_note


def test_the_budget_reaches_the_detector_under_test():
    assert build_detector(reasoning_budget=8192).reasoning_budget == 8192


def test_a_budget_hit_is_noted_apart_from_an_unfinished_answer():
    assert incomplete_note(Incomplete()) == ""
    hit = incomplete_note(Incomplete(reasoning_budget_hit=2))
    assert "2 pass(es) reached the reasoning budget" in hit
    # `!!` marks a page whose score means something else; a whole answer does not.
    assert "!!" not in hit
    both = incomplete_note(Incomplete(truncated=1, reasoning_budget_hit=2))
    assert "!! 1 cut-off" in both and "2 pass(es)" in both


def test_the_run_total_names_the_budget_it_was_taken_at():
    line = budget_line(Incomplete(reasoning_budget_hit=8), 4096)
    assert "4096" in line and "8 pass(es) reached it" in line


@pytest.mark.parametrize(
    "argv, scorer",
    [
        (["score", "--modality", "pdf", "-c", "corpus"], "pii_eval.score_pdf.score_pdf"),
        (["score", "--modality", "image", "-c", "corpus"], "pii_eval.score_image.score_image"),
        (["score", "-c", "corpus"], "pii_eval.score.score"),
        (["ground", "-c", "corpus"], "pii_eval.score_grounding.score_grounding"),
    ],
)
def test_the_budget_flag_reaches_every_scorer(monkeypatch, argv, scorer):
    from pii_eval.__main__ import main

    seen = {}
    monkeypatch.setattr(scorer, lambda *a, **k: seen.update(k) or 0)
    monkeypatch.setattr(sys, "argv", ["pii_eval", *argv, "--reasoning-budget", "8192"])
    assert main() == 0
    assert seen["reasoning_budget"] == 8192

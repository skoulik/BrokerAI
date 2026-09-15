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


def _survival(reread, *values, keep=()):
    from pii_eval.score_image import _score_survival

    entities = [{"type": "ORGANIZATION", "value": v, "strip_expected": True}
                for v in values]
    entities += [{"type": "ORGANIZATION", "value": v, "strip_expected": False}
                 for v in keep]
    _score_survival(entities, reread)
    return {e["value"]: e["verdict"] for e in entities}


def test_a_truncation_read_only_inside_the_surviving_longer_value_was_painted():
    # The truncation's own printing is gone ("ID 2 PERSON_6"); its letters are
    # still readable, but only as the start of the longer value that leaked.
    verdicts = _survival("ID 2 PERSON_6 ORG 6\nLinked Acc Trns Acme Constructi 50.00",
                         "Acme Co", "Acme Constructi")
    assert verdicts == {"Acme Co": "stripped", "Acme Constructi": "leaked"}


def test_a_truncation_with_a_printing_of_its_own_still_leaks():
    verdicts = _survival("to Acme Co 10.00\nTrns Acme Constructi 50.00",
                         "Acme Co", "Acme Constructi")
    assert verdicts == {"Acme Co": "leaked", "Acme Constructi": "leaked"}


def test_a_glued_printing_still_leaks():
    # Why this is not a word-boundary rule: the reread joins words.
    assert _survival("Funds transfer fromacme 1,500.00", "ACME") == {"ACME": "leaked"}


def test_containment_is_judged_in_the_squashed_space_too():
    # Neither value is readable exactly (1 for I, 0 for O); both squash-match.
    verdicts = _survival("Acme BUS1NESS TRUST 0NE", "Acme Business Trus",
                         "Acme Business Trust One")
    assert verdicts == {"Acme Business Trus": "stripped",
                        "Acme Business Trust One": "leaked"}


def test_an_edit_distance_match_neither_excuses_nor_is_excused():
    # 'Acme Constructiox' is readable only at edit distance 1, which has no
    # position: the shorter value inside it keeps counting as a leak.
    verdicts = _survival("Trns Acme Constructiox 50.00", "Acme Constructi",
                         "Acme Constructioxx")
    assert verdicts["Acme Constructi"] == "leaked"


def test_a_kept_value_read_only_inside_a_leaked_one_was_over_stripped():
    verdicts = _survival("Payment to Acme Bank Holdings Pty", "Acme Bank Holdings Pty",
                         keep=("Acme Bank",))
    assert verdicts == {"Acme Bank Holdings Pty": "leaked", "Acme Bank": "over-stripped"}


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

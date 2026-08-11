"""Confusion-weighted edit distance.

The load-bearing property is that the confusion table is a DISCOUNT, not a
gate: everything it does not know about still gets a usable metric. Dual
coverage per the project rule — the corpus probe is the other half.
"""

from pii.core import fuzzy


# ------------------------------------------------------- the discount itself


def test_identical_strings_cost_nothing():
    assert fuzzy.distance("162097111", "162097111") == 0.0


def test_a_listed_confusion_is_cheaper_than_an_arbitrary_substitution():
    cheap = fuzzy.distance("s12345", "512345")   # 5 <-> s, listed
    dear = fuzzy.distance("q12345", "512345")    # q <-> 5, not listed
    assert cheap < dear
    assert cheap == fuzzy.CONFUSION_COST and dear == 1.0


def test_the_table_only_ever_discounts():
    # No pair may be made MORE expensive than a plain substitution — the table
    # must never turn a match that plain Levenshtein would accept into a miss.
    for pair in fuzzy.CONFUSION_PAIRS:
        a, b = sorted(pair)
        assert fuzzy.substitution_cost(a, b) <= 1.0


# ------------------------------------------- what a substitution table cannot do


def test_a_dropped_character_still_scores():
    # The case a fold-through-confusion-classes matcher cannot express at all:
    # OCR lost a digit. Plain indel cost, and the budget absorbs it.
    assert fuzzy.distance("162097114", "1620971114") == 1.0
    assert fuzzy.matches("162097114", "1620971114") == 1.0


def test_an_inserted_character_still_scores():
    assert fuzzy.matches("16209711141", "1620971114") == 1.0


def test_damage_outside_the_table_degrades_to_plain_levenshtein():
    # 'x' is in no confusion pair; the distance is still the useful 1.0
    # rather than the "no match" a lookup table would return.
    assert fuzzy.distance("1620971x14", "1620971114") == 1.0


def test_the_measured_top_confusion_arrives_as_a_deletion():
    # `0` read as `@` (Consolas slashed zero) is the top measured pair, and it
    # cannot reach the table: `@` does not survive the alphanumeric squash, so
    # the damage is a DELETION. This is the concrete reason the table alone is
    # insufficient — see the module docstring.
    from pii.core.vlm import squash_map

    damaged, _ = squash_map("BSB @83-064")
    truth, _ = squash_map("BSB 083-064")
    assert "@" not in damaged
    assert fuzzy.matches(damaged, truth) == 1.0


# ------------------------------------------------------------------ budgets


def test_short_needles_get_a_floor_not_a_zero_budget():
    # 0.25 * 4 == 1.0 would round down to nothing useful for short
    # identifiers; the floor keeps one edit affordable.
    assert fuzzy.budget_for("4000") >= 1.0
    assert fuzzy.matches("4001", "4000") is not None


def test_damage_beyond_the_budget_is_rejected():
    assert fuzzy.matches("999999999", "1620971114") is None


def test_wildly_different_lengths_are_rejected_early():
    # The length-difference bail must agree with the full DP's verdict.
    assert fuzzy.matches("1", "1620971114") is None


def test_ceiling_never_reports_below_the_true_distance():
    # An early bail must not masquerade as a good score.
    assert fuzzy.distance("abcdef", "uvwxyz", ceiling=1.0) > 1.0


def test_empty_strings():
    assert fuzzy.distance("", "abc") == 3.0
    assert fuzzy.distance("abc", "") == 3.0
    assert fuzzy.distance("", "") == 0.0


def test_distance_matches_a_naive_reference_on_random_strings():
    """The inner loop is written for speed (per-row cost table, neighbours
    carried in locals), so it is pinned against the textbook recurrence rather
    than trusted. A wrong edit distance here silently changes what gets
    redacted."""
    import random

    from pii.core import fuzzy

    def reference(a: str, b: str, costs) -> float:
        rows = [[0.0] * (len(b) + 1) for _ in range(len(a) + 1)]
        for i in range(len(a) + 1):
            rows[i][0] = float(i)
        for j in range(len(b) + 1):
            rows[0][j] = float(j)
        for i in range(1, len(a) + 1):
            for j in range(1, len(b) + 1):
                sub = (
                    0.0 if a[i - 1] == b[j - 1]
                    else costs.get(a[i - 1], {}).get(b[j - 1], 1.0)
                )
                rows[i][j] = min(
                    rows[i - 1][j] + 1.0,
                    rows[i][j - 1] + 1.0,
                    rows[i - 1][j - 1] + sub,
                )
        return rows[-1][-1]

    rng = random.Random(42)
    alphabet = "abcdefgilos0125689"  # dense in confusable characters
    for _ in range(400):
        a = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 12)))
        b = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 12)))
        for costs in (fuzzy.CONFUSION_COSTS, fuzzy.IDENTIFIER_COSTS):
            expected = reference(a, b, costs)
            assert fuzzy.distance(a, b, costs=costs) == expected, (a, b)
            # ...and the ceiling must only ever report "not within budget",
            # never a wrong value below it.
            for ceiling in (0.5, 1.0, 2.0, 3.0):
                got = fuzzy.distance(a, b, ceiling=ceiling, costs=costs)
                if expected <= ceiling:
                    assert got == expected, (a, b, ceiling)
                else:
                    assert got > ceiling, (a, b, ceiling)


def test_the_cost_tables_agree_with_the_documented_cost_functions():
    """The tables are what the DP runs; the functions are what the docstrings
    explain. They must not drift."""
    from pii.core import fuzzy

    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789@- "
    for a in alphabet:
        for b in alphabet:
            assert fuzzy.CONFUSION_COSTS.get(a, {}).get(b, 1.0) == (
                fuzzy.substitution_cost(a, b) if a != b else 1.0
            ) or a == b
            assert fuzzy.IDENTIFIER_COSTS.get(a, {}).get(b, 1.0) == (
                fuzzy.identifier_substitution_cost(a, b) if a != b else 1.0
            ) or a == b

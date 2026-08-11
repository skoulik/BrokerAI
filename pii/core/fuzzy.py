"""Confusion-weighted edit distance — the fuzzy tier of value location.

Two transcriptions of the same pixels disagree in ways the alphanumeric
squash cannot absorb: OCR misreads a glyph, drops one, or splits one into
two. `locator.py` uses this to recognize a value through that damage, but
ONLY inside the neighbourhood a VLM box marks out — the rationale for the
constraint is in that module, and it is what makes edit distance safe here
at all.

**The confusion table is a discount inside the DP, never a gate in front of
it.** The tempting alternative — fold both strings through confusion classes
and test equality — fails on exactly the cases this exists for: damage to a
character the table does not list, and characters *dropped* or *inserted*,
which a substitution table structurally cannot express. Weighted Levenshtein
degrades to plain Levenshtein wherever the table is silent, so a known
confusion is cheaper and an unknown one is still merely expensive.

The concrete case that shapes it: the measured top confusion is `0` read as
`@` (Consolas slashed zero). `@` is not alphanumeric, so it does not survive
the squash at all — the damage reaches this function as a DELETION, not a
substitution, and no confusion table of any size would catch it. Indels
carry full cost and the budget absorbs them.

Callers normalize first (`vlm.squash_map` lowercases and drops
non-alphanumerics); nothing here folds case.
"""

from __future__ import annotations

from functools import lru_cache

# Substitution cost for a listed pair. Low enough that a confusable glyph is
# nearly free, high enough that a run of them still costs more than a clean
# match — a value differing by four "cheap" characters should not outrank one
# differing by a single expensive character.
CONFUSION_COST = 0.25

# Unordered confusion pairs, lowercased (callers squash first).
#
# Provenance matters here, because the folklore set is measurably wrong. The
# first group is MEASURED — the top pairs from the 2026-07-17 OCR fidelity
# sweep (see core/DONE.md), which the hand-written folklore list had missed.
# The second is classic glyph-shape confusion, kept because it costs nothing
# to be generous: a discount can only make a match cheaper, and the box
# constraint in locator.py is what actually prevents a wrong region being
# painted. Direction is not modelled (pairs are symmetric) — the damage
# direction is truth->OCR, but the needle is a VLM transcription that carries
# its own reading errors, so neither side is authoritative.
_MEASURED = [
    ("j", "3"),
    ("1", "2"),
    ("4", "8"),
    ("w", "h"),
]
_GLYPH_SHAPE = [
    ("0", "o"), ("1", "l"), ("1", "i"), ("l", "i"), ("5", "s"), ("8", "b"),
    ("2", "z"), ("6", "g"), ("9", "g"), ("7", "t"), ("u", "v"), ("c", "e"),
]

CONFUSION_PAIRS: frozenset[frozenset[str]] = frozenset(
    frozenset(pair) for pair in _MEASURED + _GLYPH_SHAPE
)

# The CROSS-CLASS subset: pairs where exactly one side is a digit. A letter
# standing where a digit belongs in an identifier is a transcription error;
# a digit standing where a DIFFERENT digit belongs is a different value.
#
# The distinction has no meaning for the locator (a box already guarantees the
# region, so discounting 1<->2 only helps recognize a value known to be there),
# but it is decisive for `grouping.py`, which uses the same distance to decide
# whether two strings are the same entity and has no positional anchor. There,
# discounting the digit<->digit pairs — `1<->2` and `4<->8`, both from the
# MEASURED set — would merge two different account numbers.
#
# DERIVED rather than hand-listed, so a refresh of the table above cannot leave
# a stale copy behind: a new pair lands in the right bucket by construction.
IDENTIFIER_CONFUSION_PAIRS: frozenset[frozenset[str]] = frozenset(
    pair for pair in CONFUSION_PAIRS
    if sum(1 for ch in pair if ch.isdigit()) == 1
)

# The non-digit halves of those pairs: the characters that can stand in for a
# digit. `grouping.py` counts them when deciding whether a value is
# identifier-shaped, so that a misread glyph cannot flip a value out of that
# shape and quietly hand it the permissive table.
DIGIT_CONFUSABLES: frozenset[str] = frozenset(
    ch for pair in IDENTIFIER_CONFUSION_PAIRS for ch in pair if not ch.isdigit()
)

# A match is allowed this fraction of the needle's length in edit cost, with
# a floor so short identifiers get any tolerance at all. 0.25 lets a 12-char
# account number absorb three edits; the box constraint carries the burden of
# ensuring those edits land on the right value.
MAX_RATIO = 0.25
MIN_BUDGET = 1.0


@lru_cache(maxsize=4096)
def substitution_cost(a: str, b: str) -> float:
    """Cost of reading `a` as `b`: free if identical, discounted for a listed
    confusion pair, full price otherwise."""
    if a == b:
        return 0.0
    return CONFUSION_COST if frozenset((a, b)) in CONFUSION_PAIRS else 1.0


@lru_cache(maxsize=4096)
def identifier_substitution_cost(a: str, b: str) -> float:
    """`substitution_cost` restricted to the CROSS-CLASS pairs.

    For a digit-shaped value, a digit read as a letter is damage and a digit
    read as another digit is a different value — see
    IDENTIFIER_CONFUSION_PAIRS. Used by `grouping.py`, never by the locator."""
    if a == b:
        return 0.0
    return (
        CONFUSION_COST
        if frozenset((a, b)) in IDENTIFIER_CONFUSION_PAIRS
        else 1.0
    )


def budget_for(needle: str) -> float:
    """The edit cost a needle of this length may absorb and still match."""
    return max(MIN_BUDGET, MAX_RATIO * len(needle))


def distance(
    a: str,
    b: str,
    ceiling: float | None = None,
    cost=substitution_cost,
) -> float:
    """Weighted Levenshtein distance between `a` and `b`.

    Insertions and deletions cost 1.0; substitutions cost per `cost`, which
    selects the admissible confusion table (`substitution_cost` for location,
    `identifier_substitution_cost` for identity — see that function).
    `ceiling` bails out early once every cell of a row exceeds it, returning a
    value strictly greater than the ceiling — the caller only ever needs to
    know "not within budget", and the locator calls this across every word
    window in a box.
    """
    if a == b:
        return 0.0
    if not a:
        return float(len(b))
    if not b:
        return float(len(a))
    if ceiling is not None and abs(len(a) - len(b)) > ceiling:
        # Indels alone already exceed the budget; no alignment can recover.
        return ceiling + 1.0

    previous = [float(i) for i in range(len(b) + 1)]
    for i, ch_a in enumerate(a, 1):
        current = [float(i)]
        for j, ch_b in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1.0,                     # delete
                    current[j - 1] + 1.0,                  # insert
                    previous[j - 1] + cost(ch_a, ch_b),    # substitute
                )
            )
        if ceiling is not None and min(current) > ceiling:
            return ceiling + 1.0
        previous = current
    return previous[-1]


def matches(candidate: str, needle: str) -> float | None:
    """`distance` if `candidate` is within `needle`'s budget, else None.

    Returning the distance rather than a bool is deliberate: the locator
    ranks competing windows against each other, so it needs how close, not
    merely whether.
    """
    budget = budget_for(needle)
    found = distance(candidate, needle, ceiling=budget)
    return found if found <= budget else None

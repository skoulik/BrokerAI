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

# What counts as identifier-shaped, measured on the squashed form. Lives here,
# beside the tables it selects between, because `grouping.py` (are these the
# same entity?) and `locator.py` (is this the same value?) must not disagree
# about which table a value admits.
IDENTIFIER_DIGIT_RATIO = 0.6
IDENTIFIER_MIN_DIGITS = 2


def identifier_shaped(squashed: str) -> bool:
    """Whether a value's identity lives in its digits.

    Measured AFTER allowing digit homoglyphs, so a misread glyph cannot flip a
    value out of identifier shape and quietly hand it the permissive table. The
    real-digit floor keeps letter-only words out: 'boss' and 'log' are made
    entirely of digit confusables and would otherwise qualify.
    """
    if not squashed:
        return False
    if sum(1 for ch in squashed if ch.isdigit()) < IDENTIFIER_MIN_DIGITS:
        return False
    like = sum(
        1 for ch in squashed if ch.isdigit() or ch in DIGIT_CONFUSABLES
    )
    return like >= IDENTIFIER_DIGIT_RATIO * len(squashed)

# A match is allowed this fraction of the needle's length in edit cost, with
# a floor so short identifiers get any tolerance at all. 0.25 lets a 12-char
# account number absorb three edits; the box constraint carries the burden of
# ensuring those edits land on the right value.
MAX_RATIO = 0.25
MIN_BUDGET = 1.0


@lru_cache(maxsize=4096)
def substitution_cost(a: str, b: str) -> float:
    """Cost of reading `a` as `b`: free if identical, discounted for a listed
    confusion pair, full price otherwise.

    NOT dead code although `distance` reads `CONFUSION_COSTS` instead: this is
    the readable statement of the semantics, and a test pins the table against
    it so the fast form cannot drift from the documented one."""
    if a == b:
        return 0.0
    return CONFUSION_COST if frozenset((a, b)) in CONFUSION_PAIRS else 1.0


@lru_cache(maxsize=4096)
def identifier_substitution_cost(a: str, b: str) -> float:
    """`substitution_cost` for values whose identity is their digits.

    Three outcomes rather than two, because "a digit read as another digit is a
    different value" has to be a PROHIBITION, not a high price. A budget large
    enough to absorb a truncation would otherwise also absorb a digit swap, and
    an account number one digit different is a different account:

    - a listed CROSS-CLASS pair (a digit read as a letter) is damage, discounted;
    - digit against a DIFFERENT digit is infinite — no budget can pay for it;
    - anything else is full price.

    Indels are unaffected and stay at 1.0, which is what lets a truncated
    identifier still match its full form.
    """
    if a == b:
        return 0.0
    if frozenset((a, b)) in IDENTIFIER_CONFUSION_PAIRS:
        return CONFUSION_COST
    if a.isdigit() and b.isdigit():
        return float("inf")
    return 1.0


def budget_for(needle: str) -> float:
    """The edit cost a needle of this length may absorb and still match."""
    return max(MIN_BUDGET, MAX_RATIO * len(needle))


def _cost_table(pairs, forbid_digit_swaps: bool) -> dict[str, dict[str, float]]:
    """`{a: {b: cost}}` for every non-default substitution, both directions.

    The DP below runs this table instead of calling a cost FUNCTION per cell:
    a cached call measured 0.14 us against ~0.3 us for the whole cell, so half
    the inner loop was Python call overhead. Anything absent costs 1.0, and
    equality is checked inline, so only the exceptions live here.

    Digit-against-digit is materialized explicitly when forbidden (90 entries)
    rather than left to a default, so the lookup stays a single `.get`.
    """
    table: dict[str, dict[str, float]] = {}

    def put(a: str, b: str, cost: float) -> None:
        table.setdefault(a, {})[b] = cost

    if forbid_digit_swaps:
        for a in "0123456789":
            for b in "0123456789":
                if a != b:
                    put(a, b, float("inf"))
    for pair in pairs:
        a, b = sorted(pair)
        put(a, b, CONFUSION_COST)
        put(b, a, CONFUSION_COST)
    return table


# The two admissible tables, in the form `distance` consumes.
CONFUSION_COSTS = _cost_table(CONFUSION_PAIRS, forbid_digit_swaps=False)
IDENTIFIER_COSTS = _cost_table(
    IDENTIFIER_CONFUSION_PAIRS, forbid_digit_swaps=True
)
_NO_EXCEPTIONS: dict[str, float] = {}


def distance(
    a: str,
    b: str,
    ceiling: float | None = None,
    costs: dict[str, dict[str, float]] = CONFUSION_COSTS,
) -> float:
    """Weighted Levenshtein distance between `a` and `b`.

    Insertions and deletions cost 1.0; substitutions cost per `costs`, which
    selects the admissible confusion table (`CONFUSION_COSTS` for location,
    `IDENTIFIER_COSTS` for values whose identity is their digits).
    `ceiling` bails out early once every cell of a row exceeds it, returning a
    value strictly greater than the ceiling — the caller only ever needs to
    know "not within budget", and the locator calls this across every word
    window in a box or on a page.

    The inner loop is written for speed rather than symmetry (it is the whole
    cost of borrowed matching, measured): the substitution table is read as a
    per-row dict rather than through a function call, and the diagonal and left
    neighbours are carried in locals instead of being indexed out of the two
    rows. `substitution_cost` documents the same semantics readably.
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

    n, m = len(a), len(b)
    # Reaching cell (i, j) needs at least |i - j| indels at 1.0 apiece, so with
    # a ceiling nothing outside that diagonal band can be on a qualifying path.
    # Computing only the band is what makes this affordable to call across
    # every word run of a page: at ceiling 4 a 24-character comparison drops
    # from 24 cells a row to 9. With no ceiling the band is the whole matrix
    # and this is the textbook recurrence.
    band = int(ceiling) if ceiling is not None else max(n, m)
    infinity = float("inf")

    previous = [float(j) if j <= band else infinity for j in range(m + 1)]
    for i in range(1, n + 1):
        ch_a = a[i - 1]
        row = costs.get(ch_a, _NO_EXCEPTIONS)
        lo = max(1, i - band)
        hi = min(m, i + band)
        current = [infinity] * (m + 1)
        best = current[0] = float(i) if i <= band else infinity
        diagonal = previous[lo - 1]  # previous[j - 1]
        left = current[lo - 1]       # current[j - 1]
        for j in range(lo, hi + 1):
            up = previous[j]
            ch_b = b[j - 1]
            cell = diagonal if ch_a == ch_b else diagonal + row.get(ch_b, 1.0)
            if up + 1.0 < cell:
                cell = up + 1.0
            if left + 1.0 < cell:
                cell = left + 1.0
            current[j] = cell
            if cell < best:
                best = cell
            diagonal = up
            left = cell
        if ceiling is not None and best > ceiling:
            return ceiling + 1.0
        previous = current
    return previous[m]


def matches(candidate: str, needle: str) -> float | None:
    """`distance` if `candidate` is within `needle`'s budget, else None.

    Returning the distance rather than a bool is deliberate: the locator
    ranks competing windows against each other, so it needs how close, not
    merely whether.
    """
    budget = budget_for(needle)
    found = distance(candidate, needle, ceiling=budget)
    return found if found <= budget else None

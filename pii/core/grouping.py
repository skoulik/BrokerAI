"""Document-wide entity grouping: one view of each value across all pages.

Layer 0 reads one page at a time, so its findings are per-page opinions: a
value it names on page 1 and misses on page 4 was redacted on page 1 and leaked
on page 4, and nothing in a streaming per-page pipeline could notice. This
module is the fold between the two sweeps — every page's findings in, one
document-wide view out — and `locator.locate_borrowed` is what then applies
that view to every page's OCR text.

**Grouping decides the class and the report; it does not produce recall.**
Every constituent is searched independently, so the flat set of variant strings
is what yields spans. A mis-grouping therefore cannot cause a miss or a
mis-paint — it can only mislabel, and a mislabel is visible in the group table.

**Comparison normalizes; storage and search never do.** Distance runs on the
case-folded, separator-collapsed form (`vlm.squash_map`); a group stores each
constituent's ORIGINAL text verbatim and the borrowed pass searches with those
originals. Case is the dominant variant pair in these documents — the same name
in caps in a header and title case in the body — and raw edit distance is blind
to it ('SMITH JOHN' vs 'Smith John' is 8 edits).

**One distance rule, two admissible tables.** `GROUP_BUDGET` reads: *any number
of known glyph confusions, but not a single genuine character difference*. A
listed confusion costs 0.25 so several fit inside 0.9; an ordinary substitution
or an indel costs 1.0 and splits the group. Which pairs are listed depends on
the value's shape — identifier-shaped values admit only the CROSS-CLASS pairs,
because a digit read as a letter is damage while a digit read as another digit
is a different account (`fuzzy.IDENTIFIER_CONFUSION_PAIRS`).

`fuzzy.py` warns against folding two strings through confusion classes and
testing equality, which is close to what happens here. That objection is about
*locating*, where a failed match leaves a value unpainted — a leak. Here a
failed comparison merely splits one group into two, each still searched
document-wide: no recall is lost, and the cost is one extra row in the report.
The failure mode does not transfer.

**The vote is a two-way decision, and that is deliberate** (Sergei,
2026-08-11). The elected class replaces every member's own class, so a value
reported as PII_COMPANY on ten pages and PII_NAME on one is treated as a
company everywhere — including on the page that named a person. This is the
first mechanism in the tool that can *un-redact* something a per-page run would
have redacted, which is why `EntityGroup.votes` is carried into the CLI report:
the listing is the audit surface for that decision, not decoration.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from pii.core import fuzzy
from pii.core.locator import Needle
from pii.core.vlm import VlmFinding, squash_map

# Tie-break order for the vote, most-strip-worthy first. Only the ordering
# matters, and only PERSON's position is load-bearing now: its leak is an
# automatic acceptance failure (pii_eval.build.CRITICAL), so it must win a tie.
# ORGANIZATION stays last as the most over-emitted class, but the reason it was
# put there is gone — it used to be the one class layer 0 emits that was KEPT
# by default, so a tie it won meant no redaction at all. Since 2026-08-11 every
# class strips unless the keep list exempts the value (pii.core.entity_keep),
# so this ordering decides a PLACEHOLDER LABEL, not whether anything is
# redacted. Types absent from this tuple rank last.
CLASS_PRIORITY = (
    "PERSON",
    "IDENTIFIER_GENERIC",
    "ADDRESS",
    "DATE_OF_BIRTH",
    "ORGANIZATION",
)

# Edit budget for "these two strings are the same entity". Sits between
# fuzzy.CONFUSION_COST (0.25, a known glyph slip) and 1.0 (an ordinary
# substitution or an indel), so several confusions fit and one real difference
# does not. See the module docstring.
GROUP_BUDGET = 0.9

# What counts as identifier-shaped lives in `fuzzy` beside the tables it
# selects between (`fuzzy.identifier_shaped`), because the locator asks the
# same question when it matches a borrowed value and the two must not disagree.


@dataclass(frozen=True)
class GroupVariant:
    """One distinct surface form, as the model transcribed it.

    `text` is verbatim — it is what the borrowed pass searches for, so nothing
    normalized may ever replace it."""

    text: str
    entity_type: str  # elected among THIS form's own detections
    count: int  # individual detections of this exact string
    pages: tuple[int, ...]  # 1-based, ascending, distinct


@dataclass(frozen=True)
class EntityGroup:
    """One value as the whole document sees it.

    `votes` records the tally the class was elected from, most votes first. It
    exists for the report: the election can keep a value that some page
    reported as PII, so an operator has to be able to see 'ORGANIZATION 10 /
    PERSON 1' and disagree with it."""

    entity_type: str
    variants: tuple[GroupVariant, ...]
    votes: tuple[tuple[str, int], ...]

    @property
    def count(self) -> int:
        """Individual detections across every form and page."""
        return sum(v.count for v in self.variants)

    @property
    def pages(self) -> tuple[int, ...]:
        return tuple(sorted({p for v in self.variants for p in v.pages}))


class Grouping:
    """The document-wide view: every group, and lookups over their members."""

    def __init__(self, groups: Sequence[EntityGroup] = ()) -> None:
        self.groups = tuple(groups)
        # A surface form belongs to exactly one group by construction —
        # clustering runs over DISTINCT texts.
        self._by_text = {v.text: g for g in self.groups for v in g.variants}

    def type_for(self, text: str) -> str | None:
        """The elected class for a surface form, or None if it was never
        detected (a caller then keeps the finding's own class)."""
        group = self._by_text.get(text)
        return group.entity_type if group else None

    def needles(self) -> tuple[Needle, ...]:
        """Every constituent of every group as a `locator.Needle` carrying the
        group's elected type, longest first.

        Longest first because two needles can land on the same span — 'John'
        inside 'John Smith' — and the wider one must claim it, exactly as
        `locate_findings` orders its own placement. `locate_borrowed` sorts
        again (it takes needles from two sources now), so this ordering is
        what makes the result deterministic rather than what makes it correct.

        These are layer-0 needles: each is a value the model READ and we
        located somewhere in this document, so its extent is a transcription
        of something printed and every tier is admissible for it. Layer 1's
        needles are built elsewhere and are deliberately weaker — see
        `locator.Needle`.
        """
        pairs = [
            (v.text, g.entity_type) for g in self.groups for v in g.variants
        ]
        return tuple(
            Needle(text, entity_type)
            for text, entity_type in sorted(
                pairs, key=lambda p: (-len(p[0]), p[0])
            )
        )

    def __len__(self) -> int:
        return len(self.groups)


def group_findings(
    pages: Sequence[Sequence[VlmFinding]], first_page: int = 1
) -> Grouping:
    """Fold every page's layer-0 findings into document-wide groups.

    `pages` is one finding list per page, in page order. Pure and fast — a few
    hundred distinct values compared pairwise, against ~300 s/page of model
    time — so it runs between the two sweeps with no I/O of its own.
    """
    detections = [
        (first_page + offset, finding)
        for offset, page in enumerate(pages)
        for finding in page
    ]
    if not detections:
        return Grouping()

    types: dict[str, Counter] = {}
    seen_pages: dict[str, set[int]] = {}
    for page, finding in detections:
        types.setdefault(finding.text, Counter())[finding.entity_type] += 1
        seen_pages.setdefault(finding.text, set()).add(page)

    # Sorted so clustering, and therefore the whole result, is independent of
    # the order the model happened to report values in.
    texts = sorted(types)
    squashed = {t: squash_map(t)[0] for t in texts}
    shaped = {t: fuzzy.identifier_shaped(squashed[t]) for t in texts}

    groups = []
    for cluster in _cluster(texts, squashed, shaped):
        votes: Counter = Counter()
        for text in cluster:
            votes.update(types[text])
        variants = tuple(
            sorted(
                (
                    GroupVariant(
                        text=text,
                        entity_type=_elect(types[text]),
                        count=sum(types[text].values()),
                        pages=tuple(sorted(seen_pages[text])),
                    )
                    for text in cluster
                ),
                key=lambda v: (-v.count, -len(v.text), v.text),
            )
        )
        groups.append(
            EntityGroup(
                entity_type=_elect(votes),
                variants=variants,
                votes=tuple(
                    sorted(
                        votes.items(),
                        key=lambda kv: (-kv[1], _rank(kv[0]), kv[0]),
                    )
                ),
            )
        )
    groups.sort(key=lambda g: (-g.count, -len(g.variants[0].text),
                               g.variants[0].text))
    return Grouping(groups)


def _rank(entity_type: str) -> int:
    """Position in CLASS_PRIORITY; unknown types rank last."""
    try:
        return CLASS_PRIORITY.index(entity_type)
    except ValueError:
        return len(CLASS_PRIORITY)


def _elect(votes: Counter) -> str:
    """Majority vote over INDIVIDUAL detections, ties broken by class priority.

    Counting detections rather than distinct surface forms is deliberate: a
    value the model reads the same way on eight pages should outweigh one it
    read differently once. `sorted` first so an exotic tie — two unranked types
    with equal counts — still resolves deterministically.
    """
    return max(sorted(votes.items()), key=lambda kv: (kv[1], -_rank(kv[0])))[0]


def _cluster(
    texts: Sequence[str],
    squashed: dict[str, str],
    shaped: dict[str, bool],
) -> list[list[str]]:
    """Single-link clustering by `_related`, via union-find.

    Union-find rather than a leader/canopy pass so the result cannot depend on
    the order values are visited in — the same document must group the same way
    however the model ordered its answer.
    """
    parent = list(range(len(texts)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(texts)):
        for j in range(i + 1, len(texts)):
            root_i, root_j = find(i), find(j)
            if root_i == root_j:
                continue  # already joined — skip the distance entirely
            if _related(texts[i], texts[j], squashed, shaped):
                parent[max(root_i, root_j)] = min(root_i, root_j)

    clusters: dict[int, list[str]] = {}
    for i, text in enumerate(texts):
        clusters.setdefault(find(i), []).append(text)
    return [clusters[key] for key in sorted(clusters)]


def _related(
    a: str, b: str, squashed: dict[str, str], shaped: dict[str, bool]
) -> bool:
    """Whether two surface forms name the same entity.

    If EITHER side is identifier-shaped the strict table applies: the
    permissive one must not be reachable by damaging one of the two strings
    until it stops looking like an identifier.
    """
    sq_a, sq_b = squashed[a], squashed[b]
    if not sq_a or not sq_b:
        return sq_a == sq_b
    if sq_a == sq_b:
        return True
    costs = (
        fuzzy.IDENTIFIER_COSTS
        if shaped[a] or shaped[b]
        else fuzzy.CONFUSION_COSTS
    )
    return fuzzy.distance(sq_a, sq_b, ceiling=GROUP_BUDGET, costs=costs) <= (
        GROUP_BUDGET
    )

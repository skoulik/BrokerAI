"""Layer 1, pass 2 — rules that read DETECTIONS rather than text.

Pass 1 (`recognizers.py`, run by `engine.py`) matches patterns against the raw
string and knows nothing about what any other layer found. Pass 2 runs after
it, over the union of everything detected so far, and derives what only that
union makes visible.

**A pass-2 rule consumes person/organization/… detections, never "layer 0's
output"** (Sergei, 2026-08-14). That distinction is the whole point of the
split: layer 1 may itself grow a PERSON source later — an NER recognizer, an
allow/deny list — and such a source must feed these rules with no rewiring.
A rule that reached for the VLM's findings specifically would have to be
rewritten the day that happens.

The first rule is `JointNames`. Others slot in beside it by implementing
`DerivedRule`; `apply` is the only thing the pipeline calls.

Two kinds of output, and the protocol keeps them apart because they are
handled differently downstream (`PiiPipeline.merge_detections`): spans that
already existed and were merely RE-TYPED (the keep list has already been
applied to them) and spans that are NEW (it has not).
"""

from __future__ import annotations

import dataclasses
from typing import Iterable, Protocol, Sequence

import regex

from pii.core.detection import Detection

# The joint form names two people at once, so it is neither of them: emitting
# PERSON would put a third identity in the map for two humans, with nothing
# marking it as the compound of the other two (Sergei, 2026-08-14).
JOINT_ENTITY = "PERSON_JOINT"
PERSON_ENTITY = "PERSON"

# A joint span outranks a bare-surname span covering part of it, so the merged
# label is the more specific description of that text. Score, not a `_rank`
# tier: both are ordinary specific classes and ties there resolve arbitrarily.
JOINT_SCORE = 1.0
DERIVED_PERSON_SCORE = 0.9

# 'and' needs word boundaries; '&' does not, and appears unspaced in the wild
# ('E&J Moore'). Kept as one alternation so every form is split identically.
_CONNECTOR = r"(?:&|\band\b)"

# A name word: capitalised or ALL CAPS, 2+ characters, allowing O'Brien,
# Smith-Jones and McDonald. Deliberately NOT used to decide whether something
# IS a name — a detector already said so — only to parse a value we were handed.
_WORD = r"[\p{Lu}][\p{L}'’-]+"
_INITIAL = r"\p{Lu}\.?"


class DerivedRule(Protocol):
    def apply(
        self, spans: Sequence[Detection], text: str
    ) -> tuple[list[Detection], list[Detection]]:
        """(re-typed spans, new spans). `spans` is returned whole, so a rule
        that re-types nothing still passes every input through."""


def apply(
    spans: Sequence[Detection], text: str, rules: Iterable[DerivedRule] = ()
) -> tuple[list[Detection], list[Detection]]:
    """Run pass 2. Returns (all spans, of which these are new)."""
    added: list[Detection] = []
    current = list(spans)
    for rule in rules or DEFAULT_RULES:
        current, new = rule.apply(current, text)
        added.extend(new)
    return current, added


def _words(value: str) -> list[str]:
    return value.split()


def _common_surname(a: Sequence[str], b: Sequence[str]) -> list[str]:
    """The longest common TRAILING word sequence of two names, case-folded.

    Sergei's rule is "the surname must be in both A and B". Read as "any shared
    word" it breaks on 'John Smith' + 'John Brown', which share 'John' and would
    have us hunt for 'S & B John'. A shared suffix is the same answer on every
    real case and admits multi-word surnames ('van Berg') for free.
    """
    out: list[str] = []
    for x, y in zip(reversed(a), reversed(b)):
        if x.casefold() != y.casefold():
            break
        out.append(x)
    return list(reversed(out))


def _initials(name_words: Sequence[str], surname: Sequence[str]) -> set[str]:
    """First letters of the words of a name that are NOT its surname.

    'any word of A that is not a surname' — so 'Emily Jane Moore' offers both
    E and J, which is deliberate: a joint form may use either given name.
    """
    given = name_words[: len(name_words) - len(surname)]
    return {w[0].upper() for w in given if w and w[0].isalpha()}


@dataclasses.dataclass(frozen=True)
class JointParse:
    """What a joint-shaped value decomposes into."""

    surname: tuple[str, ...]
    # Full constituent names, when the form carries them. An INITIALS form
    # carries none: 'E Moore' is not a name, an initial names nobody, and
    # pairing two of them back would only re-derive the form we started from.
    people: tuple[str, ...] = ()


def parse_joint(value: str) -> JointParse | None:
    """Parse a value a detector already called a PERSON into its joint form.

    This is NOT the retired issue-#4 problem. That was *detecting* 'Julie and
    Brian Summers' in raw prose, which no lexical rule can separate from
    ordinary text. Here a detector has already drawn the span and called it a
    person; we are parsing a value we were handed, and a mistake costs a
    placeholder label rather than a leak.

    Three shapes, plus their reverse orders:

        E & J Moore                  initials + shared surname   -> no people
        Emily and John Moore         given names + shared surname
        Emily Moore and John Moore   both fully qualified
    """
    parts = regex.split(rf"\s*{_CONNECTOR}\s*", value.strip(), flags=regex.I)
    if len(parts) != 2:
        return None
    left, right = (_words(p) for p in parts)
    if not left or len(right) < 2:
        return None

    def initial_only(word: str) -> bool:
        return bool(regex.fullmatch(_INITIAL, word))

    if len(left) == 1 and initial_only(left[0]):
        # 'E & J Moore' — the right side must be an initial plus the surname.
        if not initial_only(right[0]):
            return None
        return JointParse(surname=tuple(right[1:]))

    if len(left) == 1:
        # 'Emily and John Moore' — one given name, then given name + surname.
        if not regex.fullmatch(_WORD, left[0]):
            return None
        surname = right[1:]
        return JointParse(
            surname=tuple(surname),
            people=(
                " ".join([left[0], *surname]),
                " ".join(right),
            ),
        )

    # 'Emily Moore and John Moore' — both sides complete, surnames must agree,
    # or the two are unrelated people rather than a joint account name.
    surname = _common_surname(left, right)
    if not surname:
        return None
    return JointParse(
        surname=tuple(surname),
        people=(" ".join(left), " ".join(right)),
    )


class JointNames:
    """Joint-account names, derived from people who are already known.

    Replaces the standalone `JointNameRule` pattern (deleted 2026-08-14).
    That rule tried to recognize 'E & J Moore' from its shape alone, which
    cannot be done without knowing who the people are: it fired on every
    two-initial brand followed by a capitalised word — 'P&O Cruises',
    'H&M Stores', 'R&D Team' all stripped as PERSON — and its four guards
    (single-letter sides, case sensitivity, a corporate-word list, a corporate
    tail lookahead) were all attempts to buy back precision it never had.

    Evidence replaces every one of them. A span matches only if two DETECTED
    people share its surname and own its initials, so 'P&O Cruises' is
    unreachable unless someone named Cruises was detected, and 'E & J HOLDINGS'
    needs two people surnamed Holdings. No word list, no case trickery.

    Three steps, in order, because each feeds the next:

    1. CLASSIFY - a person whose value is itself a joint form is re-typed.
    2. DECOMPOSE - that value's constituents join the pool of known people;
       an initials form contributes its SURNAME as a person instead (Sergei,
       2026-08-14: a bare 'MOORE' in a transaction line is PII, and nothing
       else in the stack would catch it).
    3. DERIVE - every ordered pair of known people is searched for as an
       initials form. Ordered, because 'E & J Moore' and 'J & E Moore' name
       the same couple in different positions and only one can be right.
    """

    name = "JointNames"

    def apply(
        self, spans: Sequence[Detection], text: str
    ) -> tuple[list[Detection], list[Detection]]:
        people: set[str] = set()
        surnames: set[tuple[str, ...]] = set()
        out: list[Detection] = []
        for span in spans:
            if span.entity_type != PERSON_ENTITY:
                out.append(span)
                continue
            value = span.full_value or text[span.start : span.end]
            parsed = parse_joint(value)
            if parsed is None:
                people.add(value)
                out.append(span)
                continue
            people.update(parsed.people)
            # EVERY joint form contributes its surname, not just the initials
            # one. A joint account name proves the surname belongs to a person
            # either way, and keying it to the form would mean 'E & J MOORE'
            # gave better bare-surname recall than 'Emily and John Moore' —
            # backwards, since the second tells us strictly more.
            surnames.add(parsed.surname)
            out.append(dataclasses.replace(span, entity_type=JOINT_ENTITY))

        joint = self._derive_joint(people, text)
        # Surname occurrences are looked for OUTSIDE every joint span, derived
        # ones included: that span already covers them and carries the more
        # specific label.
        taken = [(s.start, s.end) for s in (*out, *joint)]
        return out, [*joint, *self._derive_surnames(surnames, text, taken)]

    def _derive_joint(
        self, people: set[str], text: str
    ) -> list[Detection]:
        """Search the text for the initials form of every ordered pair."""
        parsed = {p: _words(p) for p in people}
        found: list[Detection] = []
        seen: set[tuple[int, int]] = set()
        for a, aw in parsed.items():
            for b, bw in parsed.items():
                if a == b:
                    continue
                surname = _common_surname(aw, bw)
                if not surname:
                    continue
                for i1 in _initials(aw, surname):
                    for i2 in _initials(bw, surname):
                        for start, end in self._search(
                            text, i1, i2, surname
                        ):
                            if (start, end) in seen:
                                continue
                            seen.add((start, end))
                            found.append(
                                Detection(
                                    entity_type=JOINT_ENTITY,
                                    start=start,
                                    end=end,
                                    score=JOINT_SCORE,
                                    recognizer=self.name,
                                )
                            )
        return found

    def _search(
        self, text: str, i1: str, i2: str, surname: Sequence[str]
    ) -> list[tuple[int, int]]:
        tail = r"\s+".join(regex.escape(w) for w in surname)
        pattern = (
            rf"\b{regex.escape(i1)}\.?\s*{_CONNECTOR}\s*"
            rf"{regex.escape(i2)}\.?\s+{tail}\b"
        )
        return [
            m.span()
            for m in regex.finditer(pattern, text, regex.I | regex.MULTILINE)
        ]

    def _derive_surnames(
        self,
        surnames: set[tuple[str, ...]],
        text: str,
        taken: Sequence[tuple[int, int]],
    ) -> list[Detection]:
        """Every occurrence of a surname learned from an initials form.

        The recall this exists for: 'E & J MOORE' tells us MOORE is a person's
        surname, so the bare 'MOORE' in another transaction line strips too.
        Occurrences inside a joint span are skipped - that span already covers
        them and carries the better label.

        Accepted cost (Sergei, 2026-08-14): a surname that is also document
        vocabulary strips every occurrence of the word. Over-strip, not a leak,
        and the eval's over-strip axis is where it shows.
        """
        found: list[Detection] = []
        for surname in surnames:
            tail = r"\s+".join(regex.escape(w) for w in surname)
            for m in regex.finditer(rf"\b{tail}\b", text, regex.I):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                found.append(
                    Detection(
                        entity_type=PERSON_ENTITY,
                        start=m.start(),
                        end=m.end(),
                        score=DERIVED_PERSON_SCORE,
                        recognizer=self.name,
                    )
                )
        return found


DEFAULT_RULES: tuple[DerivedRule, ...] = (JointNames(),)

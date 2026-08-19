"""Layer 1, pass 2 — rules that read DETECTIONS rather than text.

Pass 1 (`recognizers.py`, run by `engine.py`) matches patterns against the raw
string and knows nothing about what any other layer found. Pass 2 runs after
it, over the union of everything detected so far, and derives what only that
union makes visible.

**That union is the whole DOCUMENT, not one page** (Sergei, 2026-08-19). A
pass-2 rule learns from `KnownValues` — every value either layer detected
anywhere, complete before the first page is redacted — and applies what it
learnt to one page at a time. The two halves are separable because they are
different kinds of thing: *which values name people* is a property of the
values, while *where that name is printed* is a property of a page.

Until 2026-08-19 both halves were per page, so `JointNames` could only derive
`E & J MOORE` on a page that ALSO carried Emily and John Moore: the pool was
rebuilt from one page's spans every time. That is the defect
`image_mode.layer1_needles` was created for on 2026-08-18 — "a floor that holds
per-occurrence is not a floor" — left behind by that change because the
architecture note of the day said sweep 2 was "the only place `derived.py` can
run". It is not: sweep 1 holds both layers' spans per page, and the union of
every page is strictly more than any one of them.

A caller with one page and no document — `strip_text`, `strip_image`, the
testbench — passes no `KnownValues` at all, and every rule falls back to the
spans it was handed. For those callers the page IS the document, so the two
regimes coincide and nothing about their behaviour changed.

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

# `<company> ATF <trust>` names two organizations at once, so it is neither of
# them — the PERSON_JOINT argument, in the other class. Its own type rather
# than ORGANIZATION for a second, mechanical reason too: the parties are
# SUBSTRINGS of the compound, so all three cannot be spans on the same line
# (`_merge_overlaps` unions them straight back). The compound keeps the line it
# is printed on and the parties are searched for everywhere else, which is
# exactly how `JointNames` treats a joint span and its surnames.
TRUSTEE_ENTITY = "ORGANIZATION_TRUSTEE"
ORGANIZATION_ENTITY = "ORGANIZATION"

# A joint span outranks a bare-surname span covering part of it, so the merged
# label is the more specific description of that text. Score, not a `_rank`
# tier: both are ordinary specific classes and ties there resolve arbitrarily.
JOINT_SCORE = 1.0
DERIVED_PERSON_SCORE = 0.9
# The same pair, for the trustee compound and the parties derived from it, and
# for the same reason: should a party ever be found overlapping a compound the
# compound must label the merged span, because it is the more specific
# description of that text.
TRUSTEE_SCORE = 1.0
DERIVED_ORG_SCORE = 0.9

# 'and' needs word boundaries; '&' does not, and appears unspaced in the wild
# ('E&J Moore'). Kept as one alternation so every form is split identically.
_CONNECTOR = r"(?:&|\band\b)"

# A name word: capitalised or ALL CAPS, 2+ characters, allowing O'Brien,
# Smith-Jones and McDonald. Deliberately NOT used to decide whether something
# IS a name — a detector already said so — only to parse a value we were handed.
_WORD = r"[\p{Lu}][\p{L}'’-]+"
_INITIAL = r"\p{Lu}\.?"


@dataclasses.dataclass(frozen=True)
class KnownValues:
    """Every value either layer detected ANYWHERE in the document.

    Type-and-value pairs, with no offsets and no page numbers, because an
    offset means nothing on another page and pass 2 only ever needs to know
    *what* was found — a rule that wanted to know where would be reading
    geometry, which is not this layer's business.

    The pairs are already through the keep list (`PiiPipeline.strips_value`),
    so a kept merchant name cannot seed a derivation. That mirrors
    `merge_detections`, which puts layer-0 spans through the keep list before
    calling pass 2, and it matters here for the same reason: a derived value
    inherits the evidence of the value it came from, so admitting a kept one
    would launder it back into the strip plan under a new name.

    Empty is the honest default: it means "no document-wide view was supplied",
    and every rule then falls back to the page it was handed.
    """

    pairs: frozenset[tuple[str, str]] = frozenset()

    @classmethod
    def of(cls, pairs: Iterable[tuple[str, str]]) -> "KnownValues":
        return cls(frozenset(pairs))

    def of_type(self, entity_type: str) -> frozenset[str]:
        return frozenset(v for t, v in self.pairs if t == entity_type)

    def __bool__(self) -> bool:
        return bool(self.pairs)


class DerivedRule(Protocol):
    def apply(
        self, spans: Sequence[Detection], text: str, known: KnownValues
    ) -> tuple[list[Detection], list[Detection]]:
        """(re-typed spans, new spans). `spans` is returned whole, so a rule
        that re-types nothing still passes every input through.

        `spans` and `text` are ONE PAGE; `known` is the whole document. A rule
        learns from `known` and this page's own spans together — the page is
        part of the document, and unioning the two is what makes an empty
        `known` reproduce the pre-2026-08-19 single-page behaviour exactly."""


def apply(
    spans: Sequence[Detection],
    text: str,
    known: KnownValues = KnownValues(),
    rules: Iterable[DerivedRule] = (),
) -> tuple[list[Detection], list[Detection]]:
    """Run pass 2. Returns (all spans, of which these are new)."""
    added: list[Detection] = []
    current = list(spans)
    for rule in rules or DEFAULT_RULES:
        current, new = rule.apply(current, text, known)
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

    Steps 1 and 2 read `KnownValues` — the whole document — before they read
    this page, so the pool is complete however the pages are ordered. Step 3
    searches THIS page's text, because that is the only place an offset means
    anything. A document-wide person who is printed nowhere on this page
    simply matches nothing here, which costs one regex sweep.
    """

    name = "JointNames"

    def apply(
        self, spans: Sequence[Detection], text: str, known: KnownValues
    ) -> tuple[list[Detection], list[Detection]]:
        people: set[str] = set()
        surnames: set[tuple[str, ...]] = set()
        # LEARN from the whole document first. A person named on page 1 is a
        # person on page 4, so the pool a joint form is derived against is the
        # document's, not this page's — otherwise `E & J MOORE` on the
        # transaction page derives nothing unless Emily and John happen to be
        # named there too, which is the per-occurrence defect pass 2 carried
        # until 2026-08-19. Values only, no offsets: where each one was printed
        # is this page's business, below.
        for value in known.of_type(PERSON_ENTITY):
            parsed = parse_joint(value)
            if parsed is None:
                people.add(value)
                continue
            people.update(parsed.people)
            surnames.add(parsed.surname)
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


# The trustee clause's connector, in the spellings `AtfTailRule` knows. One
# vocabulary, two rules: layer 1 matches the clause from the connector and gets
# the TRUST half only (the company before it has no shape and no left edge),
# while this rule decomposes a value layer 0 read whole and gets BOTH halves.
# A spelling one accepts and the other does not is a gap waiting to be found.
_ATF_CONNECTOR = regex.compile(r"\s+(?:atf|as\s+trustees?\s+for)\s+", regex.I)


@dataclasses.dataclass(frozen=True)
class AtfParse:
    """The two organizations a trustee clause names."""

    trustee: str
    trust: str


def parse_atf(value: str) -> AtfParse | None:
    """Split `<company> ATF <trust>`, or None if the value is not one.

    Like `parse_joint`, this answers *what form is this value*, never *is this
    an organization* — a detector already decided that. So it is only ever
    called on something already detected, and prose containing the word `atf`
    is not reachable from here.

    Both sides must be non-empty: a value that merely STARTS with the connector
    is the trust alone with a stray label on it, which is `AtfTailRule`'s job
    and not a compound. The first connector wins — a clause with two is not a
    form anyone prints, and picking the first keeps the trustee whole.
    """
    match = _ATF_CONNECTOR.search(value)
    if match is None:
        return None
    trustee, trust = value[: match.start()].strip(), value[match.end():].strip()
    if not trustee or not trust:
        return None
    return AtfParse(trustee=trustee, trust=trust)


class AtfParties:
    """`<company> ATF <trust>` decomposed into the two organizations it names.

    The sibling of `JointNames` in every structural respect, and deliberately
    so — a value that names two entities at once is neither of them, and the
    parties are worth knowing separately because each is an entity in its own
    right that the document may mention alone.

    **Layer 0 is the source that carries both halves.** `AtfTailRule` matches
    the clause from its connector, so it can only ever see the trust: a company
    name has no shape and no left edge, and the line may carry other fields
    before it. A model reading the page reports the construction whole, and
    that is the one place the trustee company is recoverable.

    Three steps, in the order Sergei called (2026-08-19) — the compound is
    matched FIRST, and the parties are then searched for outside it:

    1. CLASSIFY - an organization whose value is itself a trustee clause is
       re-typed to ORGANIZATION_TRUSTEE.
    2. DECOMPOSE - that value's two parties join the pool of known
       organizations. Document-wide, so a clause on page 1 lets a bare mention
       of either party strip on page 4.
    3. DERIVE - every known party is searched for on this page, OUTSIDE every
       compound span. Inside one it is already covered, by the span carrying
       the more specific label.

    A party must carry a word character. It is searched as a literal with word
    boundaries, so a punctuation-only fragment would have no boundaries to
    anchor on and is the one input that could match unpredictably — the same
    hazard as the punctuation-only OCR word in `locator`.
    """

    name = "AtfParties"

    def apply(
        self, spans: Sequence[Detection], text: str, known: KnownValues
    ) -> tuple[list[Detection], list[Detection]]:
        parties: set[str] = set()
        for value in (
            known.of_type(ORGANIZATION_ENTITY) | known.of_type(TRUSTEE_ENTITY)
        ):
            parsed = parse_atf(value)
            if parsed is not None:
                parties.update((parsed.trustee, parsed.trust))
        out: list[Detection] = []
        for span in spans:
            if span.entity_type != ORGANIZATION_ENTITY:
                out.append(span)
                continue
            value = span.full_value or text[span.start : span.end]
            parsed = parse_atf(value)
            if parsed is None:
                out.append(span)
                continue
            parties.update((parsed.trustee, parsed.trust))
            out.append(
                dataclasses.replace(
                    span, entity_type=TRUSTEE_ENTITY, score=TRUSTEE_SCORE
                )
            )
        taken = [(s.start, s.end) for s in out]
        return out, self._derive_parties(parties, text, taken)

    def _derive_parties(
        self,
        parties: set[str],
        text: str,
        taken: Sequence[tuple[int, int]],
    ) -> list[Detection]:
        found: list[Detection] = []
        seen: set[tuple[int, int]] = set()
        for party in parties:
            if not regex.search(r"\w", party):
                continue
            pattern = r"\s+".join(
                regex.escape(w) for w in party.split()
            )
            for m in regex.finditer(rf"\b{pattern}\b", text, regex.I):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                if (m.start(), m.end()) in seen:
                    continue
                seen.add((m.start(), m.end()))
                found.append(
                    Detection(
                        entity_type=ORGANIZATION_ENTITY,
                        start=m.start(),
                        end=m.end(),
                        score=DERIVED_ORG_SCORE,
                        recognizer=self.name,
                    )
                )
        return found


class ReversedNames:
    """`John Smith` in the header, `SMITH JOHN` in a fixed-width name field.

    Surname-first is how a statement's own name column prints a person, and the
    two forms are unreachable from each other by every mechanism the document
    already has: exact and squash matching see a different string, the borrowed
    fuzzy tier prices a word swap far above any budget, and grouping's distance
    runs on the separator-collapsed form where `johnsmith` and `smithjohn` are
    most of a string apart. Nothing but knowing that a name can be printed
    either way round reaches it.

    So the reversal is HYPOTHESISED and then searched for, exactly as
    `JointNames` hypothesises an initials form from two known people. That is
    the established shape for this: derive a plausible printed form from a
    value some layer read, and look for it. It is not a normalized form — the
    thing `grouping` forbids as a needle — because a normalization corresponds
    to nothing printed anywhere, while `Smith John` is what the name column
    actually contains.

    **Two words, and neither an initial** (Sergei, 2026-08-19). Two words bounds
    the permutation space to one candidate and matches the form that is really
    printed; excluding initials drops `J Smith` -> `Smith J`, which is both an
    implausible printing and the largest false-match surface in the family.

    Emitted as PERSON, not a form of its own: a reversed name names ONE person,
    which is what separates it from a joint form. Each surface form still takes
    its own placeholder, as every class does — the map keys on the value so that
    rehydration restores what the document had.
    """

    name = "ReversedNames"

    def apply(
        self, spans: Sequence[Detection], text: str, known: KnownValues
    ) -> tuple[list[Detection], list[Detection]]:
        people = set(known.of_type(PERSON_ENTITY))
        for span in spans:
            if span.entity_type == PERSON_ENTITY:
                people.add(span.full_value or text[span.start : span.end])
        taken = [(s.start, s.end) for s in spans]
        return list(spans), self._derive_reversed(people, text, taken)

    def _derive_reversed(
        self,
        people: set[str],
        text: str,
        taken: Sequence[tuple[int, int]],
    ) -> list[Detection]:
        found: list[Detection] = []
        seen: set[tuple[int, int]] = set()
        for person in people:
            words = _words(person)
            if len(words) != 2:
                continue
            if any(regex.fullmatch(_INITIAL, w) for w in words):
                continue
            reversed_form = f"{words[1]} {words[0]}"
            if reversed_form.casefold() == person.casefold():
                continue
            pattern = r"\s+".join(regex.escape(w) for w in reversed(words))
            for m in regex.finditer(rf"\b{pattern}\b", text, regex.I):
                if any(m.start() < e and s < m.end() for s, e in taken):
                    continue
                if (m.start(), m.end()) in seen:
                    continue
                seen.add((m.start(), m.end()))
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


DEFAULT_RULES: tuple[DerivedRule, ...] = (
    JointNames(), AtfParties(), ReversedNames(),
)

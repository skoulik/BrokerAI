"""The detection engine — rules, scoring, and the context boost.

Replaces Presidio's `AnalyzerEngine` / `RecognizerRegistry` / `PatternRecognizer`
and its `LemmaContextAwareEnhancer` (2026-08-09). Small on purpose: what we used
of Presidio was a regex loop, a validation hook, a context boost and a
threshold, and owning those removes both presidio and spaCy along with the
checksum-duplication hazard they forced (see `recognizers.py`).

Three pieces of Presidio's scoring semantics are reproduced exactly, because
every score in `recognizers.py` was tuned against them:

- **Validation overrides the pattern score.** `validate()` returning True sets
  the score to 1.0, False drops the match entirely, and None leaves the
  pattern's own score standing. That three-way return is why a recall-first
  pattern can score 0.15 and still reach 1.0 on a passing checksum, and why the
  invalid shadows can return None to *keep* their deliberately low score.
- **The context boost is +0.35, floored to 0.4 and capped at 1.0.** The
  sub-threshold patterns (bare account numbers, PayID digit runs, the `context`
  invalid tier) exist only because this promotion exists; changing the constants
  silently re-tunes all of them.
- **Duplicate spans collapse**, keeping the highest score.

**The context match is char-level, and that is an improvement, not a
shortcut.** Presidio matched lemmas from spaCy's tokenization, which the
2026-07-15 source review found actively broken on this text: `a/c` fragments
into three tokens while `TFN:123456782` stays one, so the label word never
surfaced as a token either way, and the rule lemmatizer left HEADER-CASE words
unlemmatized on top. The review's conclusion was "keep label/context matching
char-level" — `AuAccountNumberRecognizer` already worked around the gap by
matching `a/c` inside its own pattern. Searching characters case-insensitively
for the label as a substring is what that review asked for and needs no NLP
engine at all, and it is unchanged.

**What changed (2026-08-14) is where those characters come from.** They used to
be a flat 60 back in the assembled string, which models nothing: not a field,
not a line, not a column, and not whether the label already introduces some
other value. A `Layout` now supplies the candidate's neighbourhoods instead —
the words that sit beside and above it, in reading order, ending where the
candidate begins — so "the label is near the value" means near ON THE PAGE.
The engine holds only the protocol; `pii.core.layout` has the pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import regex

from pii.core.detection import Attachment, Detection
from pii.core.labels import Exact, gap_is_clean

# Presidio's LemmaContextAwareEnhancer constants, preserved exactly.
CONTEXT_BOOST = 0.35
CONTEXT_FLOOR = 0.4
MAX_SCORE = 1.0

# How far back a context word may sit under the RETIRING `WindowLayout`.
# Presidio looked back five *tokens*; this is the char-level equivalent, sized
# for five or six words of statement text including a label's punctuation
# ("Acct No.: ", "BSB / Account "). It models nothing else — see `Layout`.
CONTEXT_WINDOW_CHARS = 60

# Attachment strengths (per PATTERN, not per rule — several rules have both).
#
# NEAR   — a label sits in one of the candidate's neighbourhoods. The promotion
#          the sub-threshold patterns have always relied on, and it BOOSTS.
# STRICT — additionally, everything between that label and the value is
#          separators and fillers (`labels.gap_is_clean`). This is the gate the
#          labelled patterns used to spell as a regex lookbehind, so it behaves
#          like one: a pattern carrying it is DROPPED when unattached, and its
#          declared score stands unboosted when attached (the label is the
#          evidence that score already prices in). Together those two make
#          converting a lookbehind into a STRICT pattern score-neutral.
NEAR = "near"
STRICT = "strict"
STRENGTHS = (NEAR, STRICT)

# Which `Layout` a run uses. TRANSITIONAL: `window` is the retiring character
# lookback, kept only so one corpus can be scored both ways while the geometric
# one is measured, and deleted with `WindowLayout` when the default flips.
ATTACH_WINDOW = "window"
ATTACH_LAYOUT = "layout"
ATTACH_MODES = (ATTACH_WINDOW, ATTACH_LAYOUT)

# How many words beyond a rule's longest label the left band must reach back.
# Covers the fillers between label and value ("No.", "#") plus one, so a band
# sized from the label itself always has room for the form's own punctuation.
FILLER_ALLOWANCE = 2

# `regex`, not `re`: the account-after-BSB patterns use variable-length
# lookbehind, which the stdlib engine cannot compile.
DEFAULT_FLAGS = regex.MULTILINE | regex.DOTALL | regex.IGNORECASE


@dataclass(frozen=True)
class Pattern:
    """One named regex, the score a bare match of it carries, and how firmly a
    label must be attached for the match to count (`NEAR` / `STRICT`)."""

    name: str
    regex: str
    score: float
    attach: str = NEAR


class Rule:
    """A detector. Subclasses implement `detect`; the engine handles label
    attachment, the score boost, thresholding and deduplication around them."""

    name: str = ""
    entity: str = ""
    # The rule's LABEL spellings — the words that name this class in print.
    # Still called `context` because that is what every rule and test calls it;
    # what changed in 2026-08-14 is only how a label is judged to reach a value.
    context: tuple[str, ...] = ()

    @property
    def entities(self) -> tuple[str, ...]:
        """Every entity type this rule can emit. Declared rather than
        inferred so registry-composition tests can assert what layer 1 is
        allowed to claim without running it."""
        return (self.entity,) if self.entity else ()

    @property
    def label_words(self) -> int:
        """How far back, in words, a left band must reach for THIS rule.

        Derived from the rule's own longest label rather than set globally: a
        value right-aligned against `Australian Financial Services Licence` at
        the left margin needs a band four words deep, while `tfn` needs one,
        and a global constant would have to be the larger of the two for
        everybody (Sergei, 2026-08-14: "how much is needed by the regexp").
        """
        longest = max((len(term.split()) for term in self.context), default=1)
        return longest + FILLER_ALLOWANCE

    @property
    def label_pattern(self):
        """This rule's label spellings, compiled.

        Matching stays the char-level substring match the 2026-07-15 review
        asked for, with ONE addition: a label must begin at a word boundary.
        Without it a two-letter spelling like `ac` matches inside `back`, and
        the vocabulary cannot hold the short forms real statements print. The
        END is deliberately left open, so a spelling may be a stem —
        `account` matches `Accounts`, `enquir` matches `Enquiries` — which is
        what keeps the lists short enough to read.
        """
        compiled = getattr(self, "_label_pattern", None)
        if compiled is None:
            terms = sorted(self.context, key=len, reverse=True)
            # Words of a multi-word label are joined by `\s+`, not by the
            # literal space it was declared with: OCR prints `AFS  Licence`
            # with two, and a label that only matches one spelling of its own
            # whitespace is the 2026-08-12 separator bug in a new place.
            spellings = [
                r"\s+".join(regex.escape(word) for word in term.split())
                + (r"(?![0-9a-z])" if isinstance(term, Exact) else "")
                for term in terms
            ]
            compiled = regex.compile(
                r"(?<![0-9a-z])(?:%s)" % "|".join(spellings), regex.IGNORECASE
            )
            self._label_pattern = compiled
        return compiled

    def strength(self, pattern_name: str) -> str:
        """How firmly the named pattern's matches must be attached."""
        return NEAR

    def detect(self, text: str) -> list[Detection]:
        raise NotImplementedError


class PatternRule(Rule):
    """Regex rule with an optional validation hook — the shape of nearly
    every layer-1 detector.

    Subclasses set `entity`, `patterns` and optionally `context`, and may
    override `validate` (see the module docstring for the three-way return) or
    `emit` when one match can produce different entity types (the checksum
    rules in `recognizers.py` do exactly that).
    """

    patterns: tuple[Pattern, ...] = ()
    flags: int = DEFAULT_FLAGS

    def __init__(self) -> None:
        if not self.name:
            self.name = type(self).__name__
        for p in self.patterns:
            if p.attach not in STRENGTHS:
                raise ValueError(f"{self.name}.{p.name}: attach={p.attach!r}")
            if p.attach == STRICT and not self.context:
                # A gate with nothing to open it: the pattern could never fire.
                raise ValueError(
                    f"{self.name}.{p.name} is STRICT but the rule declares no labels"
                )
        self._strength = {p.name: p.attach for p in self.patterns}
        self._compiled = [
            (p, regex.compile(p.regex, self.flags)) for p in self.patterns
        ]

    def strength(self, pattern_name: str) -> str:
        return self._strength.get(pattern_name, NEAR)

    def validate(self, matched: str) -> bool | None:
        """True -> score 1.0, False -> drop, None -> keep the pattern score."""
        return None

    def emit(self, matched: str, pattern: Pattern) -> tuple[str, float] | None:
        """(entity_type, score) for one match, or None to drop it.

        The default applies `validate` to this rule's single entity. Override
        to let one pattern set produce several classes.
        """
        verdict = self.validate(matched)
        if verdict is False:
            return None
        return self.entity, (MAX_SCORE if verdict is True else pattern.score)

    def detect(self, text: str) -> list[Detection]:
        out = []
        for pattern, compiled in self._compiled:
            for match in compiled.finditer(text):
                matched = match.group()
                if not matched:
                    continue
                emitted = self.emit(matched, pattern)
                if emitted is None:
                    continue
                entity, score = emitted
                start, end = match.span()
                out.append(
                    Detection(
                        entity_type=entity,
                        start=start,
                        end=end,
                        score=score,
                        recognizer=self.name,
                        pattern=pattern.name,
                    )
                )
        return out


@dataclass(frozen=True)
class ContextWord:
    """One word of a neighbourhood: its text, where it came from in the
    analyzed string, and where it landed in the assembled neighbourhood."""

    text: str
    start: int
    end: int
    at: int


@dataclass(frozen=True)
class Context:
    """One neighbourhood of a candidate — the words near it, in reading order,
    **ending where the candidate begins**.

    That the string ends at the candidate is what makes the strict test a
    suffix test: whatever follows a label inside `text` is exactly what sits
    between that label and the value.

    A neighbourhood assembled from scattered words carries `words`, so a label
    matched inside `text` can be mapped back to its own span. One cut straight
    out of the analyzed string carries `origin` instead and needs no map.
    """

    relation: str
    text: str
    origin: int | None = None
    words: tuple[ContextWord, ...] = ()

    def source_span(self, lo: int, hi: int) -> tuple[int, int] | None:
        """Where `text[lo:hi]` came from in the analyzed string."""
        if self.origin is not None:
            return self.origin + lo, self.origin + hi
        touched = [w for w in self.words if w.at < hi and lo < w.at + len(w.text)]
        if not touched:
            return None
        return touched[0].start, touched[-1].end


class Layout(Protocol):
    """Supplies a candidate's neighbourhoods — the only thing the engine knows
    about where text sits.

    Structural, not inherited, so the engine gains no dependency on OCR: the
    geometric implementation lives in `pii.core.layout` and is handed in by the
    caller that has a page. The three implementations differ ONLY in which
    words count as near, which is the whole of the 2026-08-14 change.
    """

    def contexts(self, start: int, end: int, word_floor: int) -> list[Context]:
        ...


class WindowLayout:
    """RETIRING: the flat 60-character lookback.

    Kept only so one corpus can be scored both ways while the geometric layout
    is measured; it goes when the default flips. It models nothing — it cannot
    tell a label from another field's label, a line from the next, or a column
    from its neighbour, which is exactly how a mail-house reference came to be
    typed as a bank account (2026-08-14, record in DONE.md).
    """

    relation = "window"

    def __init__(self, text: str) -> None:
        self.text = text

    def contexts(self, start: int, end: int, word_floor: int) -> list[Context]:
        lo = max(0, start - CONTEXT_WINDOW_CHARS)
        return [Context(self.relation, self.text[lo:start], origin=lo)]


class TextLayout:
    """Left-only proximity for input with no geometry: plain text and CSV
    cells (Sergei, 2026-08-14 — "let's only [use] left proximity for text now").

    The band is the last `words` words before the candidate ON ITS OWN LINE. A
    line break ends it, which alone fixes the cross-line half of the specimen
    page; the rest is the word floor, which is what reaches a label that sits at
    the left margin with its value right-aligned across the line.
    """

    relation = "left"

    def __init__(self, text: str) -> None:
        self.text = text

    def contexts(self, start: int, end: int, word_floor: int) -> list[Context]:
        line_start = self.text.rfind("\n", 0, start) + 1
        prefix = self.text[line_start:start]
        cut = _back_off_words(prefix, word_floor)
        return [Context(self.relation, prefix[cut:], origin=line_start + cut)]


def _back_off_words(prefix: str, word_floor: int) -> int:
    """Offset in `prefix` where the last `word_floor` words begin.

    Counts word STARTS while scanning right to left — a non-space whose left
    neighbour is a space. Counting the first non-space of each run instead
    lands mid-word and hands the caller a fragment ("l us on" for "call us on").
    """
    if word_floor <= 0:
        return len(prefix)
    seen, index = 0, len(prefix)
    for i in range(len(prefix) - 1, -1, -1):
        if prefix[i].isspace():
            continue
        if i == 0 or prefix[i - 1].isspace():
            seen += 1
            index = i
            if seen >= word_floor:
                break
    return index


class Analyzer:
    """Runs every rule over the text and returns the surviving detections."""

    def __init__(self, rules) -> None:
        self.rules = list(rules)
        self._by_name = {r.name: r for r in self.rules}

    def rule(self, name: str) -> Rule | None:
        return self._by_name.get(name)

    def analyze(
        self, text: str, threshold: float, layout: Layout | None = None
    ) -> list[Detection]:
        """`layout` defaults to the retiring character window, so a caller that
        has no page geometry keeps today's behaviour until the measurement
        settles which default is right."""
        layout = WindowLayout(text) if layout is None else layout
        results: list[Detection] = []
        for rule in self.rules:
            found = rule.detect(text)
            if rule.context:
                found = [d for d in found if _attach(d, rule, layout) is not False]
            results.extend(found)
        kept = [r for r in results if r.score >= threshold]
        return _dedupe(kept)


def _attach(detection: Detection, rule: Rule, layout: Layout):
    """Find the label that reaches this span; boost, or drop a STRICT miss.

    Returns False when the detection must be dropped, and otherwise records the
    attachment on it (None if no label reached a NEAR candidate, which is not a
    failure — the span simply keeps its own sub-threshold score).
    """
    strict = rule.strength(detection.pattern) == STRICT
    if detection.score >= MAX_SCORE and not strict:
        return None
    for context in layout.contexts(detection.start, detection.end, rule.label_words):
        found = _nearest_label(context.text, rule.label_pattern)
        if found is None:
            continue
        term, at, after = found
        if strict and not gap_is_clean(context.text[after:]):
            continue
        span = context.source_span(at, after)
        detection.attachment = Attachment(
            term=term,
            relation=context.relation,
            start=span[0] if span else None,
            end=span[1] if span else None,
        )
        # A STRICT pattern is GATED by its label, not promoted by it: the
        # attachment is the evidence its declared score already prices in.
        # Boosting as well would double-count it, and — the reason this
        # matters — it is what lets a lookbehind become a STRICT pattern
        # without changing a single score (2026-08-14).
        if not strict:
            detection.score = min(
                max(detection.score + CONTEXT_BOOST, CONTEXT_FLOOR), MAX_SCORE
            )
        return detection.attachment
    return False if strict else None


def _nearest_label(text: str, pattern) -> tuple[str, int, int] | None:
    """The label occurrence CLOSEST to the value: (text, start, word end).

    Rightmost wins because the nearest label owns the value: on
    `BSB 013 795 Account 12345678` the account number's band carries both, and
    only one of them introduces it.

    The third element is where the label's own WORD ends, which is where the
    gap begins. It is not the end of the match: a spelling may be a stem
    (`afs lic` matching `AFS Licence`, `enquir` matching `Enquiries`), and
    measuring the gap from the middle of the label's own word would leave
    `ence` sitting in it and fail every strict test.
    """
    last = None
    for match in pattern.finditer(text):
        last = match
    if last is None:
        return None
    at, end = last.span()
    while end < len(text) and text[end].isalnum():
        end += 1
    return text[at:end], at, end


def _dedupe(results: list[Detection]) -> list[Detection]:
    """One detection per (type, start, end), keeping the highest score.

    Several patterns of one rule routinely match the same digits — a grouped
    form and a bare form of the same identifier — and the pipeline's merge
    would union them into an identical span anyway."""
    best: dict[tuple[str, int, int], Detection] = {}
    for r in results:
        prior = best.get(r.key)
        if prior is None or r.score > prior.score:
            best[r.key] = r
    return sorted(best.values(), key=lambda r: (r.start, r.end, r.entity_type))

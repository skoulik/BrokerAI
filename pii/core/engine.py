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
matching `a/c` inside its own pattern. A window of raw characters before the
match, searched case-insensitively for the context term as a substring, is what
that review asked for and needs no NLP engine at all.
"""

from __future__ import annotations

from dataclasses import dataclass

import regex

from pii.core.detection import Detection

# Presidio's LemmaContextAwareEnhancer constants, preserved exactly.
CONTEXT_BOOST = 0.35
CONTEXT_FLOOR = 0.4
MAX_SCORE = 1.0

# How far back a context word may sit. Presidio looked back five *tokens*;
# this is the char-level equivalent, sized for five or six words of statement
# text including a label's punctuation ("Acct No.: ", "BSB / Account "). It is
# deliberately generous in the recall-first direction — a context word further
# away than the value's own label is the promotion this exists for.
CONTEXT_WINDOW_CHARS = 60

# `regex`, not `re`: the account-after-BSB patterns use variable-length
# lookbehind, which the stdlib engine cannot compile.
DEFAULT_FLAGS = regex.MULTILINE | regex.DOTALL | regex.IGNORECASE


@dataclass(frozen=True)
class Pattern:
    """One named regex and the score a bare match of it carries."""

    name: str
    regex: str
    score: float


class Rule:
    """A detector. Subclasses implement `detect`; the engine handles the
    context boost, thresholding and deduplication around them."""

    name: str = ""
    entity: str = ""
    context: tuple[str, ...] = ()

    @property
    def entities(self) -> tuple[str, ...]:
        """Every entity type this rule can emit. Declared rather than
        inferred so registry-composition tests can assert what layer 1 is
        allowed to claim without running it."""
        return (self.entity,) if self.entity else ()

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
        self._compiled = [
            (p, regex.compile(p.regex, self.flags)) for p in self.patterns
        ]

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
                    )
                )
        return out


class Analyzer:
    """Runs every rule over the text and returns the surviving detections."""

    def __init__(self, rules) -> None:
        self.rules = list(rules)
        self._by_name = {r.name: r for r in self.rules}

    def rule(self, name: str) -> Rule | None:
        return self._by_name.get(name)

    def analyze(self, text: str, threshold: float) -> list[Detection]:
        results: list[Detection] = []
        for rule in self.rules:
            found = rule.detect(text)
            if rule.context:
                for detection in found:
                    _boost(detection, text, rule.context)
            results.extend(found)
        kept = [r for r in results if r.score >= threshold]
        return _dedupe(kept)


def _boost(detection: Detection, text: str, context: tuple[str, ...]) -> None:
    """Apply the context boost in place if a context term precedes the span.

    Searching the raw characters before the match, not tokens: see the module
    docstring for why that is the right instrument on statement text."""
    if detection.score >= MAX_SCORE:
        return
    window = text[max(0, detection.start - CONTEXT_WINDOW_CHARS) : detection.start]
    if not window:
        return
    lowered = window.lower()
    if any(term.lower() in lowered for term in context):
        detection.score = min(
            max(detection.score + CONTEXT_BOOST, CONTEXT_FLOOR), MAX_SCORE
        )


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

"""The detection record — what every layer emits and every consumer reads.

Replaces `presidio_analyzer.RecognizerResult` (2026-08-09). Deliberately a
plain mutable dataclass with four fields plus provenance, because that is the
entire surface the pipeline, the locator, the painter and the eval harness ever
used of the presidio type.

`recognizer` carries the NAME of the rule that produced the span, and it is
load-bearing rather than debug decoration: `_collect_invalid` suppresses a
checksum-failure candidate only when a *validated* rule covers it, and that
test has to key on the rule's identity, not on the entity type — an
unvalidated guess of the same type must never suppress a finding.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Attachment:
    """The label that promoted a detection, and how it reached it.

    Carried so the promotion is auditable: a value stripped as ACCOUNT_1 should
    say WHICH word typed it. Before this existed, diagnosing a false positive
    meant reconstructing a 60-character window by hand (2026-08-14). Same
    reason `EntityGroup.votes` reaches the report — a mechanism that can change
    a class must not be silent.

    `relation` is where the label sat relative to the value: `left` (same line),
    `above` (the column overhead), or `window` while the retiring
    character-lookback layout is still in use.
    """

    term: str
    relation: str
    # The label's own span in the analyzed text, when the layout can place it.
    start: int | None = None
    end: int | None = None


@dataclass
class Detection:
    """One detected span. Offsets are into the analyzed string."""

    entity_type: str
    start: int
    end: int
    score: float
    # Name of the rule that emitted this span; "" for spans synthesized by a
    # caller (layer-0 findings, tests).
    recognizer: str = ""
    # The COMPLETE value this span is a piece of, when the span text is not
    # the whole of it. Set only where a value occupies several ranges of the
    # analyzed string — a page that wraps an address across two lines splices
    # the neighbouring column's text between its halves (`locator`) — and it
    # is the pseudonym key, so both halves collect one placeholder instead of
    # forking one address into ADDRESS_1 and ADDRESS_2.
    full_value: str | None = None
    # Name of the PATTERN that matched, within the rule. Provenance for the
    # same reason `recognizer` is: the engine reads it back to learn how
    # strictly this span must be attached to a label (`Rule.strength`).
    pattern: str = ""
    # The label that promoted this span, if one did.
    attachment: Attachment | None = None

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"inverted span: {self.start} > {self.end}")

    @property
    def key(self) -> tuple[str, int, int]:
        return (self.entity_type, self.start, self.end)

    def covers(self, other: "Detection") -> bool:
        return self.start <= other.start and other.end <= self.end

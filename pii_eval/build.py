"""Fragment-based document builder: ground truth by construction.

Templates emit plain fragments and PII fragments; the builder tracks
character offsets so every PII value gets an exact span annotation. The
annotation types use the pii pipeline's entity names (pii/core/pipeline.py).
"""

from dataclasses import dataclass, asdict
from pathlib import Path

# The keep list the scorers run against — the corpus's own, never the shipped
# one. An organization survives only by being on a keep list (2026-08-11), so
# the over-strip axis is only meaningful against a list that names what THIS
# generator emits. See pii_eval/entity_keep.txt.
CORPUS_KEEP_FILE = Path(__file__).with_name("entity_keep.txt")

# Entity types whose leak is an automatic acceptance failure (pii/ROADMAP.md:
# scoring is recall-first and severity-weighted). PERSON_JOINT joined
# 2026-07-15 when the layer-1 JointNameRecognizer took ownership of the
# joint-initials form (100% on seeds 42/123). PERSON_REVERSED joined
# 2026-08-09: it was a per-form probe while the NER layer left a residual, and
# layer 0 closed it at 100% on seeds 42/123/7 (see
# core/reports/2026-08-09-text-layer0-vs-gliner2.md).
# AU_BANK_ACCOUNT_BSB_2_4 joined 2026-08-18 gated from the start: it is a bank
# account number under a different name, and layer 1 covers it deterministically
# once its BSB grouping is known (`AuBsbRule`).
CRITICAL = {
    "AU_TFN", "AU_MEDICARE", "AU_BANK_ACCOUNT", "AU_BSB",
    "AU_BANK_ACCOUNT_BSB_2_4",
    "CREDIT_CARD", "PERSON", "PERSON_JOINT", "PERSON_REVERSED",
}


@dataclass
class Ann:
    type: str
    value: str
    start: int
    end: int
    strip_expected: bool = True
    # For injected checksum-invalid identifiers: where the evidence that the
    # digits are an identifier sits — "in-span" (canonical grouping or an
    # immediately adjacent label), "context" (nearby context words only) or
    # "none" (bare digit run). Drives per-tier collection expectations in the
    # scorer; None for ordinary (valid) entities.
    evidence: str | None = None

    @property
    def critical(self) -> bool:
        return self.type in CRITICAL

    def to_json(self) -> dict:
        return asdict(self) | {"critical": self.critical}


class Doc:
    def __init__(self):
        self._parts: list[str] = []
        self._len = 0
        self._line_start = 0
        self.anns: list[Ann] = []

    def raw(self, text: str) -> "Doc":
        self._parts.append(text)
        self._len += len(text)
        if "\n" in text:
            self._line_start = self._len - (len(text) - text.rfind("\n") - 1)
        return self

    def pii(
        self,
        value: str,
        type: str,
        strip_expected: bool = True,
        evidence: str | None = None,
    ) -> "Doc":
        self.anns.append(
            Ann(
                type, value, self._len, self._len + len(value),
                strip_expected, evidence,
            )
        )
        return self.raw(value)

    def org(self, value: str) -> "Doc":
        # merchant/institution organizations are detected but kept by default
        return self.pii(value, "ORGANIZATION", strip_expected=False)

    def private_org(self, value: str) -> "Doc":
        # The account holder's own entity (PTY LTD / TRUST / ...). Stripped
        # because no keep list names it (pii.core.entity_keep) — which is now
        # true of ANY unrecognized organization; before 2026-08-11 it needed a
        # legal-form marker to strip at all. Own truth type so it scores on the
        # recall/leak axis, not the over-strip axis.
        return self.pii(value, "ORGANIZATION_PRIVATE", strip_expected=True)

    def page_break(self) -> "Doc":
        """Start a new page.

        A page break is a CHARACTER in the source text (form feed), not
        render-time furniture, so every tier honours the same pagination from
        one description: the text tier sees one more whitespace character,
        `pii_eval.render` splits pages on it, and annotation offsets are
        unaffected. The alternative — repeating a header at render time —
        would put PII on the image that is not in the text and break the
        paired-corpus property the image tier exists for.
        """
        self.raw("\f")
        self._line_start = self._len  # column 0 of the new page's first line
        return self

    def pad_to(self, col: int) -> "Doc":
        """Pad with spaces to the given column of the current line —
        fixed-column layouts (the legacy statement) without having to know
        the width of embedded PII values."""
        gap = col - (self._len - self._line_start)
        return self.raw(" " * max(gap, 1))

    def nl(self, n: int = 1) -> "Doc":
        return self.raw("\n" * n)

    @property
    def text(self) -> str:
        return "".join(self._parts)

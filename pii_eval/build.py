"""Fragment-based document builder: ground truth by construction.

Templates emit plain fragments and PII fragments; the builder tracks
character offsets so every PII value gets an exact span annotation. The
annotation types use the pii pipeline's entity names (pii/pipeline.py).
"""

from dataclasses import dataclass, asdict

# Entity types whose leak is an automatic acceptance failure (ROADMAP:
# scoring is recall-first and severity-weighted).
CRITICAL = {
    "AU_TFN", "AU_MEDICARE", "AU_BANK_ACCOUNT", "AU_BSB",
    "CREDIT_CARD", "PERSON",
}


@dataclass
class Ann:
    type: str
    value: str
    start: int
    end: int
    strip_expected: bool = True

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

    def pii(self, value: str, type: str, strip_expected: bool = True) -> "Doc":
        self.anns.append(
            Ann(type, value, self._len, self._len + len(value), strip_expected)
        )
        return self.raw(value)

    def org(self, value: str) -> "Doc":
        # merchants/organizations are detected but kept by default
        return self.pii(value, "ORGANIZATION", strip_expected=False)

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

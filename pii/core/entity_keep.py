"""What NOT to strip — a configurable keep list, per entity type.

Detection says a value is PII of some class; this says which values are
exempted from replacement anyway, because they identify an institution rather
than the customer. It is the one policy in the tool that is meant to be tuned
per document set, so it lives in a FILE (`data/entity_keep.txt`, or any path
passed in) rather than in code.

**A value is stripped unless the keep list matches it** (Sergei, 2026-08-11).
The organization rule used to run the other way — keep every organization,
strip only names carrying an Australian legal-form marker (PTY / TRUST / ATF /
SMSF) and not on an institution list — and this module is that inversion. Two
failures killed it, both of one kind: the marker is EVIDENCE, and a real page
routinely destroys the evidence while keeping the identifying name.

- A statement's fixed-width narrative field printed the account holder's
  `SK BUSINESS TRUST` as `SK BUSINESS TRUS`. Fully identifying, and already
  stripped in full elsewhere in the same document — but the truncated marker
  matched nothing, so the printing was kept, three times on one page
  (2026-08-11: the borrowed matcher found all three, and the policy discarded
  them).
- An OCR-fused span carrying an institution token (`SK ... TRUS ANZ HIGHETT`)
  was kept by the institution list although it hides a private entity — a
  limit the old module documented and could not fix.

Keeping now takes positive evidence, of a kind a mangled fragment cannot
fake: presence on a list someone wrote down. An unrecognized name is stripped,
which is the recoverable direction — an over-strip costs analytical value, an
under-strip is a breach.

**Per entity type, not just organizations** (Sergei, 2026-08-11, with the
motivating case: a bank's `1300` support line is detected as PHONE_NUMBER and
pseudonymized exactly like a customer's mobile, though it identifies the
institution). Sections in the file scope patterns to a class; unsectioned
lines at the top mean ORGANIZATION, which is the common case and what the
file began as.
"""

import re
from functools import lru_cache
from pathlib import Path

# Shipped default: institutions and common Australian merchants, plus the
# sections that exist to be edited. See the file itself.
DEFAULT_KEEP_FILE = Path(__file__).with_name("data") / "entity_keep.txt"

# Patterns before any [SECTION] header. Organizations are the common case and
# were the whole file before it grew sections.
DEFAULT_SECTION = "ORGANIZATION"

_SECTION = re.compile(r"^\[([A-Z][A-Z0-9_]*)\]$")


class KeepList:
    """One entity type's compiled patterns: `keeps(value)` is True for a value
    that must NOT be stripped.

    Each source line is one pattern, wrapped in `\\b(?:...)\\b` — so a plain
    word (`anz`) matches on word boundaries as it reads, while a line stays
    free to use regex syntax (`st\\.?\\s*george`, `1300[\\s-]*\\d{3}`) where a
    value has spelling variants. Matched case-insensitively and ANYWHERE in the
    value: `WOOLWORTHS NEWTOWN 4821 AU` keeps on `woolworths`, which is the
    form a statement narrative actually prints."""

    def __init__(self, patterns):
        self.patterns = tuple(patterns)
        self._compiled = tuple(
            re.compile(rf"\b(?:{p})\b", re.IGNORECASE) for p in self.patterns
        )

    def keeps(self, value: str) -> bool:
        return any(p.search(value) for p in self._compiled)

    def matches(self, value: str) -> list[tuple[int, int]]:
        """Every range of `value` a pattern exempts, merged and in order.

        The ranges, not just a yes/no, because a keep match exempts exactly
        what it matches and no more: a detected span is routinely WIDER than
        the name on the list — a model reads a whole narrative field as one
        organization ('SK BUSINESS TRUS ANZ HIGHETT LOAN') — and a token like
        ANZ inside it must not carry the account holder's own trust to
        safety."""
        spans = sorted(
            (m.start(), m.end())
            for p in self._compiled
            for m in p.finditer(value)
        )
        merged: list[tuple[int, int]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def __len__(self) -> int:
        return len(self.patterns)


class EntityKeep:
    """The whole keep list: entity type -> KeepList.

    A type with no section keeps nothing, which is the default for every class
    the file does not mention — silence means strip, never keep."""

    def __init__(self, sections: dict):
        self.sections = dict(sections)

    def keeps(self, entity_type: str, value: str) -> bool:
        section = self.sections.get(entity_type)
        return bool(section) and section.keeps(value)

    def matches(self, entity_type: str, value: str) -> list[tuple[int, int]]:
        """The ranges of `value` this type's patterns exempt (see
        KeepList.matches). Empty for a class the file does not mention."""
        section = self.sections.get(entity_type)
        return section.matches(value) if section else []

    def without(self, entity_type: str) -> "EntityKeep":
        """A copy with one type's exemptions dropped — what `--strip-orgs`
        does. Expressed as data rather than as a flag the pipeline checks, so
        "ignore the keep list for X" needs no second code path."""
        return EntityKeep(
            {k: v for k, v in self.sections.items() if k != entity_type}
        )

    def __len__(self) -> int:
        return sum(len(section) for section in self.sections.values())


def parse_keep(text: str) -> EntityKeep:
    """Parse a keep file: `[ENTITY_TYPE]` sections, one pattern per line,
    `#` comments, blank lines ignored. Lines before the first section belong
    to ORGANIZATION.

    A line that does not compile is a configuration error and is reported as
    one — silently dropping it would silently start stripping whatever it was
    written to keep."""
    sections: dict[str, list[str]] = {}
    current = DEFAULT_SECTION
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        header = _SECTION.match(line)
        if header:
            current = header.group(1)
            sections.setdefault(current, [])
            continue
        if line.startswith("["):
            raise ValueError(
                f"line {number} of the keep list looks like a section header "
                f"but is not one: {line!r} (expected [ENTITY_TYPE], upper case)"
            )
        try:
            re.compile(line)
        except re.error as exc:
            raise ValueError(
                f"line {number} of the keep list is not a valid regular "
                f"expression: {line!r} ({exc})"
            ) from None
        sections.setdefault(current, []).append(line)
    return EntityKeep(
        {name: KeepList(patterns) for name, patterns in sections.items()}
    )


def load_keep(path=None) -> EntityKeep:
    """Load a keep list from `path`, or the shipped default."""
    return _load(str(path) if path is not None else str(DEFAULT_KEEP_FILE))


@lru_cache(maxsize=8)
def _load(path: str) -> EntityKeep:
    # Cached per path: a keep list is read once per process, not once per page.
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"cannot read the keep list {path!r}: {exc}"
        ) from None
    return parse_keep(text)

"""Consistent pseudonym mapping store.

Maps each detected PII value to a stable placeholder (John Smith -> PERSON_1)
so the same value gets the same placeholder across a document set, and cloud
responses containing placeholders can be rehydrated back to the originals.

The store is a plain JSON file. Values are matched case-insensitively with
whitespace collapsed; the first-seen surface form is what rehydration
restores.
"""

import json
import re
from pathlib import Path

# Presidio entity type -> placeholder prefix
PLACEHOLDER_PREFIXES = {
    "PERSON": "PERSON",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "AU_TFN": "TFN",
    "AU_MEDICARE": "MEDICARE",
    "AU_ABN": "ABN",
    "AU_ACN": "ACN",
    # checksum-invalid candidate classes (pii.invalid_recognizers) — the
    # valid/invalid distinction survives into the stripped text so
    # cloud-side analysis can reason about typos/forgery
    "AU_TFN_INVALID": "TFN_INVALID",
    "AU_MEDICARE_INVALID": "MEDICARE_INVALID",
    "AU_MEDICARE_MALFORMED": "MEDICARE_MALFORMED",
    "AU_ABN_INVALID": "ABN_INVALID",
    "AU_ACN_INVALID": "ACN_INVALID",
    "CREDIT_CARD_INVALID": "CARD_INVALID",
    "AU_BSB": "BSB",
    "AU_BANK_ACCOUNT": "ACCOUNT",
    "AU_PAYID": "PAYID",
    "CREDIT_CARD": "CARD",
    "LOCATION": "ADDRESS",
    "ADDRESS": "ADDRESS",
    "DATE_OF_BIRTH": "DOB",
    "ORGANIZATION": "ORG",
    "URL": "URL",
    "IP_ADDRESS": "IP",
}

_PLACEHOLDER_RE = re.compile(
    r"\b(" + "|".join(sorted(set(PLACEHOLDER_PREFIXES.values()))) + r")_(\d+)\b"
)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


class PseudonymMap:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        # entity_type -> normalized value -> placeholder
        self._forward: dict[str, dict[str, str]] = {}
        # placeholder -> first-seen surface form
        self._reverse: dict[str, str] = {}
        self._counters: dict[str, int] = {}
        if self.path and self.path.exists():
            self._load()

    def placeholder_for(self, entity_type: str, value: str) -> str:
        """Return the stable placeholder for this value, allocating on first
        sight."""
        prefix = PLACEHOLDER_PREFIXES.get(entity_type, entity_type)
        key = _normalize(value)
        by_value = self._forward.setdefault(prefix, {})
        placeholder = by_value.get(key)
        if placeholder is None:
            self._counters[prefix] = self._counters.get(prefix, 0) + 1
            placeholder = f"{prefix}_{self._counters[prefix]}"
            by_value[key] = placeholder
            self._reverse[placeholder] = value.strip()
        return placeholder

    def rehydrate(self, text: str) -> str:
        """Replace placeholders in text (e.g. a cloud model's answer) with the
        original values. Unknown placeholders are left as-is."""
        return _PLACEHOLDER_RE.sub(
            lambda m: self._reverse.get(m.group(0), m.group(0)), text
        )

    def save(self, path: str | Path | None = None) -> None:
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("no path given for PseudonymMap.save()")
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "counters": self._counters,
            "forward": self._forward,
            "reverse": self._reverse,
        }
        target.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self.path = target

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._counters = data.get("counters", {})
        self._forward = data.get("forward", {})
        self._reverse = data.get("reverse", {})

    def __len__(self) -> int:
        return len(self._reverse)

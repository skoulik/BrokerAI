"""Checksum arithmetic for Australian identifiers and payment cards.

**This module is now the single source of truth.** Each rule in
pii.core.recognizers calls its function exactly once per match and branches
on the result: pass emits the valid class, fail emits the `*_INVALID`
shadow. Because one call decides both, the two halves cannot disagree —
which is the whole reason the Presidio dependency went (2026-08-09). Until
then these were hand-mirrors of Presidio's validators that had to stay
bit-identical to them, and a version bump could silently desync a
valid/invalid pair and drop values through both sides unreported; the
2.2.364 ABN change proved it (record in DONE.md).

Pure functions over digit strings. Each expects the digits already
extracted (see digits()) and returns whether the value passes its rule;
lengths outside the rule's domain return False.
"""


def digits(text: str) -> str:
    """The digit characters of text, in order."""
    return "".join(c for c in text if c.isdigit())


def tfn_checksum(d: str) -> bool:
    """ATO TFN mod-11 over 9 digits."""
    weights = (1, 4, 3, 7, 5, 8, 6, 9, 10)
    if len(d) != len(weights):
        return False
    return sum(w * int(x) for w, x in zip(weights, d)) % 11 == 0


def medicare_checksum(d: str) -> bool:
    """Medicare mod-10 over the 10-digit card number (digit 9 is the
    check digit; pass d[:10] for an 11-digit value carrying the IRN)."""
    weights = (1, 3, 7, 9, 1, 3, 7, 9)
    if len(d) != 10:
        return False
    return sum(w * int(x) for w, x in zip(weights, d)) % 10 == int(d[8])


def abn_checksum(d: str) -> bool:
    """ABN mod-89 over 11 digits.

    The ABR algorithm subtracts 1 from the first digit, so a leading zero
    becomes -1 and the value correctly fails — valid ABNs never start with
    0. (Presidio carried a special case remapping 0 to 9 until 2.2.364,
    which admitted some invalid leading-zero numbers; the two accept
    disjoint sets. `pii_eval/au.py:abn_valid` mirrors THIS function so the
    corpus generator and the detector agree.)
    """
    weights = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    if len(d) != len(weights):
        return False
    nums = [int(x) for x in d]
    nums[0] = nums[0] - 1
    return sum(w * x for w, x in zip(weights, nums)) % 89 == 0


def acn_checksum(d: str) -> bool:
    """ASIC ACN complement check over 9 digits."""
    weights = (8, 7, 6, 5, 4, 3, 2, 1)
    if len(d) != 9:
        return False
    remainder = sum(w * int(x) for w, x in zip(weights, d)) % 10
    return (10 - remainder) % 10 == int(d[8])


def luhn_checksum(d: str) -> bool:
    """Luhn over any length (payment cards use 12-19 digits)."""
    if not d:
        return False
    total = 0
    for i, c in enumerate(reversed(d)):
        x = int(c)
        if i % 2 == 1:
            x *= 2
        total += x - 9 if x > 9 else x
    return total % 10 == 0


def iban_checksum(value: str) -> bool:
    """ISO 13616 mod-97 over an IBAN (separators tolerated).

    Move the first four characters to the end, map letters to 10-35, and
    require the whole number mod 97 == 1.
    """
    compact = "".join(c for c in value if c.isalnum()).upper()
    if not 15 <= len(compact) <= 34:
        return False
    if not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False
    rearranged = compact[4:] + compact[:4]
    total = 0
    for ch in rearranged:
        if ch.isdigit():
            total = total * 10 + int(ch)
        elif ch.isalpha():
            total = total * 100 + (ord(ch) - 55)
        else:
            return False
        total %= 97
    return total == 1

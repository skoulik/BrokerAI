"""Checksum arithmetic for Australian identifiers and payment cards.

Pure functions over digit strings, shared by the shadow recognizers
(pii.core.invalid_recognizers, inverted use: emit on FAILURE) and the
GLiNER2 identifier post-validation (pii.core.gliner2_recognizer, direct
use: an NER identifier guess must pass its class arithmetic to strip
under that class). Each function expects the digits already extracted
(see digits()) and returns whether the value passes its rule; lengths
outside the rule's domain return False.
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
    """ABN mod-89 over 11 digits."""
    weights = (10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19)
    if len(d) != len(weights):
        return False
    nums = [int(x) for x in d]
    nums[0] = 9 if nums[0] == 0 else nums[0] - 1
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

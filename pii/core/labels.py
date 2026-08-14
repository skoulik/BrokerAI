"""The shared label grammar: what may sit between a label and its value.

Until 2026-08-14 every labelled pattern carried its own copy of
``\\s{0,4}(?:no\\.?|number|#)?\\s{0,4}:?\\s{0,4}`` — nine copies, diverging in
ways nobody chose (`#` accepted by TFN / AFSL / credit licence / the a-c
family and not by Medicare / ABN / ACN / card; `card` only by Medicare). Most
of the divergence was masked by a checksum or a grouped fallback, which is luck
rather than design. This module is the one copy.

**A label attaches to a value when nothing but separators and fillers sits
between them.** Two vocabularies, deliberately separate:

- SEPARATORS are characters — whitespace, the punctuation a form prints around
  a field, and the table glyphs OCR leaves behind (`|`, dashes). They carry no
  words.
- FILLERS are the words a form prints between a label and its number that name
  nothing: `no`, `number`, `#`, `ref`. They are not part of the label (the
  label is what names the class) and not part of the value (the standing
  invariant: a label is evidence, not part of the value), so they need a home
  of their own — and it has to be shared, or the divergence comes straight
  back.

`card` is a filler AND a label (`CreditCardRule` names it), which is not a
conflict: a vocabulary that lets `Medicare card 2123 45670 1` attach also lets
`Card 4564 9427 0001 0443` attach, and the rule that owns the label is the one
that emits the class.
"""

from __future__ import annotations

# Characters that may sit between a label and its value. The dash range covers
# the Unicode hyphens OCR emits for a printed rule, and `|`/`¦` are what a
# table border recognizes as.
SEPARATORS = frozenset(
    " \t\r\n   "          # spaces, incl. NBSP and figure space
    ":;.,"                                # field punctuation
    "-‐‑‒–—―"  # hyphen and dashes
    "#*/\\|¦()[]<>"                      # markers, borders, brackets
)

# Words that may sit between a label and its value. Lower-cased; a trailing
# period is a SEPARATOR, so `No.` arrives here as `no`.
FILLERS = frozenset(
    {"no", "nos", "num", "nbr", "number", "numbers", "ref", "reference", "card"}
)


class Exact(str):
    """A label spelling that must match a WHOLE word.

    Label spellings are stems by default — the match runs to the end of its
    own word, so `account` covers `Accounts` and `afs lic` covers
    `AFS Licence`, which is what keeps the vocabularies short enough to read.
    A two-letter spelling cannot afford that: `ac` (a real a/c-family form on
    Australian statements) would match the start of `across` and be reported
    as the label `Across`. Wrap those in `Exact`.
    """

    __slots__ = ()


def gap_tokens(gap: str) -> list[str]:
    """The non-separator words of `gap`, lower-cased, in order."""
    out: list[str] = []
    current: list[str] = []
    for ch in gap:
        if ch in SEPARATORS:
            if current:
                out.append("".join(current).lower())
                current = []
        else:
            current.append(ch)
    if current:
        out.append("".join(current).lower())
    return out


def gap_is_clean(gap: str) -> bool:
    """True when only separators and fillers sit in `gap`.

    This is the whole of `strict` attachment: a label is attached to a value
    when the text between them says nothing of its own. `AFS Licence No 285571`
    is clean (`no` is a filler); `Statement Enquiries 13 22 66` is not, when the
    label is `Statement` — which is the point, since the value belongs to
    `Enquiries`.
    """
    return all(token in FILLERS for token in gap_tokens(gap))

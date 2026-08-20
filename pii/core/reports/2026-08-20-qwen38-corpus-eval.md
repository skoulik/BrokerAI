# Qwen3.8-27B on the real corpus: recall, over-strip, and grounding

**Date:** 2026-08-20 · **Status: measured, ONE configuration, no baseline run.**

**Decision (Sergei, 2026-08-20, on the grounding evidence below): `combined` is NOT
production-grade. `hybrid` stays the default.** Taken on the page-3 box audit and the `ANY`
over-strip it caused, plus the 2026-08-08 prior that one-pass boxes are looser — *without* a
full corpus grounding comparison between the two geometries, which was never run. `combined`
is kept reachable as a comparison instrument, like `geometry="vlm"`.

The recall, over-strip and grounding numbers below therefore describe a **rejected**
configuration. They are kept because they are the only corpus-scale measurement of this model
so far, and because the leak class, the over-strip gap and the keep-list defect they exposed
are all independent of the geometry choice.

Follows [2026-08-19-qwen38-bringup.md](2026-08-19-qwen38-bringup.md), which established that
the mechanisms work on one page. This is the corpus run Sergei asked for: *"a bigger corpus
with xhigh, adjusted prompt, combined pass — recall, precision, grounding accuracy."*

## Configuration under test

| | |
|---|---|
| model | Qwen3.8-27B **Q8_0** (bartowski), arch `qwen35`, MTP head at Q8_0 |
| serving | patched llama.cpp `build 10501`, `-ctxcp 4`, `--spec-type draft-mtp --spec-draft-n-max 2`, `-np 1` |
| geometry | **`combined`** — one pass for values+boxes, boxes used as a search constraint |
| reasoning | **`xhigh`**, budget 4096, cut-off message, lazy grammar |
| prompt | the 2026-08-19 wording (no reasoning prohibition; explicit no-fence instruction) |
| sampling | greedy, `seed` 42 |
| corpus | `pii_eval/corpora/real/1` — 11 documents, 31 pages, hand-authored truth |

Cost: **2 h 20 m** for the survival run, **2 h 18 m** for grounding. The two duplicate all model
work over the same pages because one goes through `strip_pdf` and the other through the per-page
image path; folding survival into the grounding pass would halve every future comparison.

## Recall — 94.1%, gate PASS

102 strip-expected entities, **96 stripped, 6 leaked, zero critical misses**.

| type | n | recall | | type | n | recall |
|---|---|---|---|---|---|---|
| ADDRESS | 22 | 100% | | ORGANIZATION | 22 | **82%** |
| PERSON | 14 | 100% | | LOCATION | 2 | **0%** |
| PERSON_JOINT | 7 | 100% | | AU_BANK_ACCOUNT | 13 | 100% |
| AU_BSB | 6 | 100% | | CREDIT_CARD | 4 | 100% |
| DATE_OF_BIRTH | 2 | 100% | | BARCODE | 2 | 100% |
| REF_NUMBER | 3 | 100% | | everything else | 1 each | 100% |

Also: **58 spans were redacted from detections made elsewhere in their document** (the
cross-page borrow earning its place), and **one value was painted from the model's own box**
(`CMC INVEST`, d07 — no OCR text matched it, so no layer-1 refinement was possible).

For scale, the recorded figure on this corpus is 8 leaks (2026-07-27). That predates the
2026-08-09 detector replacement, so it is a reference point and **not** a controlled comparison.

## Every leak is the same mechanism

```
d01: LOCATION     'HIGHETT'        d05: ORGANIZATION 'Sk Busines'
d09: LOCATION     'Taranga'        d05: ORGANIZATION 'Sk Ma'
                                   d06: ORGANIZATION 'SK'
                                   d09: ORGANIZATION 'SK'
```

Context from the re-OCR of the stripped output:

```
d05: 'loan to sk busines person_1 19,000.00'   <- unpainted, beside a PAINTED PERSON_1
d05: 'id 3 sk ma org 4 conve'                  <- unpainted, beside a PAINTED ORG_4
d06: 'loan to sk - card balance $988.19'
d09: 'funds transfer toperson_3 or taranga from sk 100.00'
```

Every one is a **truncated form of the customer's own entity inside a transaction-narrative
line**: the bank clips the narrative field to a fixed width, so `SK BUSINESS TRUST` is printed
as `SK BUSINES`, `SK MA`, or bare `SK`. The painted placeholders on the same lines prove the
pipeline read and redacted those rows — it simply does not connect the clipped rendering back
to the full name it already holds.

**Checked, because the short values invited it:** these are not substring artifacts of the
scorer's normalized containment. Each was confirmed against the surrounding re-OCR text.

### Why the machinery misses them, and why the fix is not free

`locator.locate_borrowed` hunts every known value across the document through four tiers —
exact, squash, wrapped, fuzzy. All four match a value **as printed or as damaged**. None
matches a **prefix**, which is precisely what a fixed-width field produces.

A prefix tier is the obvious answer and it collides with a decision already recorded on
`locator.Needle`:

> Measured on a reference statement: the layer-1 fragment `ATF SK MANAGEMENT` matched
> `Name\nSK MANAGEMENT` on another page at distance 3.0 against a budget of 3.0, crossing a
> line break and swallowing the field label. Hence `TEXTUAL_TIERS` for layer 1.

Fragment matching with extra liberties was tried on this same family of values, measured
harmful, and deliberately restricted. A bounded prefix rule (word boundary, minimum length,
layer-0 needles only) would plausibly recover `SK BUSINES` and `SK MA`; bare `SK` is two
characters and cannot be hunted document-wide at any acceptable precision. Realistically that
is 6 leaks → 3 or 4, bought with a liberty that has bitten before. **Recorded, not
implemented.**

## Over-strip — the precision half, and what it is not

| keep-type | n | kept | over-stripped |
|---|---|---|---|
| ORGANIZATION | 24 | 8 | 16 |
| PHONE_NUMBER | 21 | 0 | 21 |
| AU_ABN | 13 | 0 | 13 |
| ADDRESS | 4 | 0 | 4 |
| EMAIL_ADDRESS | 3 | 0 | 3 |

**This is the documented keep-list gap, not a precision collapse caused by this
configuration.** `score_pdf`'s own docstring predicts it: institutional identities (bank
names, ABNs, 1300 numbers) are stripped because `entity_keep.txt` does not yet name them.
Attributing any of it to thinking, to the prompt, or to the combined pass requires the baseline
run, which has not been made.

**A real precision number is still not available**, and the reason is in the truth rather than
the tool: `gt_draft.py` deliberately leaves some references unmarked as an open policy
question, so "findings matching no truth entity" conflates false positives with intentional
gaps. Adjudicating those cases would give the corpus a permanent precision axis.

## Grounding — the new instrument

`pii_eval/score_grounding.py`, written for this run, scores geometry against the truth boxes
that have sat unused in `truth.json` since 2026-07-18. 209 truth occurrences.

### Model boxes — the search constraint

| type | n | boxed | usable | ink contained | IoU |
|---|---|---|---|---|---|
| PERSON | 22 | 22 | 22 | 96% | 69% |
| ADDRESS | 31 | 31 | 30 | 92% | 60% |
| AU_BSB | 23 | 23 | 23 | 99% | 52% |
| AU_BANK_ACCOUNT | 44 | 42 | 30 | **68%** | 47% |
| PERSON_JOINT | 22 | 17 | 12 | **54%** | 56% |
| ORGANIZATION | 42 | 21 | 20 | **46%** | 57% |
| LOCATION | 5 | 0 | 0 | 0% | 0% |
| BARCODE | 2 | 0 | 0 | 0% | 0% |
| **ALL** | **209** | **173** | **151** | **71%** | **57%** |

### Painted boxes — what covers the page

| type | n | fully covered | mean ink | partial |
|---|---|---|---|---|
| ADDRESS | 31 | 31 | 100% | 0 |
| PERSON | 22 | 22 | 100% | 0 |
| AU_BANK_ACCOUNT | 44 | 42 | 97% | 1 |
| PERSON_JOINT | 22 | 20 | 97% | 2 |
| AU_BSB | 23 | 19 | 93% | 4 |
| ORGANIZATION | 42 | 33 | 79% | 0 |
| LOCATION | 5 | 0 | 0% | 0 |
| BARCODE | 2 | 0 | 0% | 0 |
| **ALL** | **209** | **185** | **90%** | **7** |

### What the pair actually shows

**A loose model box costs little _for coverage_.** `AU_BANK_ACCOUNT` boxes contain only 68% of
the ink and just 30 of 44 clear the usability bar, yet 97% of that ink ends up painted.
`ORGANIZATION` is starker: the model grounds 21 of 42 occurrences, and 33 get painted. The
locator recovers what the constraint misses, which is the boxes-as-constraint design absorbing
a bad box rather than propagating it.

**But "costs nothing" is too strong, and the exception is serious — see the section below.**
Coverage is not the only thing a box decides.

**`LOCATION` fails on both axes** — 0/5 boxed, 0/5 painted — which independently corroborates
the leak analysis. It is not a grounding failure; the value is never detected.

**`BARCODE` 0/2 is expected**: barcode masking does not exist yet.

**7 occurrences retain visible ink** while passing the survival gate (AU_BSB 4, PERSON_JOINT 2,
AU_BANK_ACCOUNT 1). This is the class value-survival structurally cannot see, since a surviving
fragment need not match the whole value. **Do not over-read it**: mean coverage on those types
is 93-97%, so these are slivers, and the instrument cannot tell one antialiased edge pixel from
a readable digit. It says "look here", not "this leaked".

## The grounding failure mode, and why it is not cosmetic

*(Sergei, 2026-08-20, reading the layer-0 overlay of `sensitive/statements/1/1.pdf` produced
under this exact configuration — `--geometry combined --reasoning-effort xhigh`.)*

On page 3, a marketing/disclosure page, layer 0 returned 27 findings of which 21 are the single
token `ANZ`. Auditing each box against the OCR words it actually encloses:

**12 of the 21 `ANZ` boxes contain no `ANZ` at all.** They land on `Sometimes`, `linked`,
`You might`, `Avoid`, `convenient`, `While`, `mortgage. Please`, `Home`, `For`,
`own licence.`, `Phone Banking`, `Banking` — overwhelmingly the first word or two of a
paragraph. The multi-word identifiers on the same page are boxed correctly (`ABN 11 000 016
722` encloses exactly that); only `AFSL 239 545` is shifted. So the failure is specific: **a
short token repeating many times on one page gets roughly the right NUMBER of detections and
badly scattered boxes.**

This is the mechanism behind `ORGANIZATION`'s 46% ink containment in the table above, and it is
visible rather than inferred.

### It has already produced a wrong redaction

`1.pii_map.json` from that run contains `ORG_5 = 'ANY'` — the English word *any*, pseudonymized
as an organization. OCR of page 4 shows why:

```
...NOTIFY ANZ OF ANY UNAUTHORISED OR DISPUTED TRANSACTIONS...
...available at anz.com or any ANZ branch...
```

A misgrounded `ANZ` detection claimed the adjacent word `any`. **The keep list is then
consulted on the CLAIMED text, not on the value the model reported** — and while `anz` is on
the list, `any` is not. So a kept institution was smuggled past the keep list under a different
string, and a common English word became a redaction target document-wide.

That is the general shape of the risk, and it is worse than a loose box:

1. a wrong box makes the locator claim the wrong text;
2. the keep decision is then evaluated against the wrong string;
3. and where NO OCR text matches, `box_geometry` paints the model's box directly — wrong pixels
   covered, the real value left readable. This run hit that path once (`CMC INVEST`, d07).

### What it does not tell us

**Whether `hybrid` grounds any better.** Its boxes come from a dedicated `localize` call rather
than from the detection call, which is exactly the sort of thing that could differ — but no
hybrid grounding measurement exists on this model, so the comparison that matters most for
adopting `combined` has not been made and this report cannot make it.

**The prior points the wrong way for `combined`, and the bringup report under-weighted it.**
`_LOCATE_PROMPT`'s own note records that on Qwen3.6 the two-pass split did not merely recover
the lost recall — it also **boxed more tightly, 1.24x ink against the one-pass prompt's 1.41x**.
So asking one call for values and boxes together was already measured to ground worse, on the
previous model, by the metric this section is about. The 2026-08-19 case for `combined` rested
on token cost and on finding counts, and did not weigh that. Combined with the scattered-box
failure above, the honest position is that `combined` is **cheaper and unproven on grounding**,
not simply better.

## What this does NOT establish

- **Any comparison.** No baseline was run. Recall, over-strip and grounding are all
  uninterpretable as evidence *for* this configuration until `hybrid --reasoning-effort off`
  runs on the same corpus — and that needs the old prompt restored to be a true production
  control, since the prompt change is now global.
- **That thinking helps, that `xhigh` beats `medium`, or that `combined` beats `hybrid`.** Each
  is one variable, and one configuration was run.
- **A precision figure**, for the reason above.
- **That the 7 partial paints are leaks.**
- **Anything about other corpora**, the 4-bit quant, or documents unlike Australian statements.

## Method notes worth keeping

- **Coverage is measured against INK, not against the truth rectangle.** The first version
  compared rectangles and reported EVERY value as a partial leak at 77-89%. That uniformity was
  the tell: truth boxes come from the PDF text layer and carry the font's ascender/descender
  whitespace, while painted boxes come from OCR and are tight to the glyphs. Comparing them
  measures a difference of convention, not a redaction failure.
- **Two runs died before this one produced numbers.** A transient TCP reset killed a 56-minute
  run while the server sat healthy and had already generated the reply — `vlm.http_transport`
  now retries connection-level failures (never an HTTP status, which is an answer). Then the
  grounding scorer crashed 48 minutes in on truth entities that have boxes but no value
  (barcodes), which `score_pdf` skips and it did not. Both fixed, both with tests. A `--limit`
  smoke test could not have caught the second: the corpus's barcodes live in d03, d05 and d10.
- **`score_existing.py`** (session scratchpad) scores already-stripped PDFs with no model in the
  loop, so an interrupted run's completed documents are recoverable from `<corpus>/stripped/`
  rather than lost.

## Next

1. **`hybrid` vs `combined` grounding on the same corpus** — promoted to first by the failure
   mode above. The entire case for the combined pass is that its boxes are no worse, and the
   only regime measured is the one that scatters boxes on repeated short tokens.
2. **The baseline.** `hybrid --reasoning-effort off` with the pre-2026-08-19 prompt, same
   corpus. Without it none of the above is a verdict.
3. **`medium` vs `xhigh`**, one variable at a time.
4. **Consult the keep list on the model's reported value as well as on the claimed text.** The
   `ANY` case is a one-line policy question with a real consequence: today a bad box can route
   a kept institution around the keep list.
5. Fold survival scoring into the grounding pass, halving every future comparison.
6. Decide the truncated-fragment question (bounded prefix tier, or accept the leak class).
7. Adjudicate the unmarked references in `gt_draft.py` to give the corpus a precision axis.
8. Add `[AU_ABN]`, `[AU_AFSL]`, `[AU_CREDIT_LICENCE]` sections to `entity_keep.txt` listing the
   specific institution values these documents carry — the four insurer/bank registrations
   over-stripped on page 3 have no way to be kept today, because a type with no section keeps
   nothing. Specific values, never a generic ABN pattern: a business customer's own ABN must
   stay stripped, the same caution the `[PHONE_NUMBER]` section already documents.

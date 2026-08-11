# Text layer 0 (Qwen3.6-27B) vs GLiNER2 — tier-1 A/B

**Date:** 2026-08-09 · **Corpus:** `pii_eval/corpora/text/s{42,123,7}` (18/14/19 docs, ~31k/29k/37k
chars) · **Server:** Qwen3.6-27B Q8_0, llama-server, `-np 1`

The measurement that gates the GLiNER2 retirement (TODO). Both arms run the same corpus and the
same layer 1; the semantic detector is the only variable — the layer-0 arm builds
`PiiPipeline(ner=False)`, without which `merge_detections` would union GLiNER2 back in and the
comparison could only ever show layer 0 as additive.

- **arm A — `--detector layers`:** presidio patterns/checksums + GLiNER2
- **arm B — `--detector vlm`:** Qwen3.6 over document text + presidio patterns/checksums

## Recall, the classes GLiNER2 owned

| class | s42 A→B | s123 A→B | s7 A→B |
|---|---|---|---|
| PERSON | 99% → **100%** | 100% → 100% | 96% → 96% |
| PERSON_REVERSED | 89% → **100%** | 95% → **100%** | 95% → **100%** |
| PERSON_COMMA | 94% → **100%** | 94% → **100%** | 100% → 100% |
| PERSON_JOINT / _MULTIWORD / _PARTICLE | 100% → 100% | 100% → 100% | 100% → 100% |
| ADDRESS | 100% → 83% ⚠ | 100% → 83% ⚠ | 100% → 83% ⚠ |
| ADDRESS_BARE | 57% → **86%** | 80% → **100%** | 71% → **100%** |
| DATE_OF_BIRTH | 100% → 100% | 100% → 100% | 100% → 100% |
| LOCATION | 75% → **100%** | 100% → 100% | 100% → 100% |
| LOCATION_SHORT | 50% → **100%** | 100% → 100% | 100% → 100% |
| CONTEXTUAL_ID | 0% → 0% | 0% → 0% | 0% → 0% |

Layer-1 classes (TFN, Medicare, ABN/ACN, BSB, account, PayID, card, email, phone) are 100% in
both arms on all three seeds, as expected — neither detector owns them.

**The ADDRESS drop is a scoring artifact, not a leak — verified by reading the output.** The
three affected values per seed are all fixed-column locality lines
(`'NEW KAYLAMOUTH          NSW 2926'`). The model reads the two columns as the two values they
are and returns `NEW KAYLAMOUTH` and `NSW 2926` separately; both strip. What survives between
the placeholders is the column padding:

```
        ADDRESS_2          ADDRESS_3      Statement Number :      ID_1
```

GLiNER2 scored 100% here because `max_width=12` let it emit the whole run, padding included, as
one span. The scorer measures truth-span coverage, so two spans plus whitespace read as
"partial". Nothing identifying leaks either way.

**CONTEXTUAL_ID is 0% in both arms, but the composition improves**: on s42 arm A is 1 partial +
3 leaked, arm B is 4 partial + 0 leaked. The class remains excluded from `build.CRITICAL`.

## Critical gate

| seed | arm A | arm B |
|---|---|---|
| s42 | FAIL — 1 leak (`RANDALL AND JEFFREY ROCHA`) | **PASS** |
| s123 | PASS | **PASS** |
| s7 | FAIL — 1 leaked + 2 partial | FAIL — 3 partial (0 leaked) |

**s7 fails in both arms on the same case, and it is a genuine leak.** The colliding-surname
couples of `legacy_03`/`legacy_06` strip their given names but leave the surname standing,
because that surname is `FEE` — a banking word:

```
15MAR24 LOAN REPAYMENT PERSON_5 FEE          16,732.54  64,195.09
10MAR23 ONLINE ID_6 LOAN TO ORG_1 PERSON_4 AND PERSON_5 FEE  9,514.21
```

This is the colliding-surname trade-off recorded in ARCHITECTURE's joint-name decision, and
layer 0 inherits it rather than fixing it. It does convert s7's full leak into a partial, so the
arm-B failure is strictly less severe — but it is still a surname in the clear and still fails.

## Precision (over-stripping)

| seed | ORGANIZATION over-stripped, A → B |
|---|---|
| s42 | 32/61 → **29/61** |
| s123 | 30/58 → **25/58** |
| s7 | 40/55 → **28/55** |

Layer 0 over-strips **less**, on every seed, despite a prompt that deliberately carries no
institutional carve-outs. This contradicts the expectation going in. Noise findings (matching no
injected entity) are 0 in both arms.

## Regressions

1. **Invalid-identifier reporting degrades, in two ways.** `AU_TFN_INVALID` logged drops 3 → 2
   per seed; the lost candidate is the *context*-tier one on every seed, which the shadow
   recognizers do not collect at the default `likely` tier — it was being surfaced by GLiNER2's
   identifier post-validation demoting a shape-correct checksum-failure. That path dies with
   GLiNER2 and nothing replaces it.
   Separately, `stripped-anyway` rises (s42: TFN 0→4, ABN 0→1, Medicare 0→1): layer 0 reports an
   invalid identifier as `PII_IDENTIFIER`, so it strips under `IDENTIFIER_GENERIC` regardless of
   `--mask-invalid-identifiers`. The direction is safe — a typo'd TFN is a real TFN minus a digit
   — but the documented contract ("only *masked* when `mask_invalid=True`") is no longer true
   under layer 0, and an operator cannot review a value that has already been replaced.
2. **One unlocated finding** (s42, `tx_05.csv`): the model returned
   `'HARVEY AND MILLIER HOLDINGS'`, which is not in the text. Warned and counted, as designed.

## Throughput

~15 s per document (no image to ingest — the text path avoids the ~130 s prefill that dominates
the vision path). Full corpus ~5 min/seed against ~40 s for arm A.

## Verdict

On the classes it was meant to replace, layer 0 is **equal or better on every class and every
seed** except the ADDRESS artifact above, and it closes the standing `PERSON_REVERSED` residual
at 100% across all three seeds. It also over-strips less. The two things it does *not* fix are
`CONTEXTUAL_ID` (0% either way) and the colliding-surname case that fails s7 in both arms.

**Accepted 2026-08-09 (Sergei):** the colliding-surname residual is a standing loss — a surname
like Fee in bank-statement context is not worth further precision engineering — and both
invalid-identifier regressions are logged as follow-ups in [../TODO.md](../TODO.md) rather
than blocking. The GLiNER2 retirement proceeds on these numbers.

# Recognizer feed bake-off: whole page vs per layout block

**Date:** 2026-07-27 · **Corpus:** `pii_eval/corpora/real/1` — 11 real documents / 31 pages
at 300 dpi, 172 hand-authored truth entities · **Question (Sergei):** feed the recognizer a
block's lines instead of one page-wide string — how good or bad is **full** isolation?

## What was compared

Three configurations of the *same* pipeline, over the full product path
(`strip_pdf` → render → OCR → detect → paint → reassemble), scored by re-OCR value survival
against the authored truth:

| | backend | perception | feed |
|---|---|---|---|
| **a** (today's default) | `paddle` | flat `OcrResult`, `_rows` visual banding | whole page, one `detect()` |
| **b** (control) | `doclayout:v3` | `OcrPage`, PP-DocLayoutV3 blocks | whole page, one `detect()` |
| **c** (the change) | `doclayout:v3` | `OcrPage`, PP-DocLayoutV3 blocks | **one `detect()` per block** |

`b` exists to separate the two variables: `a → b` is the cost of changing perception
(backend + reading order), `b → c` is the effect of the feed alone.

Full isolation, not a sentinel: each block's text goes through its own analyzer call, so no
layer-1 pattern, no Presidio context word and no GLiNER2 attention window reaches across a
block boundary. Results are rebased into one page-level view before painting
(`linearization.rebase`), so painting, placeholder numbering and reporting are identical.

Reproduce (the corpus and its sources are gitignored/classified — local only):

```
python -m pii_eval score --modality pdf -c pii_eval/corpora/real/1
python -m pii_eval score --modality pdf -c pii_eval/corpora/real/1 --ocr-backend doclayout:v3 --feed page
python -m pii_eval score --modality pdf -c pii_eval/corpora/real/1 --ocr-backend doclayout:v3 --feed blocks
```

## Headline result

| | a — paddle / page | b — V3 / page | **c — V3 / blocks** |
|---|---|---|---|
| **critical leaks** | 9 | 12 | **8** |
| wall time (31 pages) | 301 s | 401 s | **402 s** |

**Per-block feeding is worth 4 leaks against its own control, and costs no measurable time.**
Wall time is flat between `b` and `c` even though the analyzer runs ~17× more often per page:
OCR dominates the run, and GLiNER2's encoder is quadratic in window length — many short
windows cost about what one long one did.

Per-class recall (strip side; only the rows that moved):

| entity type | n | a | b | **c** |
|---|---|---|---|---|
| AU_BSB | 6 | 100% | 67% | **100%** |
| ADDRESS | 22 | 100% | 95% | **100%** |
| PERSON_JOINT | 7 | 43% | 43% | **71%** |
| LOCATION | 2 | 0% | 0% | 50% |
| AU_BANK_ACCOUNT | 13 | 85% | 77% | 77% |
| REF_NUMBER | 3 | 67% | 33% | 33% |

Unmoved at 100% across all three: DATE_OF_BIRTH, EMAIL_ADDRESS, PHONE_NUMBER, POLICY_NUMBER,
VEHICLE_REGO, BARCODE. Unmoved elsewhere: ORGANIZATION 55%, PERSON 86%, CREDIT_CARD 75%,
MEMBERSHIP_NUMBER 0% (no class covers it — the loyalty-ID TODO).

Leak sets (document ids, values elided):

- `a`: PERSON×2, CREDIT_CARD, AU_BANK_ACCOUNT×2 (d04), PERSON_JOINT×3 (d05, d10)
- `b`: all of `a`, **plus** AU_BSB (d05), AU_BSB (d09), AU_BANK_ACCOUNT (d11)
- `c`: `a` minus PERSON_JOINT (d05 `… and S`), minus PERSON_JOINT (d10), **plus**
  AU_BANK_ACCOUNT (d11)

Over-strip side (the precision axis): ORGANIZATION over-strips 4 → 6 on the perception
change, and AU_ABN 11 → 12 on the feed — one more institutional ABN, which is the recorded
keep-list gap, not a new failure class. Everything else is unchanged.

## Why isolation helps rather than hurts

The predicted cost of full isolation is losing cross-block context: a label in one block can
no longer promote a value in the next. That is real and pinned by a unit test
(`test_blocks_feed_isolates_context_across_a_block_boundary`: `BSB` alone in one block and
`014-936` in the next detects **nothing**, where the page-wide feed detects both the BSB at
0.95 and the account it promotes).

It did not bite on the corpus, because under PP-DocLayoutV3 the label/value panels that carry
those identifiers land *inside* one `table` block. What the corpus does have is the opposite
problem in quantity — a page-wide string mixes a header panel with 40 transaction rows in one
attention window, which is exactly the interference GLiNER2 was already documented to suffer
(same person in two word orders, canonical mention keeps its score, the variant collapses).
Cutting at block boundaries removes it by construction, and the joint-name and BSB recoveries
are where that shows.

## The one regression is reading order inside a block, not the feed

`d11` (`Statements - 1114.pdf`) p2 leaks its account number in **both** `b` and `c`, so it is
a perception-level regression, not a feed one. The panel *is* detected — one `table` block of
15 lines holding the whole header — but lines within a block are emitted in `(top, left)`
order, and the panel is three columns, so they interleave:

```
': 162-097111-4' / 'THE DIRECTOR' / 'Account Number' / '23JUN22' / '25 OAKLANDS WAY'
/ 'Statement Period From :' / 'VIC 3810' / …
```

The value is emitted *before* its own label. `AuAccountNumberRecognizer` promotes on an
adjacent context word, so the promotion never happens. The flat `paddle` path gets this right
by accident: `_rows` bands side-by-side detection regions into one visual line, so the label
and value arrive as `'Account Number : 162-097111-4'` on a single line.

That is the same defect as review issue #8a (two-column header panels) seen from the other
side, and it is what blocks adopting `doclayout:v3` as the strip default. The fix belongs to
intra-block column structure — cell/column segmentation for `table` blocks — not to the feed.

## Verdict

- The per-block feed is a **win on its own axis**: −4 leaks against its own control, free.
- **Decision (Sergei, on these numbers, same day): `doclayout:v3` + `--feed blocks` becomes
  the default** for `strip --image`/`--pdf` and for the eval scorers. Net against the previous
  default it is 8 leaks vs 9, so the `d11` account and the 2 extra ORGANIZATION over-strips
  are **accepted knowingly**, not overlooked — they are perception-level and get fixed by
  intra-block column structure, not by reverting the feed. The flat path stays reachable as
  `--ocr-backend paddle --feed page`.
### Default-flip verification (same day)

Both eval tiers re-run with **no flags** after the defaults changed:

- `score --modality pdf -c corpora/real/1` reproduces config `c` byte-for-byte (8 leaks) —
  the defaults are wired to what was measured.
- `score --modality image` (synthetic tier-1 renders, single-column) is a **wash on leaks,
  6 → 6**, with the composition changing (`legacy_06` joint form fixed, a second `tx_02` joint
  form newly leaking) and three secondary axes improving: CONTEXTUAL_ID 25 → 50%,
  LOCATION 75 → 100%, LOCATION_SHORT 75 → 100%, invalid-identifier noise findings 2 → 0.
  Every other row identical. Expected: these pages are single-column, so layout blocks and the
  feed have little to bite on — the tier stays a guard, not a discriminator.

- Next, in order: intra-block column segmentation (also closes #8a and the `d11` regression),
  then overlapping trial linearizations, then orphan clustering — an orphan is a one-line
  block under this feed, so it is now a context-free window by construction.

# pii — local PII stripping tool

Phase 1 of the BrokerAI revival ([../ROADMAP.md](../ROADMAP.md)). Strips
personally identifiable information from documents locally so the stripped
version can be shared with cloud models. Uses **pseudonymization with a
consistent mapping** (`John Smith → PERSON_1` everywhere), not blank
redaction, so cloud answers can be rehydrated and analytical utility is
preserved.

Standalone from the RAG app — nothing here imports `rag_tools` or the web app.

The tool is organised into three components under `pii/`: `pii.core` (the engine),
`pii.cli` (this command-line front-end), and `pii.gui` (a planned GUI). This file
covers installation and CLI usage. Component map and dependency rules:
[ARCHITECTURE.md](ARCHITECTURE.md); status: [ROADMAP.md](ROADMAP.md). The engine's
design and open tasks are in [core/ARCHITECTURE.md](core/ARCHITECTURE.md) and
[core/TODO.md](core/TODO.md).

## Install

```
pip install -r pii/requirements.txt
```

**Detection needs a running llama-server.** Layer 0 — a local LLM — is the
detector for every input mode, reached over HTTP: set `--vlm-url` or
`$PII_VLM_URL` (default `http://localhost:8080`). Nothing here starts it. The
only run that does without it is `--layer0 off`, which skips the semantic
detector entirely — fast, offline, and a [reduced
redaction](#skipping-layer-0).

## Usage

```
python -m pii strip document.txt -o document.clean.txt --report
python -m pii strip scan.png --image -o scan.clean.png
python -m pii strip statement.pdf --pdf -o statement.clean.pdf
python -m pii analyze document.txt            # show detections, change nothing
python -m pii rehydrate cloud_answer.txt --map statement.pii_map.json
python -m pii strip statement.pdf --pdf -o statement.clean.pdf \
    --debug=ocr,layer-0,layer-1               # + an annotated copy, see below
```

`strip`/`analyze` accept `-` for stdin. The pseudonym map is
**per-document by default**: `--map` defaults to
`<input>.pii_map.json` next to the input file, so placeholder numbering
restarts with each document. Pass one `--map` path across runs to keep
placeholders consistent over a document set instead; `rehydrate` and
stdin input always need an explicit `--map` (there is no input document
to derive it from). **The map contains the original PII — it is
gitignored and must never leave the machine.**

Flags: `--entity-keep` / `--strip-orgs` (see [the keep
list](#the-keep-list-what-survives) below), `--threshold` (default 0.4),
`--layer0 off` (skip the semantic detector — fast, offline, [reduced
redaction](#skipping-layer-0)), and the checksum-invalid identifier controls
below.

A label — `Account Number`, `ABN`, `AFSL` — belongs to the value beside it by
**visual proximity on the page**: the words to its left on the same line, and
the column directly above it, whichever label is nearest. Text with no page
(plain text, CSV cells, stdin) has only the left band, on the value's own line.
`--report` names the label that promoted each value, so a listing shows *why* a
value was replaced:

```
  AU_BANK_ACCOUNT      0.50  '0007 3111 4'  <- left 'Account'
  AU_AFSL              0.70  '233714'  <- left 'AFSL'
```

## The keep list: what survives

**A detected value is replaced unless the keep list matches it, and a match
exempts only what it covers.** That list — institutions, card networks,
utilities and common merchants — is a plain file of one regex per line, so it
is tuned per document set rather than in code:

```
python -m pii strip statement.pdf --pdf -o out.pdf --entity-keep my_keeps.txt
```

Resolution: `--entity-keep FILE`, else `$PII_ENTITY_KEEP`, else the shipped
`pii/core/data/entity_keep.txt`. `--strip-orgs` ignores the list's
`ORGANIZATION` section and replaces every organization name.

Sections scope patterns to one class (`[PHONE_NUMBER]`, `[ADDRESS]`, …); lines
before the first section are `ORGANIZATION`, the common case. A class the file
does not mention keeps nothing — **silence means strip**.

A detected span is often wider than the name on the list — a model reads a
whole statement narrative field as one organization — so the match is
**subtracted**, not treated as a pass for everything around it:

```
FROM SK BUSINESS TRUS ANZ HIGHETT LOAN   ->   FROM ORG_1 ANZ ORG_2
WOOLWORTHS NEWTOWN 4821 AU               ->   WOOLWORTHS ORG_3
www.anz.com                              ->   www.anz.com
```

The match grows to its whitespace-delimited token (so `www.anz.com` survives
whole rather than becoming `ORG.ANZ.ORG`), and a leftover fragment under four
alphanumeric characters is left alone — `ANZ App` and `TO ANZ LN` keep their
connectives instead of shredding into placeholders.

Why keeping is opt-in rather than the reverse: an unrecognized organization may
be the account holder's own company or trust, which identifies them as surely
as their surname, and a real statement mangles the evidence that used to prove
it — one printed `SK BUSINESS TRUST` as `SK BUSINESS TRUS` in a fixed-width
field and the truncated name was kept three times on a page. An over-strip
costs analytical value; an under-strip is a breach. The cost is real: a
merchant you never listed becomes `ORG_n`, so grow the file from what your
documents actually contain. The shipped `[PHONE_NUMBER]` section shows the
shape of the next case — an institution's `1300` support line — commented out,
because on a business account the holder's own service line is identifying too.

## Images

`strip --image` detects the PII on the page, locates each detected value in
the OCR text, and paints its placeholder over the matching pixels —
background-filled boxes with the placeholder drawn in, so the output image
stays pseudonymized and rehydratable, not blacked out. Painting always happens
on the original image (`pii/core/image_mode.py`).

A local LLM finds the PII in every input mode — reading the page image for
`--image`/`--pdf` and the document text otherwise. The pattern/checksum
recognizers then refine each value into its precise class, restore the
checksum-invalid shadows, and add anything the model missed. Minutes per page
on `--image`/`--pdf`; text is far cheaper, with no image to ingest.

On text and CSV a detected value is located by finding it in the text, and
**every** occurrence of it is replaced, not only the one the model reported. A
value the model returns that is not in the text cannot be redacted; those are
counted and always reported on stderr, independently of `--report`.

On `--image`/`--pdf` the same is now true across the whole document. Values
found anywhere are grouped — spellings that differ only by case, spacing or a
misread glyph are treated as one entity — and every page is then searched for
every value any page produced. So a name the model reads on page 1 and
overlooks on page 4 is redacted on both, and a value printed three times on a
page is painted three times even if the model mentioned it once. The count of
values a page owed to the rest of the document is printed on stderr.

That search tolerates a page not printing the value quite the same way:
differences of case, spacing and punctuation, a name **truncated** to fit a
fixed-width field (`SK BUSINESS TRUST` printed as `SK BUSINESS TRUS`), or a
glyph OCR misread. Short values are matched exactly only — under eight
characters there is not enough of a value to recognize it through damage
without matching half the page. Numbers are stricter again: a digit read as a
letter is treated as damage, but a digit read as a *different* digit is a
different account and never matches.

Each group's class is decided by a majority vote over the individual
detections, and that class applies on every page — including a page that read
the value differently. The vote can therefore go either way: a merchant name
read as a person on one page out of eleven stays kept. `--report` prints every
group with its tally and each spelling it covers, so you can see and check
those decisions.

`--geometry` chooses how detected values are placed on the *page*, so it
applies to `--image`/`--pdf` only.

- `hybrid` (**default**) — a second model pass boxes each detected value, and
  those boxes constrain the search for it in the OCR text. Painting still uses
  exact OCR word boxes; the model's own box is painted only where there is no
  OCR text at all, as for a logo or a barcode, padded and reported separately.
- `ocr` — no second pass; each value is searched for across the whole page
  string. The behaviour before boxes were used, kept for comparison.
- `vlm` — paint the model's own boxes, skipping OCR entirely.

**Use the default.** The model's boxes are far too unreliable to *paint* — 16%
clip by more than 20 px, stochastically, leaving part of a value legible — but
reliable enough to say which occurrence of a value you are looking at, which
is all `hybrid` asks of them. `vlm` is a comparison instrument, not a
production option.

Two lines in the run output report the weaker outcomes, and they are printed
whether or not you passed `--report`: values painted from the model's own box
(approximate geometry, no checksum validation) and values that could not be
placed at all (**not redacted** — treat as a leak and re-run or handle by
hand).

A third warning, printed the same way, says a model response was **cut off at
the token budget** or came back unparseable. Read it differently from the
other two: those name the value they failed on, and this one cannot. What the
model would have gone on to report is unknown, so the affected page may be
missing names, addresses or organizations with nothing to list. Everything the
answer *did* contain is still used. On a PDF the affected page numbers are
printed alongside; re-run those pages, or split the input so each request has
room to finish. Both the strip and analyze commands take `--no-grammar`, which
turns off the GBNF constraint on the model's reply — for comparing detection
quality, or for a server that does not support the `grammar` field. If you see
the "no usable JSON array" warning without having passed it, the server
ignored the grammar.

The OCR engine is **PaddleOCR**, and it supplies *geometry*, not detection.
`--ocr-backend` selects the model tier: `paddle` (default, = `paddle:v6_medium`),
`paddle:v6_medium` or `paddle:v5_server`. Models auto-download to
`models/paddlex` on first use. OCR runs in-process on either wheel.

The numbers behind these defaults are in
[core/reports/2026-08-08-vlm-oneshot-qwen36.md](core/reports/2026-08-08-vlm-oneshot-qwen36.md).

## PDFs

`strip --pdf` treats the PDF as images: every page is rendered to pixels
(`--dpi`, default 300), run through the image path above, and embedded
into a **fresh, image-only output PDF** at the source page's physical
size. Nothing from the source document's internal structure survives —
no text layer, annotations, attachments or metadata — which eliminates
the hidden-text-layer leak class (financial PDFs have been observed
hiding account numbers under white rectangles) by construction rather
than by scrubbing. Placeholders are consistent across the document's
pages; processing is lossless end-to-end with a JPEG embed only at the
final step (~0.2 MB/page at 300 DPI).

### Repair from the PDF's own text layer

A text PDF carries the true characters; the OCR does not always. Where a
word's OCR reading and the document's own text layer describe the same
pixels and agree about what is printed there, the text layer's characters
win — so an account number OCR'd as `O18057571`, which matches no rule at
all because a digit run cannot start after a letter, is read as
`018057571` and redacted. On the reference statements this also restores
an ABN's checksum (`32 O09 656 74O` → `32 009 656 740`).

The text layer is only ever a **repair source**: no word is added, no box
moves, and the output is still rebuilt from pixels, so nothing hidden in
the source can reach it. A page whose text disagrees with its own pixels
— a different revision, another tool's OCR baked in — is refused and read
from OCR alone, and the run says which pages those were. Words the text
layer does not reach at all (an embedded image, a scanned footer) simply
keep their OCR reading; the `ocr` debug overlay colours them so you can
see which is which.

The run always reports what happened (`text layer: 15 OCR reading(s)
repaired, 679 confirmed, of 721 word(s)`). `--text-repair off` is the
OCR-only baseline, for measuring what it buys.

Placeholders are also drawn in the face they replace — bold where the
original was bold, monospaced where it was monospaced, at the document's
own size — which comes from the same pairing and applies to `--pdf` only.

The run makes **two passes** over the document — every page is read
before any page is redacted, which is what lets a value found on one
page be redacted on all of them. Progress on stderr names the pass
(`page 2/9 reading …`, then `page 2/9 redacting …`); the model only runs
in the first, so the second is quick. Rendered pages are held in a
temporary directory in between (a few MB per page, deleted as the second
pass consumes them) rather than being rendered again, so what gets
painted is exactly what the model looked at. `--report` prefixes
detections with their page number and prints the entity groups.

## Debug overlays

`--debug=<layers>` on `strip --image`/`--pdf` writes annotated copies of the page(s) beside the
output, showing how the run reached its decisions. There is one layer per **pipeline stage**,
each independently selectable (`--debug=all` for every one), and **each layer gets its own
file** — combined, they are unreadable on a real statement page:

| layer | drawn | tells you |
|---|---|---|
| `ocr` | word boxes coloured by where the READING came from — grey OCR unaided, green the text layer confirmed it, magenta the text layer replaced it — and assembled line boxes, numbered (blue) | what OCR perceived, how rows were banded, and (on a text PDF) which pixels the document's own text vouches for: the grey words are the regions it does not reach, which is where OCR damage survives |
| `layer-0` | the model's own `bbox_2d` (magenta), labelled with its class | what the LLM named and as which class — nothing else. Empty under `--geometry ocr`, which never asks the model for boxes |
| `locate` | where the value was actually placed (orange), labelled with the tier | which route tied the model's string to pixels: `exact` / `squash` / `fuzzy` (matched inside the box, OCR damage) / `box` (no OCR text matched — the model's padded box is the only geometry) / `dup` (already covered by a wider finding) |
| `layer-1` | the boxes actually painted (red), labelled `CLASS source` | the final plan: the class after refinement, and where the span came from — `L0` the model found it here, `DOC` another page (or another occurrence) did, `L1` only a pattern/checksum did |

Compared across files they explain the pipeline's characteristic moves: an `IDENTIFIER_GENERIC`
on `layer-0` under an `AU_TFN L0` on `layer-1` is layer 1 refining a coarse class; a `… L1` with
nothing under it on `layer-0` is the deterministic recall floor catching what the model missed;
and a `layer-0` box with **no box at the same place on `locate`** is a detection nothing could
place — an unredacted value, the one thing not to miss. The difference between the `layer-0` and
`locate` rectangles is the design in [core/ARCHITECTURE.md](core/ARCHITECTURE.md) made visible:
the model's box is a search constraint, never paint geometry.

Files are named from the clean output with `.debug` and the layer inserted before the extension
— `statement.clean.pdf` → `statement.clean.debug.ocr.pdf`, `…debug.locate.pdf`, and so on;
`--debug-out` overrides the base. Each PDF is a fresh image-only document with every page
annotated, the same reassembly as `--pdf` strip.

### The findings listing

`--debug` also writes `statement.clean.debug.findings.json`, and it is not a fifth layer — it is
what the layers structurally cannot show. Every overlay is geometry, so a value the model named
but returned **no box** for appears on none of them, while still reaching the plan and, through
document-wide grouping, every other page. The listing carries every layer-0 finding for every
page — class, text, `box` (model space, `null` when the model gave none), the tier that placed
it, and the spans it landed on — plus the page's `borrowed` spans, which have no geometry to
draw here by construction because the value was named on another page:

```json
{"summary": {"layer0": "vision", "pages": 6, "findings": 44, "without_box": 2, "unplaced": 0},
 "pages": [{"page": 2,
            "findings": [{"type": "ADDRESS", "text": "24 Stacey Dr Carrickalinga SA 5204",
                          "box": [747, 247, 888, 274], "placed": "squash",
                          "spans": [{"start": 459, "end": 471, "text": "24 Stacey Dr"},
                                    {"start": 511, "end": 532, "text": "Carrickalinga SA 5204"}]},
                         {"type": "PERSON", "text": "-", "box": null, "placed": "exact",
                          "spans": [{"start": 246, "end": 247, "text": "-"}]}],
            "borrowed": [{"type": "ADDRESS", "start": 1034, "end": 1046, "text": "24 Stacey Dr",
                          "value": "24 Stacey Dr Carrickalinga SA 5204"}]}]}
```

`summary.without_box` and `summary.unplaced` are the two counts worth scanning first: the former
is what no overlay will show you, the latter is what was detected and **not redacted**.
`summary.layer0` names the detector that produced the listing (`vision` or `text`): a listing
records what was found, not what was asked for. Under `--layer0 off` no listing is written at
all, rather than an empty one.

**The overlay is not redacted.** It is drawn on the original page — that is the point, you are
reading the text under the boxes — so it is near-PII: keep it local, like the map file.

There is no separate OCR-inspection command; the overlay comes from a real strip run, so what
it shows is what that run did rather than a re-run that may differ (`pii debug ocr` was retired
2026-08-11).

## Checksum-invalid identifiers

A value shaped like a TFN whose mod-11 arithmetic fails is a typo, bad OCR,
or forgery — all three worth surfacing rather than silently dropping.
Shadow recognizers (`pii/core/invalid_recognizers.py`) mirror the checksummed
recognizers (TFN, Medicare, ABN, ACN, credit card/Luhn) with the validation
inverted, emitting distinct classes: `*_INVALID` (checksum fails) and
`*_MALFORMED` (structurally impossible, e.g. a Medicare first digit outside
2-6) — the typo-vs-impossible distinction is exactly the forgery signal.

Three orthogonal controls on `strip`:

- `--invalid-identifiers {ignore,likely,context,all}` — which candidates
  are *collected* (default `likely`). Cumulative tiers: `likely` needs
  evidence inside the span (canonical grouping "123 456 782" or an adjacent
  label "TFN: 123456780"); `context` adds bare digit runs promoted by
  nearby context words; `all` takes every
  failing match — noisy, ~90% of random 9-digit runs fail the TFN checksum.
- `--log-invalid-identifiers {yes,no}` (default `yes`) — list the collected
  candidates on stderr with the precise failed rule. **The log is
  near-PII** (a typo'd TFN is a real TFN minus a digit): local-only, like
  the map file.
- `--mask-invalid-identifiers {yes,no}` (default `no`) — also pseudonymize
  them (`TFN_INVALID_1`, `MEDICARE_MALFORMED_1`, ...), so the
  valid/invalid distinction survives into the stripped text. Combining
  with `all` warns: it would eat most reference/receipt numbers.

Guardrails: a candidate covered by a *validated* detection of another class
is not collected (every valid TFN fails the ACN checksum; suppression keys
on the validating recognizer's name, so an unvalidated guess of the same
class never suppresses); when a collected span overlaps a valid detection in masking,
the extents union and the valid class wins the placeholder (recall-first).
Tier-1 eval (2026-07-14): `likely` and `context` run zero-noise; `all`
logged 44 noise findings over 11 docs.

## Detection layers

0. **Local LLM** — reads the page image (`pii/core/vlm.py`) or the document
   text (`pii/core/text_llm.py`) and names the values, in five coarse classes.
   The semantic detector: names, addresses, organizations, dates of birth, and
   anything identifier-shaped. Layer 1 still runs over the same string and is
   merged on top.
1. **Patterns and checksums** (`pii/core/recognizers.py`, run by
   `pii/core/engine.py`) — `AU_TFN`, `AU_MEDICARE`, `AU_ABN`, `AU_ACN` and
   payment cards, each from ONE rule that emits the valid class or its
   `*_INVALID` shadow from a single checksum call; plus BSB (`AU_BSB`),
   account numbers (`AU_BANK_ACCOUNT`), PayID (`AU_PAYID`), corporate licence
   numbers (`AU_AFSL`, `AU_CREDIT_LICENCE` → `AFSL_n` / `ACL_n`), the
   `ATF` trustee
   clauses, email, IBAN and AU-region phones. Layer 1 is the deterministic
   floor under a stochastic detector: it types identifiers, validates their
   checksums, and catches what the model missed.
2. ~~**Zero-shot NER**~~ — retired 2026-08-09; layer 0 replaced it. Bare place
   names are still not detected — a lone city/town name is acceptable verbatim
   in financial documents.
3. **Local-LLM audit pass** — planned.

### Skipping layer 0

Runs layer 1 alone. No model server is contacted, which makes it one to two
orders of magnitude faster and the only offline mode. It is meant for fast dry
runs, low-sensitivity documents where speed wins, and debugging layer 1 in
isolation.

**It is a reduced redaction, not a free speedup.** Layer 1 is patterns and
checksums, so identifiers are replaced but **PERSON, ADDRESS, ORGANIZATION and
DATE_OF_BIRTH are not detected at all** — names and addresses stay on the page.
Every such run prints a warning saying so, whether or not `--report` was asked
for. Do not treat the output as safe to share.

Works on every input mode. On `--image`/`--pdf` the run becomes OCR → layer 1 →
paint, so pages are still read and painted normally. It cannot be combined with
`--geometry vlm`, which never runs OCR: with no semantic detector there would be
no text for layer 1 either, and the output would be an unredacted copy of the
input. That combination is rejected.

`--debug` writes fewer artifacts: the `layer-0` and `locate` overlays and the
findings listing all describe what the semantic detector did, so with none of
it they would be blank files — and a blank overlay is still an unredacted copy
of the page. They are skipped, and the run says which ones and why. `--debug
all` therefore gives you `ocr` and `layer-1`; asking for *only* the skipped
layers by name writes nothing at all.

Behaviour worth knowing when running the tool: `DATE_TIME` is detected but
**kept** (transaction dates; `DATE_OF_BIRTH` is stripped), and everything else
detected is replaced unless [the keep list](#the-keep-list-what-survives)
names it — which is how merchant and institution names survive. Some
over-stripping is the accepted recall-first cost: every ambiguity resolves
toward stripping. The design rationale behind all of this lives in
[core/ARCHITECTURE.md](core/ARCHITECTURE.md).

## Performance

Dominated by the model server. Text runs at roughly 15 s per document;
`--image`/`--pdf` at minutes per page, most of it spent ingesting the page
image. Locally there is nothing heavy left to load — layer 1 is regexes —
and OCR runs only for `--image`/`--pdf`, where it supplies painting geometry.
`--layer0 off` removes the server from the run entirely and is one to two
orders of magnitude faster, at the cost described [above](#skipping-layer-0).

## Evaluation

Scored by the Tier-1 synthetic corpus in [pii_eval](../pii_eval/README.md)
(`python -m pii_eval generate` / `score`). Current state: all pattern
entities 100%; PERSON, PERSON_REVERSED and PERSON_COMMA 100%. Contextual
identifiers ("a dentist in Wagga Wagga") remain at 0% — a target for the
planned layer-3 audit.

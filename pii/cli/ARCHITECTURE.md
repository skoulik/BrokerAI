# PII CLI — Architecture

The command-line front-end (`pii.cli`) over the [core engine](../core/ARCHITECTURE.md). It owns
argument parsing, input/output plumbing, and stderr reporting — **no detection logic**. Part of
the three-component split; boundary and dependency rules are in the umbrella
[../ARCHITECTURE.md](../ARCHITECTURE.md).

## Surface

Three subcommands (`pii/cli/__init__.py`, `main()`):

| Command | Does |
|---|---|
| `strip` | Replace PII with placeholders; extends the pseudonym map. Modes: text (default), `--csv` (per-cell), `--image` (OCR → paint), `--pdf` (render → OCR → paint → reassemble). |
| `analyze` | Report detections on stdout, change nothing. |
| `rehydrate` | Restore original values in a cloud answer from the map (`--map` required). |

`strip`/`analyze` accept `-` for stdin; `strip` writes stdout or `-o FILE`. Flags cover
threshold, `--strip-orgs`, `--report`, CSV column selection, `--dpi` (PDF render
resolution), `--ocr-backend`, and the three checksum-invalid identifier controls
(`--invalid-identifiers`, `--log-invalid-identifiers`, `--mask-invalid-identifiers`).
Full usage is in [../README.md](../README.md).

## How it maps to `pii.core`

The CLI is thin glue over the core public API:

- Builds a `PiiPipeline` from parsed args (`threshold`, `strip_entities`, invalid-identifier
  policy). `--strip-orgs` just adds `ORGANIZATION` to `DEFAULT_STRIP_ENTITIES` — the pipeline
  already takes a `strip_entities` set; the CLI only assembles it.
- Dispatches by mode to the core entry points: `pipeline.strip` / `pipeline.analyze` for text,
  `pii.core.csv_mode.strip_csv` for `--csv`, `pii.core.image_mode.strip_image` for `--image`,
  `pii.core.pdf_mode.strip_pdf` for `--pdf` (imported lazily so the image/PDF stack —
  Pillow/PaddleOCR/pymupdf — loads only when needed).
- `PseudonymMap` is loaded from `--map`, extended, and saved by the CLI; `rehydrate` is a pure
  map operation with no pipeline.

**Rule:** if the GUI ever needs one of these assembly steps, it moves **down into `pii.core`**,
not imported from here — `cli` and `gui` never depend on each other.

## Design notes

- **Maps are per-document by default (Sergei, 2026-07-18).** `--map` defaults to
  `<input>.pii_map.json` next to the input document, derived in `_derive_map`; placeholder
  numbering therefore restarts per document. Passing one `--map` path across runs restores
  shared-map behaviour when cross-document consistency is wanted. Two corollaries: stdin
  input has no filename to derive from, so `strip -` requires an explicit `--map`; and
  `rehydrate`'s input is a cloud *answer*, not the document, so its `--map` is a required
  argument — a guessed default would grab the wrong document's map more often than the right
  one. The planned extension — per-document + global (+ per-group, definition deferred)
  layered maps — is recorded in [../core/TODO.md](../core/TODO.md); it, not a shared default,
  owns cross-document placeholder consistency.

- **Entry points.** `python -m pii` (canonical, via `pii/__main__.py`) and `python -m pii.cli`
  both reach `main()`. Kept identical so existing docs/usage don't break.
- **The invalid-identifier log is near-PII.** A typo'd TFN is a real TFN minus a digit, so the
  collected candidates are printed to **stderr** and are a local-only artifact, like the map
  file — never stdout, never the output document. `--mask-invalid-identifiers=yes` combined
  with `--invalid-identifiers=all` warns because it would eat most reference/receipt numbers.
- **Mode guards.** `--csv`, `--image` and `--pdf` are mutually exclusive; `--image`/`--pdf`
  require `-o` (an output file path). Enforced with `parser.error` **before** pipeline
  construction, so bad invocations fail instantly instead of after the model load.
- **PDF mode reporting.** `--report` prefixes each detection with its page (`p3`), and a
  `page N/M ...` heartbeat goes to stderr — OCR + NER make multi-page documents slow enough
  to want one.

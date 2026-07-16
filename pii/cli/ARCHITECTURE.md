# PII CLI — Architecture

The command-line front-end (`pii.cli`) over the [core engine](../core/ARCHITECTURE.md). It owns
argument parsing, input/output plumbing, and stderr reporting — **no detection logic**. Part of
the three-component split; boundary and dependency rules are in the umbrella
[../ARCHITECTURE.md](../ARCHITECTURE.md).

## Surface

Three subcommands (`pii/cli/__init__.py`, `main()`):

| Command | Does |
|---|---|
| `strip` | Replace PII with placeholders; extends the pseudonym map. Modes: text (default), `--csv` (per-cell), `--image` (OCR → paint). |
| `analyze` | Report detections on stdout, change nothing. |
| `rehydrate` | Restore original values in a cloud answer from the map. |

`strip`/`analyze` accept `-` for stdin; `strip` writes stdout or `-o FILE`. Flags cover
threshold, `--strip-orgs`, `--report`, CSV column selection, and the three checksum-invalid
identifier controls (`--invalid-identifiers`, `--log-invalid-identifiers`,
`--mask-invalid-identifiers`). Full usage is in [../README.md](../README.md).

## How it maps to `pii.core`

The CLI is thin glue over the core public API:

- Builds a `PiiPipeline` from parsed args (`threshold`, `strip_entities`, invalid-identifier
  policy). `--strip-orgs` just adds `ORGANIZATION` to `DEFAULT_STRIP_ENTITIES` — the pipeline
  already takes a `strip_entities` set; the CLI only assembles it.
- Dispatches by mode to the core entry points: `pipeline.strip` / `pipeline.analyze` for text,
  `pii.core.csv_mode.strip_csv` for `--csv`, `pii.core.image_mode.strip_image` for `--image`
  (imported lazily so the image stack — Pillow/pytesseract — loads only when needed).
- `PseudonymMap` is loaded from `--map`, extended, and saved by the CLI; `rehydrate` is a pure
  map operation with no pipeline.

**Rule:** if the GUI ever needs one of these assembly steps, it moves **down into `pii.core`**,
not imported from here — `cli` and `gui` never depend on each other.

## Design notes

- **Entry points.** `python -m pii` (canonical, via `pii/__main__.py`) and `python -m pii.cli`
  both reach `main()`. Kept identical so existing docs/usage don't break.
- **The invalid-identifier log is near-PII.** A typo'd TFN is a real TFN minus a digit, so the
  collected candidates are printed to **stderr** and are a local-only artifact, like the map
  file — never stdout, never the output document. `--mask-invalid-identifiers=yes` combined
  with `--invalid-identifiers=all` warns because it would eat most reference/receipt numbers.
- **Mode guards.** `--image` and `--csv` are mutually exclusive; `--image` requires `-o` (an
  output image path). These are enforced with `parser.error`.

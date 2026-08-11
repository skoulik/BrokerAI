# PII CLI — Architecture

The command-line front-end (`pii.cli`) over the [core engine](../core/ARCHITECTURE.md). It owns
argument parsing, input/output plumbing, and stderr reporting — **no detection logic**. Part of
the three-component split; boundary and dependency rules are in the umbrella
[../ARCHITECTURE.md](../ARCHITECTURE.md).

## Surface

Three subcommands (`pii/cli/__init__.py`, `main()`) — the `debug` namespace was retired
2026-08-11, its job now done by `strip --debug`:

| Command | Does |
|---|---|
| `strip` | Replace PII with placeholders; extends the pseudonym map. Modes: text (default), `--csv` (per-cell), `--image` (OCR → paint), `--pdf` (render → OCR → paint → reassemble). |
| `analyze` | Report detections on stdout, change nothing. |
| `rehydrate` | Restore original values in a cloud answer from the map (`--map` required). |

`strip`/`analyze` accept `-` for stdin; `strip` writes stdout or `-o FILE`. Flags cover
threshold, `--strip-orgs`, `--report`, CSV column selection, `--dpi` (PDF render
resolution), `--detector`/`--geometry`/`--vlm-url`, `--ocr-backend`, `--debug`/`--debug-out`
(diagnostic overlays), and the three checksum-invalid identifier controls
(`--invalid-identifiers`, `--log-invalid-identifiers`, `--mask-invalid-identifiers`). Full
usage is in [../README.md](../README.md).

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
- **`--detector` resolves per mode (2026-08-09).** Its argparse default is `None`, not a
  detector name, because the right default differs by input: `vlm` for `--image`/`--pdf`,
  `layers` for text and CSV (pending the A/B that gates the GLiNER2 retirement). A sentinel
  rather than a literal default is what lets the CLI tell "the user asked for this" from
  "nobody said" — with `default="vlm"` a plain `pii strip file.txt` would demand a model
  server. **Which detector CLASS is built follows the input, not the flag** (`_build_detector`):
  `--detector vlm` means `VlmDetector` on a page and `TextDetector` on a string. `--geometry`
  is rejected on text input, because there is no page for it to mean anything about.
- **VlmError becomes a message, not a traceback.** A missing or unreachable llama-server is an
  operator problem, so both the media path and the text path catch `VlmError` and re-raise as
  `SystemExit` with the text. This matters more since the flip: it is the failure mode of the
  *default* path on `--image`/`--pdf`.
- **Unlocated values are reported unconditionally**, like the media path's geometry warnings
  and for the same reason: they are detections that were *not* redacted, so the operator must
  see them whether or not `--report` was asked for.
- **PDF mode reporting.** `--report` prefixes each detection with its page (`p3`), and a
  `page N/M ...` heartbeat goes to stderr — OCR + NER make multi-page documents slow enough
  to want one.
- **`--debug` rides on a real run (2026-08-11), replacing the `debug` namespace.** The CLI
  parses the layer list (`pii.core.debug_overlay.parse_layers`) and resolves the destination
  **before** the model server is touched — a typo'd layer name must not surface after minutes of
  detection with the artifact unwritable — then either draws the overlays itself (`--image`: the
  front-end still holds the page it passed in) or hands `strip_pdf` a `DebugSpec` (`--pdf`: the
  pixels live in the run's page cache and are gone by the time a result comes back). Either way
  it is **one file per layer** (`DebugSpec.paths`); rejected on text/CSV like `--geometry`, and
  for the same reason: there is no page. `--debug-out` is a base path defaulting to the OUTPUT
  with `.debug` inserted, so a run's artifacts stay together and a re-run with different flags
  overwrites its own overlays. The not-redacted warning prints once, with every path listed under
  it — once per file, four identical warnings would train the operator to skip them. Engine
  design in [../core/ARCHITECTURE.md](../core/ARCHITECTURE.md) "Diagnostics".

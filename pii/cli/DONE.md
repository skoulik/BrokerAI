# DONE — PII CLI

Completed CLI work. The engineering records for the underlying engine features live in
[../core/DONE.md](../core/DONE.md); this file records the **command surface** as it shipped.

## Command surface *(through 2026-07-16)*

- [x] `strip` / `analyze` / `rehydrate` subcommands with stdin (`-`) and file I/O
      *(2026-07-12)*.
- [x] `--csv` per-cell mode and `--columns` selection *(2026-07-12)*.
- [x] `--image` mode (requires `-o`; mutually exclusive with `--csv`) *(2026-07-14)*.
- [x] Checksum-invalid-identifier controls — `--invalid-identifiers`,
      `--log-invalid-identifiers`, `--mask-invalid-identifiers` — with the near-PII log routed
      to stderr as a local-only artifact *(2026-07-14)*.
- [x] `--strip-orgs`, `--threshold`, `--report` *(2026-07-12)*.

## PDF mode + per-document maps *(2026-07-18)*

- [x] `--pdf` mode (requires `-o`; mutually exclusive with `--csv`/`--image`) with `--dpi`
      (default 300), shared `--ocr-backend`, a `page N/M` heartbeat on stderr, and per-page
      (`p3`) prefixes in `--report`. Mode guards now run **before** pipeline construction so
      bad invocations fail instantly. Engine record in [../core/DONE.md](../core/DONE.md).
- [x] **Per-document pseudonym-map default** (Sergei's call): `--map` defaults to
      `<input>.pii_map.json` next to the input document, for all strip modes; stdin and
      `rehydrate` now require an explicit `--map`. Rationale and the layered-map extension
      plan in [ARCHITECTURE.md](ARCHITECTURE.md) / [../core/TODO.md](../core/TODO.md). First
      CLI tests landed with this (`tests/pii/cli/test_cli.py` — map derivation, mode guards).

## Component split *(2026-07-16)*

- [x] `cli.py` → `pii/cli/__init__.py`; added `pii/cli/__main__.py`. `python -m pii` preserved
      as the canonical entry; CLI now imports the engine via the `pii.core` public API. Details
      in the umbrella [../DONE.md](../DONE.md).

## OCR debug command *(2026-07-24)*

- [x] `debug ocr <image|pdf>` — OCR the page(s) into `OcrPage`(s) and dump them: `--format` json
      (round-trippable) / text (human summary) / overlay (annotated raster). PDFs process **all
      pages by default** (`--page N` selects one; `--dpi` sets render resolution); overlay output
      follows the `-o` extension — `.pdf` reconstructs a fresh image-only PDF with every page
      annotated (via `pdf_mode.rebuild_pdf`, same fresh-document reassembly as `--pdf` strip),
      `.png` annotates a single page. Renderers live in `pii.core.ocr_debug` (drawing reuses the
      shared `pii.core.paint` toolkit); the CLI is arg-parsing + renderer selection only (no
      detection). Note: the overlay PDF is **not** redacted (original text with boxes on top) —
      a near-PII local artifact. Engine record in [../core/DONE.md](../core/DONE.md).

- [x] `--detector` / `--geometry` / `--vlm-url` on `strip --image`/`--pdf`, and the
      **default flip to `--detector vlm --geometry ocr`** *(2026-08-09; engine record in
      [../core/DONE.md](../core/DONE.md)). `--feed` was removed with the per-block feed, and
      `--ocr-backend` collapsed to the paddle model tiers (default `paddle`) when the layout
      backends went. `--detector` uses a `None` sentinel so it can resolve per mode — vlm for
      media, layers for text/CSV — which keeps a plain text run from demanding a model
      server.)*

- [x] `--geometry hybrid` becomes the default, and the two lower-confidence outcomes get a
      voice *(2026-08-09; engine record and rationale in [../core/DONE.md](../core/DONE.md)).
      `--geometry` grew a third choice and now defaults to `hybrid`; `ocr` (the previous
      default) and `vlm` stay as comparison instruments. `_build_detector` still ties
      `want_boxes` to the geometry, but the tie is narrower — only `vlm` uses the one-pass
      boxes prompt, since `hybrid` takes its geometry from a second call that costs no recall.

      `_report_geometry` prints two new lines: values painted from the model's own box, and
      values that could not be placed at all. Both print **independently of `--report`** — one
      is a weaker redaction and the other is no redaction, so neither may be conditional on
      the operator having asked for a detection listing. The counts come from the new
      `box_geometry`/`unlocated` fields rather than from warnings, because Python's default
      filter deduplicates an identical warning from the same line and a second page with the
      same residue would otherwise report nothing. The same `--geometry` flag was added to
      `pii_eval score`, so the hybrid-vs-ocr A/B is one flag.)*

## Debug overlays replace the `debug` namespace *(2026-08-11)*

- [x] `strip --debug=<layers>` / `--debug-out` — annotate the page(s) a run processed with any
      combination of `ocr`, `layer-0`, `locate`, `layer-1` (or `all`), **one file per layer**,
      written beside the output (`--debug-out` is a BASE: the layer name is inserted before the
      extension, defaulting to the output path with `.debug` in it —
      `statement.clean.debug.locate.pdf`). `--image` draws in the CLI (the front-end still holds
      the page); `--pdf` hands `strip_pdf` a `DebugSpec`, because the pixels live in the run's
      page cache. Layers are parsed and the destination resolved by `parser.error` **before** the
      model server is touched — a typo'd layer must not surface after minutes of detection — and
      `--debug` is rejected on text/CSV like `--geometry`, there being no page. The not-redacted
      warning prints once per run with every path listed under it, rather than once per file:
      four identical warnings train an operator to skip them.

- [x] `debug ocr` and the whole `debug` subcommand namespace **retired**, with
      `pii.core.ocr_debug`. It could only show perception, never a detection, and it showed its
      own re-run rather than the run that produced the output. Engine record and the three
      design decisions in [../core/DONE.md](../core/DONE.md).

## The keep list becomes configurable *(2026-08-11)*

- [x] `--entity-keep FILE` / `$PII_ENTITY_KEEP` — the file of patterns whose matches are NOT
      replaced, in optional `[ENTITY_TYPE]` sections (default: the shipped
      `pii/core/data/entity_keep.txt`). Resolved and LOADED in the CLI before the pipeline is
      built, because `pii.core` reads no environment and a bad path or a bad pattern must fail
      the run before a document is processed against a list that is not what the operator
      thinks it is.
- [x] `--strip-orgs` re-expressed as data: it drops the keep list's `ORGANIZATION` section
      (`EntityKeep.without`) instead of adding `ORGANIZATION` to `strip_entities`, which no
      longer means anything now that every class strips by default. Same observable behaviour,
      no second code path in the engine. Engine record and the leak that caused the inversion in
      [../core/DONE.md](../core/DONE.md).

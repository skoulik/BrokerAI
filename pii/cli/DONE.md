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

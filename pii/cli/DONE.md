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

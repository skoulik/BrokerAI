# PII CLI Roadmap

The command-line front-end. Part of the umbrella [../ROADMAP.md](../ROADMAP.md); engine roadmap
is [../core/ROADMAP.md](../core/ROADMAP.md).

## Shipped

- `strip` / `analyze` / `rehydrate` subcommands.
- Text, `--csv` (per-cell, column-aware), `--image` (OCR → paint), and `--pdf`
  (render → OCR → paint → reassemble; `--dpi`) modes.
- Per-document pseudonym-map default (`<input>.pii_map.json`, 2026-07-18).
- Checksum-invalid-identifier controls (collect / log / mask).
- `python -m pii` and `python -m pii.cli` entry points.

## Planned

- Configurable strip-entity selection (`--entities` / named profiles) — see
  [TODO.md](TODO.md).
- Surface any new core input modes as they land in the engine.

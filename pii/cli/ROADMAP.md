# PII CLI Roadmap

The command-line front-end. Part of the umbrella [../ROADMAP.md](../ROADMAP.md); engine roadmap
is [../core/ROADMAP.md](../core/ROADMAP.md).

## Shipped

- `strip` / `analyze` / `rehydrate` subcommands.
- Text, `--csv` (per-cell, column-aware), and `--image` (OCR → paint) modes.
- Checksum-invalid-identifier controls (collect / log / mask).
- `python -m pii` and `python -m pii.cli` entry points.

## Planned

- Configurable strip-entity selection (`--entities` / named profiles) — see
  [TODO.md](TODO.md).
- Surface any new core input modes (e.g. PDF) as they land in the engine.

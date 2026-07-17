# TODO — PII Tool (umbrella)

Open tasks live with their component. This file holds only **cross-cutting** items and pointers.

## By component

- **Core** (engine, detection, OCR/image, eval, GLiNER2 experiments): [core/TODO.md](core/TODO.md)
  — the bulk of open work, including the current image/PDF track.
- **CLI**: [cli/TODO.md](cli/TODO.md)
- **GUI**: [gui/TODO.md](gui/TODO.md) — planning/requirements, nothing built yet.

## Cross-cutting

- [ ] Per-component dependency management — `pii/` keeps a single `requirements.txt` today.
      When the GUI lands it will add its own stack (a GUI toolkit or web framework); decide
      then whether to split requirements per component or move to the repo-wide `pyproject.toml`
      + uv (a Phase 2 item in the root [ROADMAP.md](../ROADMAP.md)). The image stack
      (Pillow/PaddleOCR) is already an optional-feature dependency of `core`.
- [ ] Keep the `gui ⊥ cli` boundary honest as both grow: when the GUI needs behaviour the CLI
      already implements, push it down into `pii.core` rather than importing across front-ends
      (see [ARCHITECTURE.md](ARCHITECTURE.md)).

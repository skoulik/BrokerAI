# DONE — PII Tool (umbrella)

Completed **structural / cross-cutting** work. Per-component engineering records live in each
component's `DONE.md`: [core/DONE.md](core/DONE.md) (the bulk — detection, OCR/image, eval,
GLiNER2), [cli/DONE.md](cli/DONE.md), [gui/DONE.md](gui/DONE.md).

## Component split — core / cli / gui *(2026-07-16)*

- [x] Split `pii/` into three subpackages: `pii.core` (engine), `pii.cli` (command-line
      front-end), `pii.gui` (planned GUI, stubs). Both front-ends build on `core`; the two
      never import each other; `core` depends on no front-end. Rationale and dependency rules:
      [ARCHITECTURE.md](ARCHITECTURE.md).
      - 8 engine modules moved to `pii/core/`; `RECORD_SEPARATOR` moved to
        `pii/core/constants.py` (zero-import, cycle-free), re-exported by `pii.core`.
      - `pii/core/__init__.py` now exposes the engine public API
        (`PiiPipeline`, `PseudonymMap`, `RECORD_SEPARATOR`, `DEFAULT_STRIP_ENTITIES`,
        `InvalidFinding`, `INVALID_ENTITY_TYPES`); `strip_csv`/`strip_image` stay submodule
        imports so `import pii.core` doesn't pull in Pillow/pytesseract.
      - `cli.py` → `pii/cli/__init__.py`, plus a new `pii/cli/__main__.py`. `python -m pii`
        preserved as the canonical CLI entry (unchanged `pii/__main__.py`); `python -m pii.cli`
        added. `pii/__init__.py` became a thin back-compat facade over `pii.core`.
      - Tests moved to `tests/pii/core/`; the `conftest.py` GLiNER2 `sys.modules` stub and the
        deferred import in `pipeline.py` both track `pii.core.gliner2_recognizer`.
      - `pii_eval/score.py` imports from `pii.core`.
      - Docs reorganised: the engine's `ARCHITECTURE`/`ROADMAP`/`TODO`/`DONE` moved to
        `pii/core/`; root docs slimmed to this umbrella set; `cli/` and `gui/` doc sets created
        (GUI as stubs).
      - Verification: full test suite (92) green, including the real-GLiNER2 `model` tests and
        the eval gate; `python -m pii analyze` confirmed end-to-end.

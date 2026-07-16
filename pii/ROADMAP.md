# PII Tool Roadmap (umbrella)

Phase 1 of the [BrokerAI revival](../ROADMAP.md): a standalone, local PII-stripping tool that
lets classified documents be shared with cloud models after **pseudonymization with a
consistent local mapping** (`John Smith → PERSON_1`), rehydratable locally.

As of 2026-07-16 the tool is organised into three components (rationale and dependency rules:
[ARCHITECTURE.md](ARCHITECTURE.md)). Each has its own roadmap; this file is the top-level
status board.

## Components at a glance

| Component | Status | Roadmap |
|---|---|---|
| **Core** (`pii.core`) | Text, CSV and image paths shipped end-to-end; detection layers 1–2 eval-gated on the Tier-1 text corpus. Current front: the image/PDF track. Layer 3 (LLM audit) is contingent. | [core/ROADMAP.md](core/ROADMAP.md) |
| **CLI** (`pii.cli`) | Shipped: `strip` / `analyze` / `rehydrate`, text/CSV/image modes, checksum-invalid-identifier controls. | [cli/ROADMAP.md](cli/ROADMAP.md) |
| **GUI** (`pii.gui`) | **New direction (2026-07-16).** Planning only — requirements not yet finalized; flavor (native vs local web) undecided. Stubs in place. | [gui/ROADMAP.md](gui/ROADMAP.md) |

## Evaluation

The [`pii_eval`](../pii_eval/README.md) harness (Tier-1 synthetic corpus, with Tier-2/3 planned)
scores the **core** engine; it is recall-first and severity-weighted (acceptance = zero critical
misses, not an F1 number). The tier plan lives in [core/ROADMAP.md](core/ROADMAP.md).

## Near-term direction

1. **Core:** image/PDF track — PDF mode → image eval tier → OCR bake-off — then the end-to-end
   evaluation that decides whether layer 3 is needed at all.
2. **GUI:** finalize requirements with Sergei, then choose a flavor and spike a prototype over
   the `pii.core` API (see [gui/TODO.md](gui/TODO.md)).

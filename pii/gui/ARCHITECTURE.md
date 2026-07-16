# PII GUI — Architecture (planning stub)

> **Status: planning only.** No GUI is built yet. Requirements are not finalized and the
> **flavor is undecided** (Sergei leans PyQt6; a local web app is the main alternative — see
> below). This document seeds the design; nothing here is committed. Boundary and dependency
> rules for the whole tool are in the umbrella [../ARCHITECTURE.md](../ARCHITECTURE.md).

## Purpose

A local, interactive front-end over the [core engine](../core/ARCHITECTURE.md) for users who
don't want the CLI: drag in a document, pick the mode, **preview** detections before committing,
run the strip, and manage the pseudonym map (view / rehydrate). It targets the same jobs the CLI
does, plus the review-and-edit loop a terminal can't offer.

## Firm constraints (independent of flavor)

1. **Depends on `pii.core` only — never on `pii.cli`.** The GUI drives the engine through the
   `pii.core` public API (`PiiPipeline`, `PseudonymMap`, `strip_csv`, `strip_image`, …). Any
   behaviour the CLI has that the GUI also needs gets pushed **down into `core`**, not imported
   from `cli`. This is the forcing function that keeps `core` a real library.
2. **Strictly local.** The inputs are classified until stripped, so the GUI must do everything
   on-machine: no telemetry, no external calls, no CDN assets. A localhost-only web server is
   acceptable (nothing leaves the box); a native app is acceptable. Anything that phones home is
   not.
3. **The map and the invalid-identifier log are sensitive.** The map contains original PII; the
   invalid-identifier list is near-PII. The GUI must treat both as local-only and never surface
   them anywhere they could be exported by accident.

## Flavor — open decision

| Option | For | Against |
|---|---|---|
| **Native desktop (PyQt6)** — *Sergei's current lean* | No browser/server; real file dialogs & drag-drop; strongest offline story; single process. | Heavier dependency; more UI code; no ready document viewer — a PDF/image preview widget must be built or pulled in. |
| **Local web app (Quart + browser frontend)** | Mirrors the existing RAG app stack (Quart + PDF.js + jQuery); PDF.js is exactly the document+highlighted-span review surface the image/PDF path needs; cross-platform. | Runs a local server + browser; must be locked to localhost; two-tier (backend/frontend) for a single-user tool. |

Not chosen. To be decided once requirements are set. Whichever wins, the seam to `pii.core`
is identical — the engine call surface doesn't change with the UI toolkit.

## Provisional feature set (to refine with requirements)

- Input-mode selection: text / CSV / image / PDF (PDF pending in core).
- **Analyze preview:** show detected spans highlighted with type + score; let the user
  accept/reject before stripping (recall-first, so default is strip-all).
- Entity selection: which types to strip (names/addresses only, etc.) — pairs with the core
  `strip_entities` work.
- Run strip → save output; view/manage the pseudonym map; rehydrate a pasted cloud answer.
- Invalid-identifier review panel (near-PII — clearly marked local-only).

## Relationship to the eval Tier-3 review UI

The core roadmap already envisions a "local side-by-side review UI" for Tier-3 evaluation
([../core/TODO.md](../core/TODO.md)). The GUI and that review surface likely converge — worth
resolving when GUI requirements are set, so the review UI isn't built twice.

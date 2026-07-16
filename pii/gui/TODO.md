# TODO — PII GUI (stub)

> Planning only. The first task gates all others. Design notes in
> [ARCHITECTURE.md](ARCHITECTURE.md); engine tasks in [../core/TODO.md](../core/TODO.md).

- [ ] **Finalize GUI requirements with Sergei** — scope, primary workflows, target users,
      must-have vs nice-to-have. Everything below depends on this.

## Open questions to resolve (before/with requirements)

- [ ] **Flavor:** native PyQt6 (Sergei's lean) vs local web app (Quart + browser). Decide on
      the requirements; the `pii.core` seam is the same either way.
- [ ] **Model server:** does the GUI assume a running llama-server (for a future layer-3), or
      launch/manage one? (Only relevant if layer 3 is built.)
- [ ] **Packaging:** run-from-source vs a packaged installer; which OSes.
- [ ] **Batch / large files:** single-document interactive use, or batch queues too? Progress
      and cancellation for slow (image/OCR) runs.
- [ ] **Sensitive-data UX:** how the pseudonym map and the near-PII invalid-identifier log are
      shown without creating an export/leak path.
- [ ] **Review-UI convergence:** decide whether this GUI subsumes the eval Tier-3 "local
      side-by-side review UI" ([../core/TODO.md](../core/TODO.md)) so it isn't built twice.

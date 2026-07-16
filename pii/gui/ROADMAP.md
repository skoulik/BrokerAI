# PII GUI Roadmap (stub)

> **New direction (2026-07-16).** Planning only — nothing built. Requirements to be finalized
> with Sergei; flavor undecided (PyQt6 vs local web app — see
> [ARCHITECTURE.md](ARCHITECTURE.md)). Umbrella roadmap: [../ROADMAP.md](../ROADMAP.md).

## Provisional phases

These are placeholders, sequenced but not scheduled — they firm up once requirements land.

1. **Requirements** — decide scope, primary workflows, and target users with Sergei.
   *(the gate for everything below — see [TODO.md](TODO.md))*
2. **Flavor decision** — PyQt6 vs Quart/web, on the finalized requirements.
3. **Spike** — minimal prototype over the `pii.core` API: open a text file, analyze, show
   highlighted spans. Proves the engine seam and the chosen toolkit.
4. **MVP** — strip/analyze/rehydrate for text + CSV + image, map management, invalid-identifier
   review panel.
5. **Beyond** — PDF once core ships it; convergence with the eval Tier-3 review UI; packaging.

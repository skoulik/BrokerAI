# Engineering reports

Standalone analysis reports too large for a DONE.md record — bake-off
comparisons, degradation studies, tuning rounds. Each DONE record that has
a report here links to it; the record carries the distilled conclusions,
the report carries the full evidence.

Naming: `YYYY-MM-DD-<topic>.md` (date first, so they sort
chronologically). Raw data behind a report (JSONL sweeps etc.) lives in
gitignored locations like `pii_eval/reports/` — each report states how to
regenerate its data.

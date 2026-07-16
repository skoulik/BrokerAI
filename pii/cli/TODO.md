# TODO — PII CLI

Open tasks for the command-line front-end. Engine tasks are in
[../core/TODO.md](../core/TODO.md); design in [ARCHITECTURE.md](ARCHITECTURE.md).

- [ ] **Configurable strip-entity selection** — let a run choose which data types to strip
      (e.g. names and addresses only). The engine already accepts a `strip_entities` set
      (`PiiPipeline(strip_entities=…)`, `DEFAULT_STRIP_ENTITIES`); this is purely CLI surface:
      an `--entities` flag and/or named profiles, plus documentation. Today only `--strip-orgs`
      exposes it (adds `ORGANIZATION`). Decide the flag grammar (explicit list vs add/remove vs
      profiles) and how it composes with `--strip-orgs`.

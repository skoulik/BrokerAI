# TODO — PII CLI

Open tasks for the command-line front-end. Engine tasks are in
[../core/TODO.md](../core/TODO.md); design in [ARCHITECTURE.md](ARCHITECTURE.md).

- [ ] **Configurable strip-entity selection** — let a run choose which data types to strip
      (e.g. names and addresses only). The engine already accepts a `strip_entities` set
      (`PiiPipeline(strip_entities=…)`, `DEFAULT_STRIP_ENTITIES`); this is purely CLI surface:
      an `--entities` flag and/or named profiles, plus documentation. Today only `--strip-orgs`
      exposes it (adds `ORGANIZATION`). Decide the flag grammar (explicit list vs add/remove vs
      profiles) and how it composes with `--strip-orgs`.

- [ ] **Decide the exit code for a run that under-redacted** *(deferred deliberately,
      2026-08-12, when the truncation counters landed — Sergei: "leave 0, write a todo item to
      think later")*. `strip` returns 0 unconditionally, including when it has just printed a
      WARNING that something was NOT redacted. There are now four such outcomes, and they are
      not equally bad:

      - `unlocated` — a detection that could not be placed. We can name the value.
      - `unlocated_painted_elsewhere` — placed nowhere, but that value was painted somewhere.
      - `box_geometry` — painted from the model's own box: weaker redaction, not none.
      - `incomplete` (truncated/malformed) — the model's answer did not finish. **We cannot
        name what is missing**, which is what makes this one different in kind: every other
        warning tells the operator what to go and look at.

      The question is not "should truncation exit non-zero" but what the exit code MEANS across
      all four, since a script wrapping the tool has only that one bit. Options: keep 0 and make
      the warnings the contract; a distinct code for "output produced, redaction incomplete";
      or a `--strict` flag that turns any of them into a failure. Whatever is chosen applies to
      all four at once — a per-outcome patchwork is exactly the thing to avoid. Note the
      testbench asserts `main(...) == 0` in several places.

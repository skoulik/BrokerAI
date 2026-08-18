# DONE — completed-task engineering records

Completed Phase 1 tasks, moved here as-is from [ROADMAP.md](ROADMAP.md) in the 2026-07-14
doc reorganization, so the roadmap stays a readable overview while the engineering records —
findings, source-review harvests, eval numbers — stay greppable. The durable decisions and
know-how are *distilled* into [ARCHITECTURE.md](ARCHITECTURE.md); this file is the raw
history. Open tasks live in [TODO.md](TODO.md). Only cross-references were touched during
the move; new completed tasks append to the matching section with their records.

> **Path note (2026-07-16 component split):** records below are verbatim and use the module
> paths that were current when written. The engine modules have since moved from `pii/…` to
> `pii/core/…` (e.g. `pii/pipeline.py` → `pii/core/pipeline.py`), the CLI to `pii/cli/`, and
> the tests to `tests/pii/core/…`; `RECORD_SEPARATOR` moved from `pii/__init__.py` to
> `pii/core/constants.py`. See [ARCHITECTURE.md](ARCHITECTURE.md) for the current map.

## Input types

- [x] Plain text *(2026-07-12: `pii/` package — see its README)*
- [x] Images (scans, screenshots) — OCR with word-level bounding boxes, redact by painting
      over pixel regions *(2026-07-14: `pii/ocr.py` (Tesseract adapter → engine-neutral
      word boxes, char intervals recorded at assembly, span→boxes = interval intersection)
      + `pii/image_mode.py` (full text pipeline on the OCR text, placeholders painted onto
      the original pixels — pseudonymization, not blank redaction) + CLI `strip --image`.
      Tesseract 5.4.0 installed system-wide (winget, UB Mannheim). First e2e demo caught
      all planted PII incl. checksum-valid TFN/Medicare through OCR, and survived OCR
      mangling ("0412 345678"). Still open on the image path: barcode masking, statement
      tables, OCR preprocessing knobs, engine bake-off, PDF reassembly.)*
- [x] PDFs — **treat as images**: render pages → OCR → redact pixels → reassemble PDF.
      Rationale: financial-sector PDFs often have junk/broken text layers, and rebuilding from
      pixels also eliminates the hidden-text-layer leak class entirely.
      *(2026-07-18: `strip_pdf` in `pii/core/pdf_mode.py` — the reassembly leg over the
      2026-07-17 render leg. Per page: render (300 DPI default) → OCR → full text pipeline →
      paint → embed into a fresh pymupdf document at the source page's physical size
      (points). The output is built from scratch, so text layers, annotations, attachments
      and source metadata are absent by construction (metadata dict explicitly emptied too)
      — the PDF slice of the metadata-scrubbing task came free. Lossless end-to-end,
      JPEG q90 at the final embed only (Sergei's call; ~0.2 MB/page vs 1-4 MB PNG;
      configurability is a recorded TODO). Pages stream through one pipeline + one shared
      map: memory flat, placeholders document-consistent. CLI: `strip --pdf -o out.pdf`
      (+ `--dpi`), with per-page `--report` prefixes and a page-progress heartbeat on
      stderr. Same session, per-document pseudonym-map default landed across all strip
      modes: `--map` defaults to `<input>.pii_map.json`, stdin/rehydrate now require an
      explicit `--map`; cross-document consistency deferred to the layered-map task
      (per-document + global + group, TODO.md). Eval wiring: `pii_eval score --modality
      pdf -c corpora/real/<set>` runs strip_pdf on the real corpus' source PDFs and
      scores re-OCR value survival with the image tier's matcher (`score_pdf.py`;
      criticality from build.CRITICAL — real truth carries no critical flags; valueless
      barcode entities skipped until barcode masking; stripped PDFs kept under
      <corpus>/stripped/ for review; summary tables now split strip/keep per entity's
      strip_expected, not per type — real corpora have ORGANIZATION/PHONE_NUMBER on both
      sides). Tests: reassembly contract model-free via a fake OCR at the
      `pdf_mode.get_ocr` seam (page count/size, empty text layer, JPEG embed, clean
      metadata, painted pixels, progress callback) + CLI map-derivation/mode-guard tests
      (`tests/pii/cli/`). E2e smoke on a synthetic 2-page statement PDF: all planted PII
      painted, keeps intact, map + placeholder consistency verified; known warts observed
      unchanged (merchant-suburb over-strip, BSB→TFN_INVALID-style label competition —
      both recorded pre-existing items). Belt-and-braces text-layer scan split out as its
      own TODO.)*
- [x] Bank transaction lists (CSV / statement tables) — column-aware handling.
      *(2026-07-12: CSV mode done — per-cell detection, `--columns` filter; statement tables
      from the image path still pending, see [TODO.md](TODO.md).)* Descriptions
      contain personal names, PayID emails/phones, BSB/account refs; these reveal spending
      patterns and allow re-identification. Keep merchant names (analytical value), strip
      person names — zero-shot NER labels (GLiNER2) distinguish person vs organization.
      Consistent pseudonyms per counterparty so patterns survive but identity doesn't.

## Tasks

- [x] Standalone module/CLI, separate from the RAG app (shares the local model server)
      *(2026-07-12: `pii/`, layers 1–2 working: Presidio + custom AU recognizers, GLiNER.
      Findings: Presidio's AU recognizers need explicit registration; overlapping PII spans
      must be merged not ranked, or partially-covered spans leak; GLiNER needs per-line and
      de-capitalized passes for all-caps statement lines. LLM audit layer still pending.
      CPU-only torch is slow (~1 min/page-ish) — install CUDA torch for the 2080 Ti when it
      matters.)*
- [x] Consistent pseudonym mapping store + rehydration of cloud responses
      *(2026-07-12: JSON store, document-order numbering, case-insensitive value matching.)*
- [x] Review presidio-image-redactor sources (same drill as the gliner2-rs review below, same
      reasons: harvest insights/know-how, not adopt). Decision already made
      (2026-07-14, recorded in [ARCHITECTURE.md](ARCHITECTURE.md)): our image path is
      orthogonal — wrong hook
      point (below our pipeline's merge/invalid/pseudonym layers), wrong output model
      (box-fill redaction, not pseudonymization), no home for barcodes/VLM/PDF-reassembly.
      Review targets: their OCR-word → assembled-text → span → bbox mapping (the one solved
      piece we want as a reference for `pii/ocr.py`), OCR preprocessing knobs (they do
      image preprocessing for Tesseract — bilateral filtering, thresholding variants),
      allow-list/score-threshold plumbing, DICOM handling (skim only — out of scope), and
      any Tesseract quirks encoded in their tests.
      Result (2026-07-14): reviewed v0.0.59 at monorepo head (~3.1k lines + tests; MIT;
      their own docs say "still in beta and not production ready"; tested by MS against
      Tesseract 5.2.0). Orthogonality decision confirmed — and the surprise is that the
      "one solved piece" is the package's WEAKEST part: the span→bbox mapping is a
      what-to-avoid reference, not a crib. Harvested knowledge:
      (a) **Text assembly is a flat `" ".join(words)`** — no line/paragraph structure —
      and char offsets are *re-derived* inside the mapping loop by accumulating
      `len(word)+1`; every bug below lives in that re-derivation. Design for `pii/ocr.py`:
      record `(char_start, char_end, bbox)` per word AT assembly time; span→boxes then
      reduces to pure interval intersection. Their overlap predicate
      `max(pos, start) < min(end, pos+len(word))` is the one core idea worth keeping.
      (b) **Two silent-leak classes in their mapping** (no box painted, no error):
      a substring sanity check `(entity_text in word) or (word in entity_text)` skips
      words when entity boundaries fall mid-word at both ends; and multi-word entities
      advance the shared word iterator in an inner loop, so a second *overlapping*
      analyzer result never sees the consumed words (Presidio returns overlapping
      results!). Our merge-before-paint rule eliminates the second class by construction
      — paint from merged spans only, never from raw analyzer results.
      (c) **Allow-list plumbing is dual-level and the word level is a leak vector**:
      allow_list goes to AnalyzerEngine.analyze (entity-level, fine) but is ALSO
      re-checked per word at paint time — an allow-listed word *inside* a PII entity
      keeps its pixels. Lesson: allow-listing belongs in the text layer only; the paint
      layer must follow merged spans exactly.
      (d) **OCR interchange contract worth adopting**: Tesseract `image_to_data` DICT
      (parallel lists text/left/top/width/height/conf) as the neutral format; their
      DocumentIntelligenceOCR adapter shows any engine normalizes into it (polygon →
      axis-aligned envelope) — the clean seam for our Tesseract/Paddle/VLM bake-off.
      Quirks: conf −1 marks structural non-word boxes (threshold range is [−1, 100]);
      Tesseract emits empty/whitespace-only word boxes that must be dropped *before*
      assembly (their `remove_space_boxes`).
      (e) **Preprocessing is opt-in (default no-op)**. Their full chain
      (ContrastSegmentedImageEnhancer): bilateral filter (d=3, σcolor=σspace=40, grey) →
      linear contrast stretch if std ≤ 40 (α=1.5, β=−mean·α) → adaptive mean threshold
      (block 5, C=10 low-/40 high-contrast selected by std ≤ 40; BINARY_INV when the
      most-common pixel < 122, i.e. dark backgrounds) → Otsu → rescale (2× up < 1 MP,
      2× down > 4 MP, INTER_AREA). Architectural pattern to copy: the preprocessed image
      feeds OCR ONLY; painting happens on the ORIGINAL pixels, with scale_factor metadata
      mapping boxes back (ceil, min dimension 1) — exactly the coordinate-transform
      discipline our render→OCR→paint→reassemble path needs at render-DPI.
      (f) **Tesseract edge quirk**: the DICOM path pads images with a uniform border
      (default 25 px, most-common-bg color) before OCR because Tesseract misreads text
      flush against image edges, then subtracts the padding from boxes (clamped ≥ 0).
      Remember for tightly-cropped statement screenshots.
      (g) **DICOM skim — one idea worth stealing**: a per-document deny-list built from
      *known-by-construction* PHI (metadata name fields), augmented via separator→space,
      upper/lower/title casing, and individual name tokens, fed as an ad-hoc deny-list
      recognizer. Analogue for us: account-holder name/account number known from context.
      Also neat: redaction fill "contrast" = max_pixel − most_common(corner crops).
      (h) **Output model confirmed** as per the orthogonality decision: one result per
      word (a multi-word entity = N boxes sharing one text span), redaction = rectangle
      fill, text is never rebuilt → no pseudonymization seam; AnalyzerEngine is called
      directly → nothing below it can hook in.
      (i) **Testing**: their integration tests pin exact Tesseract pixel boxes (breaks
      across Tesseract versions); their own DICOM eval instead matches with 50 px
      tolerance — our image-tier eval should do tolerance matching from day one.
      Beta-quality signals beyond the mapping bugs: ImageRescaling only works on ndarrays
      despite PIL type hints (PIL input raises TypeError on `image.size < int`).
- [x] Debug the three warts from the first image-path e2e demo (2026-07-14). Raw-result
      attribution: PERSON 'Emily Watson\nAddress' (glued across the OCR line break; GLiNER2
      had the exact span separately) and PERSON '03/06/2026 Transfer' (date as name) were
      BOTH en_core_web_sm (SpacyRecognizer); ADDRESS 'NEWTOWN' inside kept-ORG
      'WOOLWORTHS NEWTOWN' is GLiNER2 label competition (→ overlaps task in
      [TODO.md](TODO.md)).
      Tier-1 ablation, SpacyRecognizer fully removed: PERSON stays 100% (GLiNER2 alone),
      ORGANIZATION over-strips improve 8→6, but CONTEXTUAL_ID goes 3x partial → 3x LEAKED —
      spaCy LOCATION on bare city names ("a teacher in Cairns") is the only contextual-
      identifier coverage layers 1–2 have. Fix adopted: with use_ner=True, SpacyRecognizer
      is restricted to LOCATION (pii/pipeline.py); patterns-only mode keeps the full
      recognizer (its name leaks are already documented). Line-clamping NER spans at OCR
      newlines was considered and rejected: clamping splits a glue span but each fragment
      would still be painted, so it fixes nothing the restriction doesn't.
      Regression tests: tests/pii/test_spacy_policy.py — registry-policy tests run in the
      default suite via a stubbed GLiNER2 (sys.modules shim, no model load) + one
      model-marked test on the real stack. REVISIT when the layer-3 LLM audit lands: it
      should own contextual IDs, after which spaCy emissions can likely be dropped
      entirely (rerun the ablation).
- [x] Experiment: GLiNER2 location label vs spaCy LOCATION (2026-07-14). The spacy
      restriction above only *removed* SpacyRecognizer; this taught GLiNER2 a place-name
      label instead and compared head-to-head. Added a default-off `location=True` flag on
      Gliner2Recognizer: a dedicated single-label LOCATION schema pass (isolated from the
      main labels to dodge label competition — the same reasoning as the address passes).
      Corpus: 32 synthetic docs, seed 123, `--docs 30` (11 CONTEXTUAL_ID notes, 176
      ORGANIZATION merchant keeps, 42 addresses). Three NER-on variants, one shared model:
      A = spaCy LOCATION-only (production), B = GLiNER2 location + spaCy removed, C = both.
      Results (CONTEXTUAL_ID town caught / ORG over-stripped / ADDRESS leaked):
      A 6/11 · 33 · 1;  B **11/11 · 33 · 0**;  C 11/11 · 34 · 0. spaCy is simply blind to
      'Wagga Wagga' and 'Dubbo' (never emits them); GLiNER2 catches all four towns. PERSON
      identical across all (170/172, isolated pass didn't disturb it). B strictly dominates
      the spaCy baseline — higher contextual recall, zero extra org over-strip, one fewer
      address leak — and C (both) is worse than B, so spaCy's *detector* role is droppable.
      FP tuning to reach B's parity: (1) tightened the label description to exclude
      state/country abbreviations and bank/shop/brand names; (2) a min-length floor
      (`LOCATION_MIN_CHARS=4`) — the raw FPs were all short ALL-CAPS tokens ('AU' country
      suffix ×16, 'NSW', 'NAB'), none a real place, and every AU place in the corpus is ≥4
      chars; the floor subsumes an earlier explicit {AU,NSW,…} stop-list (all members ≤3
      chars) and removed the whole +4 incremental over-strip. Trade-off recorded: genuine
      3-letter suburbs (Kew, Ayr) are sacrificed — acceptable for a contextual-ID net the
      layer-3 audit is meant to own. Flag left default-off; the ship decision (flip
      defaults, drop SpacyRecognizer, land the ORG-absorbs-location merge rule) is a
      follow-up in TODO.md. Experiment harness: scratchpad only, not committed.
- [x] Retire the last spaCy recognizer and remove the `--no-ner` regime (2026-07-15).
      Shipped the location-label experiment above as the production decision: flipped
      `Gliner2Recognizer(location=True)` to the default (flag kept for ablations), removed
      `SpacyRecognizer` from the registry unconditionally, and dropped the patterns-only
      regime entirely (Sergei's scope call, 2026-07-15) — `use_ner`/`--no-ner` gone from
      pipeline.py, cli.py, pii_eval score.py/`__main__.py`. spaCy stays only as Presidio's
      NLP engine (tokens/lemmas → context enhancer; NLP_CONFIG untouched). Docstrings, the
      registry-policy comment, ARCHITECTURE.md (spaCy row, diagram, single-pipeline section,
      the two 2026-07-14 decision sections superseded by a dated retirement decision),
      pii/CLAUDE.md, and both READMEs updated. Tests: `_NoopGliner2` stub + a `_gliner2_stub`
      context manager moved into tests/conftest.py; `make_pipeline` grew a `stub_ner=True`
      default (built under the shim → model-free; part of the cache key, not forwarded to
      PiiPipeline), `stub_ner=False` for the model-marked tests; test_spacy_policy.py
      replaced by the slimmer tests/pii/test_registry_policy.py (SpacyRecognizer absent,
      Gliner2Recognizer present and owning LOCATION, model-free via the shim; two
      model-marked real-stack tests: the Emily-Watson nuance and "a teacher in Cairns"
      → LOCATION); test_invalid.py's CLI test runs under the shim, no `--no-ner`.
      Verification: default `pytest` 72 passed / 3 deselected, still model-free (~5 s);
      `pytest -m "slow or model"` 3 passed. Full pii_eval generate+score, seeds 42 and 123,
      `--docs 30` — reproduced the experiment-B numbers: CONTEXTUAL_ID **11/11** towns caught
      both seeds (baseline spaCy: 7/11 on 42, 6/11 on 123 — blind to 'Wagga Wagga'/'Dubbo'),
      ORGANIZATION over-strips unchanged at baseline (seed 42: 22; seed 123: 33), one fewer
      ADDRESS leak each (seed 42: 4→3; seed 123: 2→1), PERSON identical (seed 123: 170/172).
      The only remaining critical misses are the pre-existing joint-name GLiNER2 gap
      ('Jeffrey and Randall Lawrence' seed 42; 'JULIE AND BRIAN SUMMERS'/'BRIAN AND AARON
      MILLER' seed 123) — verified identical on the pre-change baseline over the same corpora,
      i.e. untouched by this work; they are the PERSON_JOINT/PERSON_REVERSED gap already
      queued for the layer-3 audit (the committed docs-9 gate, seed 42, still PASSes). Out of
      scope, as planned: the ORG-absorbs-contained-location merge rule (overlaps task, TODO.md)
      — the location pass reaches org-over-strip parity without it.
- [x] **Retire standalone LOCATION detection** *(2026-07-23; reverses the 2026-07-15
      "GLiNER2 owns LOCATION" ship above). Sergei's call: a lone city/town name
      ('Security property is in Cairns') is acceptable verbatim in mortgage-policy and
      bank-statement documents, and is not worth a dedicated schema pass' latency or its
      false-positive surface. Removed the single-label GLiNER2 location pass
      (`LOCATION_LABELS`/`LOCATION_THRESHOLD`) and its `LOCATION_MIN_CHARS=4` floor; dropped
      `LOCATION` from `DEFAULT_STRIP_ENTITIES`, the placeholder map, and
      `Gliner2Recognizer`'s supported entities. The ADDRESS passes are untouched — full
      addresses and suburb-state-postcode lines still strip, and a suburb in clearly
      address-flavoured context ('resided in Kew') can still be caught by the ADDRESS pass
      (an intended residual overlap). Contextual identifiers that are neither addresses nor
      layer-1 types are now deferred wholesale to the planned layer-3 audit. Corpus
      counterpart: the `LOCATION` truth type flipped from a strip probe to a KEEP probe, and
      `LOCATION_SHORT` (the old floor-sacrifice probe) was removed. The AU place-name
      gazetteer TODO is now contingent on reversing this stance.)*
- [x] **Drop URL and IP_ADDRESS detection** *(2026-07-23). Not relevant to financial
      documents; the predefined `UrlRecognizer`/`IpRecognizer` are removed from the registry
      so they never detect (leaving them loaded-but-unstripped would still clutter
      analyze()/reports).)*
- [x] Log checksum-invalid identifiers. If an identifier candidate passes the detectors, but
      is rejected by the checksum validator, this should be logged. Evaluate if the output
      will become too noisy because of this and if so, make the feature optional. Rationale:
      detect typos, wrong OCR output or outright forgery - all three are important classes.
      Planned design (2026-07-14 discussion, Sergei + Claude): three orthogonal controls.
      `--invalid-identifiers={ignore,all,likely,context}` selects which checksum-rejected
      candidates are *collected*; `--log-invalid-identifiers={yes,no}` and
      `--mask-invalid-identifiers={yes,no}` then act independently on the collected set.
      Collection tiers, distinguished by *where the evidence sits*:
      - ignore = today's silent drop; all = every pattern match failing its checksum;
      - likely = evidence INSIDE the matched span: canonical digit grouping
        ("123 456 782") or an immediately-adjacent label captured by the regex itself
        ("TFN: 123456780") — purely lexical, no NLP; accidental digit runs almost never
        carry canonical grouping or a label;
      - context = evidence OUTSIDE the span: bare unformatted runs promoted by nearby
        context words via Presidio's lemma-based context enhancer (label in a form header,
        value in a cell — patterns can't reach that; the enhancer can).
      Implementation note: no deep Presidio hook or multi-pass needed — add *shadow
      recognizers* mirroring the checksummed recognizers (AU_TFN, AU_MEDICARE, AU_ABN,
      AU_ACN, CREDIT_CARD/Luhn) that emit invalid-class entity types with an inverted
      validate_result (emit only when the checksum FAILS). The collection tiers are then
      just per-pattern base-score configuration, and `context` falls out of Presidio's own
      context enhancer exactly the way AuAccountNumberRecognizer works today (low base
      score + context boost). mask=yes simply adds the invalid classes to strip_entities.
      Decided:
      - Distinct placeholder classes, TWO per failure mode: `*_INVALID` (checksum fails,
        e.g. AU_TFN_INVALID_1) and `*_MALFORMED` (structurally impossible, e.g. Medicare
        first digit outside 2-6: AU_MEDICARE_MALFORMED_1) — they arise from different
        mechanisms anyway (inverted validation on the same pattern vs a RELAXED shadow
        pattern, since Presidio's Medicare regex constrains the first digit so such
        numbers never reach the validator), and the checksum-typo vs structurally-
        impossible distinction is exactly the forgery signal cloud-side analysis needs.
        Report records the precise failed rule.
      - Overlap rule: when an invalid-class span overlaps a valid detection, UNION the
        extents but the valid class wins the type/placeholder regardless of score
        (recall-first: the loser's uncovered tail must never leak; mechanically a
        tie-break in _merge_overlaps ranking invalid classes below any valid type —
        concrete input to the overlaps-merging task in [TODO.md](TODO.md)).
      - Warn on mask=yes with `--invalid-identifiers=all` — it would pseudonymize most
        reference/receipt numbers on a statement (~90% of random 9-digit runs fail the
        TFN checksum) and gut analytical utility.
      Defaults (proposed): likely + log=yes + mask=no.
      Still open: CSV mode needs the same per-cell span clamping NER spans got; the
      log/report content is near-PII (a typo'd TFN is a real TFN minus a digit) —
      document it as a local-only artifact like map.json.
      Sequencing (decided 2026-07-14): eval generator FIRST, then the feature. Extend
      pii_eval with checksum-invalid injection (single-digit typos, wrong first digits)
      with ground truth known by construction, so the feature can be scored the moment it
      lands: leak risk at mask=no (do other layers catch mangled TFNs?), log noise floor
      on clean documents — this confirms the defaults and whether `context` earns its
      keep. First customer of the repo-wide testbench (see root ROADMAP, Phase 2).
      **Result (2026-07-14): shipped as planned** — pii/invalid_recognizers.py shadow
      recognizers, three CLI controls, adopted defaults likely+log=yes+mask=no;
      pii_eval injection docs (loan_inv/tx_inv, evidence-annotated in-span/context/
      none) plus scorer axes logged/missed, stripped-anyway, noise; covered by
      tests/pii/test_invalid.py). Findings beyond the plan:
      - Suppression of "valid identifier of another class" must key on the
        *validating recognizer's name*, not entity type: GLiNER2 emits PHONE_NUMBER/
        CREDIT_CARD as unvalidated guesses, and an NER phone guess over a typo'd TFN
        silently swallowed the finding (caught by the eval, regression-tested).
        Coverage-based, not any-overlap: a spurious valid TFN *inside* a typo'd ABN
        must not suppress the ABN finding (~11% of ABN tails pass the TFN checksum).
      - Grouped-fragment dedupe needed: the 3-3-3 tail of an 11-digit ABN matches the
        TFN/ACN shadow patterns; findings strictly contained in a longer finding are
        dropped, identical spans all kept (same digits failing two checksums are one
        candidate with two rules — both reported).
      - Tier-1 eval (seed 42): likely = 5/5 in-span logged, ZERO noise; context =
        +1/1 context-evidence logged, still ZERO noise — context more than earns its
        keep on synthetic data and may deserve to become the default after tier-2
        (real layouts) confirms; all = 7/7 logged but 44 noise findings over 11 docs
        (licence numbers, ATO/policy refs — the predicted ~90% effect). Leak risk at
        mask=no: 3 of 4 typo'd TFNs were stripped anyway by other layers (NER labels
        them without checksumming); 1 CSV bare run survived — mask=yes exists for
        exactly that.
      - The eval's CSV per-cell clamping concern resolved for free: pattern matches
        cannot cross the cell sentinel, and masked invalid spans ride the existing
        clamp.
      - The appended injection docs' fresh rng draws exposed intermittent GLiNER2
        misses on joint-initials ("E & J Moore") and reversed-caps ("ROCHA RANDALL")
        name forms — the already-documented layer-2 gap, previously unsampled at
        seed 42. Following the CONTEXTUAL_ID precedent they now carry distinct truth
        types (PERSON_JOINT 70%, PERSON_REVERSED 90% on seed 42), visible per-form in
        the report without tripping the layers-1/2 gate; PROMOTE BOTH INTO
        build.CRITICAL when the layer-3 LLM audit lands.
- [x] Evaluate GLiNER2 (https://github.com/fastino-ai/GLiNER2) — why it exist, what it adds
      or improves compared to GLiNER, is it maintained, what license/usage terms.
      Result (2026-07-12): unified schema-driven extractor from Fastino (GLiNER lineage),
      Apache 2.0 incl. the PII model (fastino/gliner2-privacy-filter-PII-multi), actively
      maintained, open training code (fine-tuning on our synthetic corpus is possible).
      Implemented as selectable layer-2 backend (`--ner-backend gliner2`, see
      pii/gliner2_recognizer.py for tuning quirks). Tier-1 eval: PERSON 100% (== GLiNER),
      ~4.7x faster, no ALL-CAPS/context weaknesses; weaker on multi-part AU addresses
      (fragments them into street/suburb spans — pipeline-level adjacent-span merging,
      see the overlaps task in [TODO.md](TODO.md), would close most of the gap) and 3
      extra ORGANIZATION
      over-strips. Decision (Sergei, 2026-07-12): GLiNER2 is the default layer-2 backend;
      `--ner-backend gliner` keeps the old model available for comparison.
- [x] Cleanup sources by removing GLiNER (v1) implementation — it is in git anyways, we
      can get back to it at any time. *(2026-07-13: removed `pii/gliner_recognizer.py`,
      the `--ner-backend` switch, and the `gliner` dep; GLiNER2 is the sole layer-2
      backend. Last commit with v1: 46212eb.)*
- [x] Review sources of gliner2-rs (https://github.com/SemplificaAI/gliner2-rs) — perhaps
      we can leverage some of their ideas, knowledge and experience in relation to GLiNER2
      Result (2026-07-14): reviewed v0.5.1 (~2.3k lines Rust + ONNX export scripts;
      Apache 2.0, single-author beta from Semplifica s.r.l.). Recommendation: do NOT
      adopt — their processor has no label-description support (we depend on it), it's
      Rust/ONNX vs our Python/Presidio stack, and their own benchmarks show PyTorch CUDA
      ~6x faster than best-case ONNX on discrete GPUs (the fragmented 8-session export
      pays per-fragment launch overhead), so no perf win on our 2080 Ti. ONNX/Rust route
      only matters for cold start, CPU/edge, or NPU targets. Harvested knowledge:
      (a) **max_width = 8 words** — confirmed in our model's config.json; GLiNER2
      enumerates spans of 1..8 whitespace words, so entities longer than 8 words
      cannot be emitted → root cause of multi-part AU address fragmentation. NOT
      baked into weights (SpanMarkerV0 span rep = f(start token, end token), no
      width embedding), so it can be lifted at inference by overriding
      `model.max_width` — but the model saw zero positive spans wider than 8 during
      training, so whether the scorer generalizes is an open experiment (cheap:
      bump to 12, rerun the address eval). If it fails, the LoRA task is the proper
      fix (train with larger max_width; lora.py already targets span_rep).
      (b) **max_count = 20** — baked into trained weights twice (count_pred MLP has a
      literal 20-class output, CountLSTM.pos_embedding has 20 rows). BUT for plain
      entity extraction (our use) it does NOT cap mentions: `_extract_entities` uses
      only count slot 0 and returns every span above threshold; pred_count only acts
      as an empty-result gate (≤0 → no output). Count slots matter for structure/
      relation tasks only. No eval probe needed.
      (c) Count-based decoding (one count from the [P] token, all labels of a task
      share the slots + NMS) explains the label-competition effect we work around with
      separate address-only passes — the workaround is well-founded.
      (d) Their `mask_pii_text` drops overlapped spans by score rank — the leaky
      approach we already rejected in favour of merging; confirms our choice.
      (e) Their export scripts default to a self-fine-tuned GLiNER2 checkpoint —
      independent evidence GLiNER2 fine-tunes fine (relevant to the LoRA task).
      (f) If we ever use the ort crate: pin =2.0.0-rc.9 (rc.11/rc.12 hang).
- [x] Experiment: lift GLiNER2 max_width at inference. Follow-up to the gliner2-rs
      review above: max_width=8 is a span-enumeration parameter, not baked into
      weights, so the model *can* score wider spans — but it saw zero positive
      spans wider than 8 words during training, so whether scores generalize was an
      open empirical question.
      Result (2026-07-14): **success — adopted, default max_width=12** (constructor
      option on Gliner2Recognizer, override applied after from_pretrained to both
      `model.max_width` and `model.span_rep.span_rep_layer.max_width`; the plan's
      caveat about the span_rep copy was right). Findings, per plan step:
      1. Corpus width distribution: only ADDRESS exceeds 8 words; widest gold
         spans are the four 9-word one-line addresses (the known fragmentation
         cases); everything else ≤ 4 words.
      2. The scorer generalizes past its training width: 'Flat 66 7 Maddox
         Alleyway, New Kaylamouth NSW 2926' scores 0.99 as ONE span at width ≥ 10
         vs 0.29 for the locality fragment at width 8. Width 9 was NOT enough —
         the model's word tokenizer counts the comma as a word, so nominal word
         counts need ~+1 margin. NMS keeps the whole span and drops fragments.
      3. Tier-1 eval (same code, per width): ADDRESS 6/4/2 (stripped/partial/
         leaked) at w8 → 10/0/2 at w10/12/16 — all four one-line addresses flip
         partial→stripped; every other class unchanged; ORGANIZATION over-strips
         unchanged at w10/12 (52k/8o) with one extra over-strip at w16 → first
         sign of wide-span FP creep, so 12 chosen, not 16. The 2 remaining
         ADDRESS leaks ('53 MILES SUBWAY', 3 words) are width-independent recall
         misses (all-caps street line with no state/postcode context).
      4. Latency (warmed-up, 3-pass schema on a 3000-char window, CUDA): 36.8 ms
         (w8) → 37.3 ms (w12, +1.5%) → 38.4 ms (w16, +4%). Negligible.
      Implication for the LoRA task: no architectural blocker and inference
      already handles wide spans; fine-tuning with larger max_width remains
      desirable only to *train* on wide positives if real-world addresses
      regress — not needed for the synthetic corpus.
      The address workarounds in the recognizer (dedicated address-only passes,
      the 0.3 threshold with score flooring, adjacent-span coalescing) are
      KEPT unchanged — max_width lifts what the model *can* emit, not the label
      competition or the low AU-address confidences those workarounds exist for.
- [x] Layer-1 gap: space-grouped bank accounts leaked (found + fixed 2026-07-14).
      `a/c 1234 5678` (4+4) was detected by nobody: AuAccountNumberRecognizer's
      `\d{5,10}` needs a contiguous run (each half falls short), no pattern spanned the
      internal space, and GLiNER2's recall on the form is inconsistent (catches
      `0007 3111 4`, missed `1234 5678`). Generalization adopted after probing whether
      the label must live in the regex — it mostly needn't (Sergei's catch: the
      "bare-pattern precision disaster" examples all carried their own non-account
      labels, which Presidio's context scoring already discriminates):
      - **"account grouped"** — bare space/hyphen-grouped pattern at 0.15, promoted
        only by account context words, exactly the existing bare-run idiom. Lookahead
        spares year ranges: "account statement period 2023 2024" was the one measured
        FP the context mechanism could not reject on its own.
      - **"labeled account"** extended — the a/c label family (`a/c`, `A/C`, `A/c.`,
        `Ac.`, `Ac:`, `AC`, `acct`, `acc`, optional `no./number/#/:`) matched in-span at
        0.5, because the slash form never survives tokenization into a context term
        (recognizers.py's documented quirk) and a/c is the dominant written form on
        Australian statements (Sergei, 2026-07-14). Contiguous digit alternative
        ordered first so unbroken runs aren't truncated by the grouped alternative.
      - **validate_result digit floor** — <5 total digits across groups is never an
        account; a bound regex alone can't express across separators. Presidio trap
        found reading PatternRecognizer.analyze: the validator must return None (not
        True) on pass — True boosts the score to MAX_SCORE (1.0), destroying the
        sub-threshold context gating the bare patterns rely on.
      Verified: 21-case probe (all label variants strip; year ranges, invoice pairs,
      <5-digit fragments kept); tier-1 patterns-only identical on seed 42 and seed 123
      (one benign delta: the injected invalid CREDIT_CARD is now stripped-anyway —
      its 4x4 groups match near account context; recall-positive); full-NER gate PASS,
      all critical types 100%. Tests: test_pipeline.py (label forms, context promotion,
      year-range guard, no-context kept, digit floor). Known cosmetic quirk, accepted:
      in patterns-only mode spaCy sometimes glues "Salary Ac." into a PERSON span and
      the recall-first merge unions it — digits still stripped, label off.
- [x] Deep source review of spaCy 3.8.13 + en_core_web_sm 3.8.0 (2026-07-15; the drill from
      the gliner2-rs and presidio-image-redactor reviews: harvest, not adopt). Scope as
      planned: focused core (tokenizer/lemmatizer feeding Presidio's context enhancer, the
      NER detector being retired, pattern machinery, span/overlap handling) + architecture.
      Read in place at site-packages; findings verified with tokenizer/NER probes against
      the installed model (tokenizer.explain, feature dumps). Harvested knowledge:
      (a) **Tokenizer algorithm**: whitespace-first segmentation — text splits on
      whitespace runs; a run of *exactly one space* becomes a `spacy` flag on the previous
      token, but ANY other whitespace run (`\n`, `\t`, double spaces) becomes a real
      token that flows into every downstream component, including NER. Per chunk:
      special-case/cache lookup → iterative prefix/suffix regex stripping (re-checking
      specials + token_match each round) → token_match/url_match → infix regex splits.
      Chunk-level tokenization cache (hash of chunk string, default 10k entries);
      multi-token special cases are re-found on the assembled Doc via an internal
      PhraseMatcher and spliced in by a retokenizer (tokenizer.pyx `_apply_special_cases`).
      (b) **The `a/c` quirk, explained from source**: the only infix rule for `/` and `:`
      is `(?<=[alnum])[:<>=/](?=[ALPHA])` (lang/punctuation.py; the en override keeps it) —
      these split ONLY when followed by a letter. So `a/c` → `a|/|c` (POS-tagged X/SYM/NOUN
      — never a usable lemma-context term), while `ac/12345678`, `TFN:123456782`,
      `ph:0412345678` stay SINGLE tokens — a label glued to a digit never becomes its
      own token. Both directions make label words invisible to Presidio's
      LemmaContextAwareEnhancer; our char-level regex label matching is immune. Verified:
      `'A/c No: 12345678'` → `A|/|c|No|:|12345678`.
      (c) **Other boundary rules affecting us** (lang/en/punctuation.py overrides the
      shared defaults): hyphens split after letters AND digits → `062-000` → `062|-|000`,
      `Anne-Marie` → `Anne|-|Marie` (3 tokens each); number+unit suffixes split
      (`100km` → `100|km`); currency prefixes split (`$1,200.50` → `$|1,200.50`); but
      `16/06/2024` and `120/80` stay single tokens (no letter after `/`). Tokenizer
      exceptions are ~500 lines of generated contraction rules (incl. apostrophe-less
      `youll`/`shes` variants, guarded by an `_exclude` list for real words like
      Ill/Shell/Well) — exact-string match only; ORTH concat must equal the source string,
      only NORM may differ.
      (d) **Lemmatizer** (what `token.lemma_` actually is in en_core_web_sm): rule-mode
      EnglishLemmatizer — POS-gated table/suffix-rule lookup with an `is_base_form`
      short-circuit driven by morph features; POS comes from tagger+attribute_ruler, so
      lemma quality degrades exactly where OCR text confuses the tagger. Confirmed gap:
      capitalized header/label words get tagged PROPN and **PROPN lemmas pass through
      unchanged** (`Direct Debits` → lemma `Debits`), so the enhancer's lemma matching
      sees surface forms for HEADER-CASE label words; lowercase inflections lemmatize
      fine (`accounts`→`account`, `debited`→`debit`).
      (e) **NER architecture** (the detector being retired): transition-based BILUO
      (B/I/L/U/O moves over a buffer, pipeline/_parser_internals/ner.pyx), decoded
      GREEDILY — per token, argmax over *valid* transitions; no beam in the shipped
      config, no global optimum. The classifier state is just **three token vectors**
      (current token, first token of the open entity, previous token — _state.pxd
      `set_context_tokens`, n=3) → 64-wide maxout → action scores. Token vectors come
      from an NER-private tok2vec (config.cfg: the shared tok2vec feeds only
      tagger/parser via Tok2VecListener): hash embeddings of NORM + 1-char PREFIX +
      3-char SUFFIX + SHAPE (rows 5000/1000/2500/2500, width 96, **no static vectors**)
      through a depth-4 window-1 maxout CNN — receptive field ±4 tokens.
      (f) **Cross-line glue spans, from mechanism**: `Begin.is_valid` forbids an entity
      from *starting* on an IS_SPACE token or crossing a sentence boundary — but `In`/
      `Last` have NO whitespace check, so `\n` tokens legally sit *inside* an open
      entity; and sentence boundaries come from the parser (senter ships disabled), which
      emits none on punctuation-less OCR lines. Nothing stops a name from swallowing the
      whole block; greedy decoding then commits it. Reproduced:
      `John Citizen\n123 Fake St\nWagga Wagga` = one PERSON (+ `2650` = DATE).
      (g) **AU-place blindness, from mechanism**: trained on OntoNotes 5 (US
      news/broadcast; meta.json sources); no gazetteer, no vectors — an OOV town is
      represented only as a hash-bucketed NORM + prefix/suffix/shape. `Wagga` and `Smith`
      have identical SHAPE (`Xxxxx`), 1-char prefix, 3-char suffix; reduplicated
      `Wagga Wagga` looks like FIRSTNAME LASTNAME → PERSON (verified in sentence
      context); bare `Dubbo` → nothing. Self-reported in-domain scores confirm the class
      weakness: ents_f 0.843 overall but LOC f=0.668, FAC f=0.349 — address-adjacent
      classes were weak even on newswire. `2650` → DATE is the same story: SHAPE `dddd`
      is year-like, and the model sees only ±4 tokens of layout-free context to
      disambiguate. The retirement rationale now rests on mechanism, not just eval
      numbers.
      (h) **Preset-entity cooperation** (worth knowing for rule+model hybrids): the
      transition validity functions honor pre-set `ent_iob` on tokens — presets can
      force-continue an entity across whitespace/sentence bounds and block conflicting
      moves; `doc.set_ents(..., default="unmodified")` is the seam EntityRuler uses to
      pre-seed the model. spaCy's rule/model conflict policy is pluggable per SpanRuler
      (`ents_filter`: prioritize-new vs prioritize-existing, both built on filter_spans).
      (i) **Matcher** (token-pattern DSL, matcher/matcher.pyx): per-token attr dicts with
      quantifiers `! ? + * {n} {n,m}`, predicates REGEX/IN/NOT_IN/IS_SUBSET/IS_SUPERSET/
      INTERSECTS/comparisons, and **FUZZY/FUZZY1–9** per-attr fuzzy token matching via
      bundled polyleven Levenshtein with the default edit budget
      `max(2, round(0.3·len(pattern)))` (matcher/levenshtein.pyx) — ready-made prior art
      for OCR-robust token patterns, and a defensible fuzz-budget formula worth stealing.
      `+`/`*` return ALL matches; optional per-key `greedy="FIRST"|"LONGEST"` post-filter.
      (j) **PhraseMatcher** (phrasematcher.pyx): the FlashText algorithm — a trie over
      ONE hashed token attribute, nogil scan, O(tokens × depth), emits all (overlapping)
      matches; patterns are Docs, so pattern and text share one tokenizer and cannot
      disagree; matching on LOWER/NORM gives case-insensitivity for free (OCR ALL-CAPS).
      This is the engine the AU place-name gazetteer idea should copy (→ TODO). Caveat
      from (a): whitespace *tokens* sit in the sequence, so `Wagga  Wagga` (double space)
      breaks trie continuity — normalize whitespace before matching, or match at our
      char level instead.
      (k) **Span/overlap handling**: `util.filter_spans` = precision-first
      winner-take-all — sort by (length desc, start asc), keep a span iff its start and
      end−1 tokens are both unseen, mark the whole range seen (endpoint-only check; the
      same trick as the tokenizer's special-case filter). The documented standard
      alternative to our recall-first union merge — useful vocabulary for the
      overlaps-task write-up, not a replacement. `Doc.char_span` offers
      strict/contract/expand alignment of char offsets to token boundaries (strict
      returns None on misalignment; binary-search token lookup) — spaCy's version of the
      char↔token alignment discipline our ocr.py solves with assembly-time interval
      recording. `SpanGroup`/`doc.spans` is their "keep overlapping spans, resolve
      later" container — the same recall-first philosophy as our merge input.
      (l) **Architecture/engineering practices worth stealing**: (1) the whole pipeline
      is one declarative config.cfg (thinc/confection) — every component/model/
      hyperparameter is a registry reference (`@architectures = "..."`) with `${...}`
      interpolation; a shipped model IS its config + binary weights, and meta.json embeds
      the full per-class eval numbers — self-documenting eval provenance (pii_eval could
      emit a machine-readable results block to live next to the config it measured).
      (2) Models are versioned pip packages with a spacy_version compat range checked at
      load — the packaging answer to "which code can load which artifact". (3) DocBin:
      columnar uint64 arrays + interned-string list, gzipped msgpack, explicitly designed
      so deserialization never executes code (anti-pickle stance for cached corpora —
      relevant to our db/ caches). (4) Vocab/StringStore: murmurhash64 interning, attrs
      are uint64 hashes everywhere, collision risk consciously accepted; a Doc is one
      contiguous TokenC array in an arena (cymem Pool) whose tokens reference shared
      LexemeC structs from the Vocab, with a per-token `spacy` bool making text
      reconstruction lossless. (5) They ship the full test suite in the wheel
      (`pytest --pyargs spacy` runs against the installed build) plus registry snapshot
      files (factory_registrations.json / registry_contents.json) pinning the plugin
      surface — cheap regression nets for a growing codebase. (6) `tokenizer.explain()`:
      a debug mode attributing every token to the rule that produced it — the
      attribution-first debugging pattern our layer-attribution metadata already follows;
      worth extending as the pipeline grows.
      (m) **Production observation** (presidio_analyzer/nlp_engine/spacy_nlp_engine.py):
      Presidio loads the model with plain `spacy.load()` — no component exclusions — so
      every analyzed text pays for tok2vec+tagger+parser+attribute_ruler+lemmatizer+ner.
      With the detector retirement, spaCy's `ner` output is consumed by nobody, and
      `parser` only produces sentence bounds nothing reads (the lemmatizer needs
      tagger+attribute_ruler only). → TODO: benchmark excluding parser+ner from the
      Presidio NLP engine.

- [x] **Joint-name GLiNER2 gap → layer-1 JointNameRecognizer** *(2026-07-15; the
      reversed-caps residual stays in TODO.md)*. The diagnostic (previous entry in git
      history / TODO item) showed the joint forms score 0.93+ in clean context but lose
      span segmentation inside transaction-line junk — glue spans ('LAWRENCE RENT'@0.55,
      initials dropped), split pairs ('BRIAN SUMMERS'@0.98 + 'JULIE'@0.49, connector
      leaks) — i.e. the failure lives exactly where text is machine-regular, so the
      mechanical forms moved to layer 1. `JointNameRecognizer` (pii/recognizers.py,
      emits PERSON): 'A & B Surname' initials pattern @0.5 and 'First and First Surname'
      @0.45 (one pattern covers title-case and ALL-CAPS; mixed case accepted). Scores
      are confident, NOT context-gated — the Presidio context enhancer looks only 5
      tokens back (verified: `LemmaContextAwareEnhancer(context_prefix_count=5,
      context_suffix_count=0)`) and the corpus's 'Online W... Loan to ORG PTY LTD
      <joint>' line puts the name beyond that window. Precision guard: validate_result
      rejects matches containing statement/corporate vocabulary (TERMS AND CONDITIONS
      APPLY, PRINCIPAL AND INTEREST PAYMENT, ANGUS AND ROBERTSON PTY) — accepted
      trade-offs, documented on the class: surnames colliding with that vocabulary are
      sacrificed, and 'X AND Y Z' orgs without a corporate tail get stripped
      (recall-first; the ORGANIZATION over-strip axis watches for creep — it did not
      move: 21 on seed 42 before and after). Results: PERSON_JOINT 1/6 → **6/6** (seed
      42), **18/18** (seed 123); PERSON 100% on both seeds including the previously
      missed 'JULIE AND BRIAN SUMMERS' / 'BRIAN AND AARON MILLER' joint-full draws;
      gate PASS on both. **PERSON_JOINT promoted into pii_eval `build.CRITICAL`**;
      PERSON_REVERSED unchanged (4/6, 6/8) — no mechanical pattern exists for two bare
      caps words, so it stays a per-form probe with its own TODO item. Dual coverage
      per the working agreement: tests/pii/test_joint_names.py (8 model-free tests:
      the diagnostic lines, the beyond-context-window line, stop-vocabulary and
      lowercase-prose negatives) + the existing PERSON_JOINT corpus probes now gated.
      **Review round (same day, Sergei's challenge on the stop-vocabulary trade-off):**
      the sacrificed classes were documented but unmeasured — the corpus generated no
      'AND'-orgs and no colliding surnames. Fixes: (1) the guard went **positional** —
      given-name slots reject statement vocabulary, the surname slot rejects only
      corporate markers, plus a corporate-tail lookahead on both patterns — so real
      colliding surnames (Fee, Card) now strip while 'TAYLOR AND SCOTT LAWYERS PTY
      LTD' / 'HARVEY AND MILLER HOLDINGS' stay kept; (2) dual coverage for every
      trade-off class: `ORGANIZATION_AND` keep-probe (guarded org forms, 7/7 kept
      both seeds), `ORGANIZATION_AND_BARE` keep-probe (the no-marker sacrifice,
      expected over-strips: 0/7, 0/8 kept — measured, not just documented),
      colliding-surname joint draws annotated critical PERSON (a guard regression
      trips the gate), and pytest counterparts (10 model-free tests total). Gate PASS
      both seeds after all additions. **Reversed-caps diagnosis (same round, blob
      probes):** all PERSON_REVERSED leaks are CSV docs; on bare lines GLiNER2 covers
      the form ('LAWRENCE JEFFREY RENT'@0.97 glue) but in the sentinel-joined column
      blob it fails via (a) mention shadowing — the person is detected under their
      canonical first-last mention from another row ('JOSEPH SCHAEFER'@0.93) while
      the reversed mention itself only yields sub-threshold fragments
      ('LAWRENCE'@0.15), unreachable by literal occurrence re-finding — and (b)
      blob-scale label competition — person-only emits 'FULLER CHRISTOPHER'@0.80,
      the production schema 0.33. Adjacent-span coalescing cannot fix either: at the
      production threshold there is nothing near the name to coalesce (checked
      explicitly). The sentinel char itself was ruled out (plain-\n joins reproduce
      the failures). Candidates recorded in the TODO item, led by a known-person
      permutation pass (the DICOM deny-list idea from the presidio-image-redactor
      harvest); the labels-per-pass experiment gained direct rescue evidence.
      **Root-cause round (same day, Sergei's question: was reversed order simply not
      learned?):** No. Probe set 2 (form matrix × context frames, junk-mass ×
      canonical-mention sweep, description steering): reversed order IS learned — a
      10–20-row junk blob without a canonical mention detects 'SCHAEFER JOSEPH'@0.94;
      adding ONE canonical-order row of the same person collapses the reversed mention
      to fragments. The interference requires both orders of the same person in one
      attention window. Canonical order proved robust across name classes (Spanish
      double surnames, particle surnames, Indian multi-word, hyphenated) even inside
      ref-code junk; reversed forms weaken in junk; reversed particle surnames
      ('VAN DEN BERG JAN') fail even bare. Negative result, do not retry: a
      surname-first hint in the person label description LOWERED all scores
      (canonical 0.92→0.53).
      **Fix shipped (2026-07-15): cell-isolation NER windows + PERSON coalescing +
      name-forms statistics doc.** `RECORD_SEPARATOR` (U+241E, defined in
      pii/__init__.py) is now a hard GLiNER2 window boundary — csv_mode's sentinel
      embeds it, so every CSV cell predicts in its own window (cells are independent;
      spans were already clamped per cell, so cross-cell context was pure noise;
      batching through batch_extract_entities is unchanged). The ADDRESS
      adjacent-span coalescing generalized to `_coalesce_adjacent` over
      {ADDRESS, PERSON}: isolated lines emit reversed names as fragment pairs
      ('SCHAEFER' + 'JOSEPH RENT') whose union misses only the joining space —
      coalescing closes it; merging two genuinely distinct adjacent names costs a
      pseudonym wart, never a leak. Statistics (Sergei's requirement: real numbers,
      not n=5 noise): new `pii_eval/nameforms.py` — 32 curated distinct names
      (12 Anglo + 10 particle + 10 multi-word non-Anglo), each drawn once per form
      into a names_*.csv per corpus; fixed per-form n by construction. New per-form
      truth types PERSON_COMMA / PERSON_PARTICLE / PERSON_MULTIWORD (convention
      unchanged: distinct rows, not gated). Results: PERSON_REVERSED **33/35 (s42) +
      37/37 (s123) = 70/72**, PERSON_COMMA 32/32, PERSON_PARTICLE 20/20,
      PERSON_MULTIWORD 20/20, PERSON 100% both seeds, gate PASS; ORGANIZATION
      over-strips *improved* (13→7 on s42) — cell isolation helped merchants too.
      The two residual leaks are label competition on isolated caps lines
      (person-only 'REID'@0.86+'THOMAS'@0.85 vs production org 'REID THOMAS
      RENT'@0.86) — re-owned by the labels-per-pass experiment; a person-names
      database layer was added to TODO as the deterministic fallback (Sergei).
      Watch item: ADDRESS_BARE dropped to 4/7 on the reshuffled s42 draws (was
      11/12) — known un-gated miss class, possibly draw noise vs lost cross-cell
      context; judge on the next few runs. Tests: tests/pii/test_gliner2_windows.py
      (window split at the separator, offset mapping, fragment coalescing,
      distinct-names non-merge; model-free fakes).

- [x] Tesseract docs/config review + pytesseract source review *(2026-07-16; the two prep
      items done as one combined pass, harvest-not-adopt. Pinned stack: Tesseract
      v5.4.0.20240606 (UB Mannheim winget) + leptonica 1.84.1; pytesseract 0.3.13;
      installed `eng.traineddata` is LSTM-only — `--oem 0` fails ("legacy engine ...
      components are not present"), so the engine is pinned to LSTM by the install itself.
      PSM default 3 (full auto, no OSD).
      Docs/empirical findings:
      **(a) The quality driver is x-height in pixels**, not DPI: <10 px poor, <8 px
      "noise removed", LSTM ceiling ~30 px (tessdoc FAQ). Measured our 9 corpus fonts
      (PIL `getbbox('x')`): em 10 → x-height 4–5 px, em 16 → 7–9, em 20 → 9–11 (render.py's
      20 px floor sits exactly on the documented cliff), em 32 → 14–18; a realistic 300-dpi
      scan of 10 pt text is ~42 px em ≈ x-height ~20.
      **(b) The DPI hint is a recognition no-op on the LSTM path** (verified: identical
      output at `--dpi 70/150/300/auto` on 12/16/24 px samples) — and DPI metadata never
      reaches Tesseract from our pipeline anyway: `ocr.py`'s edge-pad builds a fresh
      `Image.new` (PIL info lost) and pytesseract's temp-file re-save writes no pHYs even
      when info is present (verified). Decision: never stamp/pass DPI; glyph pixel size is
      the only size variable.
      **(c)** Reproduced the target error classes at small x-height: "TFN"→"TEN" (F→E flip)
      at em 12–16 Times (x-height 6–7) plus a hallucinated leading glyph at em 12.
      **(d)** Internal binarization is Otsu (5.0+ adds Adaptive Otsu / Sauvola via
      `thresholding_method`); external binarization helps only on uneven backgrounds —
      feeds the preprocessing-knobs task, irrelevant to clean renders.
      **(e)** Borders/skew: needs ~10 px border (our 25 px edge pad already exceeds it);
      dark scan borders get read as characters; skew "significantly" degrades *line
      segmentation* — degradation-phase factors.
      **(f)** PSM candidates for statement layouts: 4 (single column), 6 (uniform block),
      11 (sparse); pipeline ships PSM 3 and the fidelity sweep keeps it pinned — a PSM axis
      is a possible follow-up.
      **(g)** `conf` is word-level only (−1 rows are structural — matches `ocr.py`'s
      filter); LSTM conf calibration is undocumented, so never threshold on it without our
      own numbers — the fidelity sweep records per-word conf against alignment errors to
      measure predictiveness empirically.
      pytesseract 0.3.13 seam findings: round-trip is PIL image → `prepare()` (alpha
      flattened onto white; format defaults to PNG) → temp-file save *without metadata
      kwargs* (the DPI drop above) → subprocess. `image_to_data` DICT coerces every
      non-text cell `int(float(...))` — conf arrives int-truncated (96.06 → 96); the
      last-row-empty-text missing-cell bug is patched upstream; rows shorter than the
      header are skipped per-column, which would desync the parallel lists, but is
      unreachable (Tesseract words never contain whitespace; TSV always emits 12 cells) and
      would crash loudly in our assembly rather than misalign silently — no defensive code
      added. Config strings go through `shlex.split(posix=False)` on Windows — quotes are
      NOT stripped, so config values must stay unquoted (`-c key=value`). Errors: nonzero
      exit → `TesseractError(status, stderr)`; timeout kills the process →
      `RuntimeError('Tesseract process timeout')`. Output files are decoded as UTF-8
      regardless of codepage — safe at our seam (but ad-hoc `subprocess` experiments must
      decode UTF-8 themselves; the cp1251 Windows default bit us during this review).
      Consequences pinned for the OCR-fidelity sweep: analysis axis = *measured x-height*
      per (font, size), not em size; size grid extended past 32 px em toward the realistic
      300-dpi regime (~40–48 px em); no `--dpi`; PSM/OEM at pipeline defaults (3 / LSTM);
      per-word conf recorded per error. Was distilled into an ARCHITECTURE.md section
      ("Tesseract operational profile"); that distillation was removed 2026-08-12 — the backend
      went 2026-07-17 and ARCHITECTURE carries current design, not retired engines. **This
      record is the only home for these facts now.** They are engine-specific and do not
      transfer to PaddleOCR.)*

- [x] OCR-fidelity factor sweep — glyph size × font face, Tesseract findings
      *(2026-07-17; design agreed 2026-07-16, spec preserved in git history of TODO.md.
      Instrument: `pii_eval ocr-report` (pii_eval/ocr_report.py) — renders every corpus
      doc of seeds 42/7/123 at each font × em-size cell, OCRs through the `get_ocr` seam,
      aligns output against the exact drawn text (line-DP with SequenceMatcher costs, then
      char/word Levenshtein with backtrace), buckets every divergence, and appends JSONL
      cells resumably (`pii_eval/reports/`, gitignored). OCR words are re-bucketed into
      geometric visual lines first, so Tesseract's block fragmentation reads as
      `resegmented_lines`, not mass line loss. 1,980 cells; 21 model-free tests.
      Tesseract 5.4.0/LSTM findings:
      **(a) The x-height cliff is measured**: x-height 4–5 px (em 10) is catastrophic and
      font-dependent (CER 4.3% Verdana … 96.5% Courier); 6–7 px is the edge (0.7–7.6%);
      ≥10 px is a flat plateau (prose 0.2–0.6%) with no LSTM ceiling visible up to
      x-height 26. render.py's 20 px floor sits just on the safe side; 300-dpi scans
      (x-height ~20) are comfortably safe; <~150-dpi equivalents are in the cliff.
      **(b) Font face matters only at the cliff**, via x-height ratio per em (Verdana most
      tolerant) and stroke weight (thin Courier collapses first); above x-height 10 all
      nine faces converge.
      **(c) Structure dominates the error mass**: lost_chars 74k is the top bucket
      (cliff-zone line loss); splits 11.5k > merges 7.2k; fixed-column docs run 2–3×
      prose CER at every size. Courier fixed-doc anomaly root-caused: a split explosion
      at em 32–40 (wide monospace letter gaps crossing the word-gap threshold; 527–631
      splits) plus one catastrophic column-loss cell (s42 names_09 @40: 965 alpha
      deletions inside paired lines); recovers at em 48. Reinforces the s42 image-tier
      conclusion — identifiers die of shape/layout damage, not digit misreads.
      **(d) Measured confusion matrix** (top: `0->@` 1674 — Consolas slashed zero,
      `0->O` 1517, `F->E` 964, `5->S`, `J->I`, `1->2`, `0->8`, `J->3`, `4->8`, `W->H`)
      — feeds the `_CONFUSION` refresh task; folklore missed several of the top pairs.
      **(e) conf is a weak error filter at scale** (n=44,354 erroneous words): means 64.3
      erroneous vs 91.8 correct, but 41% of erroneous words carry conf ≥ 80 — the
      no-naive-thresholding ban is now data-backed.)*

- [x] PaddleOCR backend: stack review + adapter *(2026-07-17; Sergei's call to bring the
      second bake-off engine up while the Tesseract sweep ran. Adapter:
      `pii/core/ocr_paddle.py` behind the new `pii/core/ocr.py::get_ocr(backend)` seam
      (backends: tesseract, paddle[:v5_server|:v6_medium]), threaded through
      `strip_image(ocr_backend=)`, `pii strip --ocr-backend`, `pii_eval ocr-report/score
      --ocr-backend`; 14 model-free tests (fake result dicts against `result_to_ocr`).
      Stack: paddleocr 3.7.0 / paddlex 3.7.2 / paddlepaddle-gpu 3.3.1 cu126; models under
      `models/paddlex` via `PADDLE_PDX_CACHE_HOME` (repo convention, set by the adapter);
      tiers pinned PP-OCRv5_server (v5 top) vs PP-OCRv6_medium (v6 ships no server tier).
      Review findings:
      **(a) Line-oriented output**: detection finds arbitrary line regions (no page-layout
      model — the opposite failure profile from Tesseract's block segmentation);
      recognition returns one string + one conf per region. `rec_texts` is authoritative
      for assembled text; `return_word_box` fragments have unreliable boundaries (merged
      tokens like "TFN123") and are used as GEOMETRY only — line words map onto the
      squeezed fragment char stream, boxes union over overlaps, proportional
      interpolation as fallback. Regions band into visual rows by y-center before
      assembly so statement rows reach recognizers as single lines.
      **(b) Windows DLL rules (all verified)**: CPU wheel — torch must import before
      paddle (else torch's shm.dll breaks). GPU wheel — torch and paddle are MUTUALLY
      EXCLUSIVE per process (both bundle cudnn_cnn64_9.dll, different CUDA families;
      second loader gets WinError 127 in either order). Worse, paddleocr's own chain
      hard-imports torch (paddlex official_models → modelscope → torch), so a GPU-wheel
      process installs a permissive torch STUB (package-shaped, __spec__,
      torch.distributed/multiprocessing probes, catch-all __getattr__) after loading
      paddle and before paddleocr — modelscope is satisfied, real torch never loads.
      Consequence: GPU paddle serves torch-free/OCR-only processes (the fidelity sweep);
      the full pipeline (GLiNER2 on torch) pairs with the CPU wheel until the worker-
      isolation task lands. CUDA-version alignment (torch cu126 + paddle cu126) was
      considered and rejected: torch here is cu130, paddle has no cu128 channel, and the
      pairing would pin both stacks forever.
      **(c) pii package inits went lazy (PEP 562)** — load-bearing, not cosmetic:
      `import pii.core.ocr` used to pull pipeline → presidio → spaCy → thinc → torch,
      which would have made every process torch-tainted and GPU paddle unusable.
      **(d) Upstream bugs**: paddle 3.3.x oneDNN PIR executor crashes on PP-OCRv5 server
      (`ConvertPirAttribute2RuntimeAttribute … ArrayAttribute<DoubleAttribute>`) —
      avoided with `enable_mkldnn=False` (CPU path; inert on GPU). The cuDNN 9.9-built /
      9.5-machine warning is demonstrably benign: GPU CER identical to CPU CER on every
      overlapping smoke cell.
      **(e) First numbers** (Consolas em 12/28, s42, vs Tesseract same cells): CER 2–5×
      lower (legacy @12: 2.0% vs 5.9%; tx @12: 0.7% vs 3.2%), biggest wins at small
      glyphs. Speed: CPU 30–95 s/page (server tier, no mkldnn); GPU (2080 Ti sm_75,
      cu126 wheel verified) 0.6–3.5 s/cell on real pages, ~25×. VRAM sits near the 11 GB
      ceiling on the largest renders (Sergei observed 10.5 GB + WDDM spill to shared
      system RAM): paddle's auto_growth allocator caches its high-water mark and returns
      nothing until process exit; detection memory scales with image area (em-48 table
      renders reach ~2600×5000 px). Harmless to correctness, slows only the giant cells
      (~13 s worst); don't run GLiNER2 CUDA jobs concurrently with a paddle sweep; first
      OOM lever is `text_det_limit_side_len` (already on the knobs list). Full three-seed
      sweeps for both tiers + leak-gate comparison tracked in the PaddleOCR TODO item.)*

- [x] OCR bake-off round 1: Tesseract vs PaddleOCR (fidelity sweeps, clean renders)
      *(2026-07-17; full report with all tables in
      [reports/2026-07-17-ocr-fidelity-tesseract-vs-paddleocr.md](reports/2026-07-17-ocr-fidelity-tesseract-vs-paddleocr.md);
      1,980 paired cells per backend, seeds 42/7/123. Distilled: **PP-OCRv6_medium wins
      every axis** — CER 0.2% vs Tesseract 3.5–6.9% (~25×) and v5_server 1.2–1.4% (~6×);
      the Tesseract x-height cliff does not exist for it (0.6% CER at x-height 4–5 px
      where Tesseract loses 45–96%), so the "below ~150 dpi equivalent is unusable" rule
      is Tesseract-specific; structure damage — the class that kills identifiers — nearly
      vanishes (5 lost lines vs Tesseract's 1,649 + 21,960 block-fragmented). Notable:
      v5_server has a word-MERGE pathology (68/10k chars, 2.6× Tesseract — its prose WER
      is worse than Tesseract's despite 2.5× better CER); v6 is 20× cleaner (3.3/10k).
      v6's residual digit risk is almost entirely the 0↔O/o confusion class. Paddle conf
      is NO error filter (99–100% of erroneous words at conf ≥ 80, vs Tesseract's 41%) —
      the never-threshold-on-conf ban now spans both engines (~70k errors of evidence).
      Cost: paddle GPU ≈ Tesseract-CPU speed per page (2.1 vs 1.4 s/cell), hard GPU
      dependency + torch process rules. Per-seed CER flat across corpora — not seed luck.
      Caveats: clean renders, no degradation yet. Decisions (Sergei): retire Tesseract
      (plan in TODO.md), v6_medium becomes the paddle default tier, watch for a future
      PP-OCRv6 server tier, 0↔O post-processing heuristic idea recorded; Surya + local
      VLM in a future session.)*

- [x] **OCR bake-off round 2: Surya 2 — evaluated and retired the same day**
      *(2026-07-17; full findings in
      [reports/2026-07-17-ocr-bakeoff-round2-surya.md](reports/2026-07-17-ocr-bakeoff-round2-surya.md);
      Sergei's decision on the numbers: not worth keeping except as history — the adapter
      was committed working, then removed in the immediately following commit, so a
      revert restores it whole. Sequence: deep source/docs/license review of surya-ocr
      0.21.2 → env phase (transformers 5.6.2→5.14.1 with GLiNER2 re-gate green, pillow
      12.3→10.4 with corpus re-render + paddle-baseline re-take: s42+s123 PASS) → adapter
      (`ocr_surya.py`: detection lines → whitespace-gap segment splitting → per-segment
      VLM OCR via llama-server → HTML flatten with pipe-strip + Unicode-digit folding →
      interpolation; the line→word helpers `_to_box`/`_interpolate`/`_rows` moved from
      ocr_paddle.py to ocr.py as neutral seam machinery and STAY there) → s42 leak gate.
      Verdict drivers: 6→3→5 critical leaks across three temp-0 runs (llama.cpp parallel
      batching makes greedy decode non-reproducible — disqualifying for a gate by
      itself), fabrication/loops from vision-token starvation (fixed by
      `--image-min-tokens 1024` at ~10× prefill cost → >10 min/corpus vs paddle's ~2),
      cross-script digit homoglyphs (U+06F5 for '5' on a clean render), residual digit
      shattering/omission in dense rows. Levers untried and revisit conditions are in the
      report. Also retired the docTR candidate without evaluation (Sergei: no expected
      gains; Apache-2.0 fallback if licensing ever matters). The one-pass-VLM TODO item
      inherits the operational lessons (vision-token floor = correctness knob,
      homoglyph folding, determinism requirements, llama-server attach/cleanup
      patterns).)*

- [x] **Retire the Tesseract backend** *(2026-07-17; Sergei's decision on the round-1
      report — Tesseract clearly inferior on every measured axis. Executed the ordered plan
      from TODO.md, each step gated on the previous.*
      **Step 1 — v6_medium default.** `ocr_paddle.DEFAULT_TIER` v5_server → v6_medium (the
      report verdict). One line + docstrings.
      **Step 2 — paddle worker-process isolation (the core of the task).** The GPU paddle
      wheel and torch cannot share a Windows process (bundled-cudnn mutual exclusion, the
      PaddleOCR DONE record above); with Tesseract gone, the image pipeline must run both
      (GLiNER2 on torch + paddle for OCR), so paddle moved into a **persistent worker
      subprocess**: `pii/core/ocr_worker.py`, spawned lazily per model tier and kept alive
      for the run, engine loaded once. Protocol: framed PNG-in / pickled-`OcrResult`-out
      over the child's stdio. Design decisions (full rationale in ARCHITECTURE.md "Paddle
      worker-process isolation"): (a) **routing by wheel, not torch-load timing** —
      `ocr_paddle.make_paddle_ocr(tier)` returns the worker callable on the GPU wheel, the
      in-process partial on the CPU wheel; the image pipeline OCRs *before* it runs NER, so a
      "is torch imported yet" check would wrongly pick in-process, so the decision is by
      wheel and order-independent (CPU-wheel + torch-free fidelity sweep keep the fast direct
      path); (b) **fd 1 claimed for the protocol, Python+C stdout redirected to stderr and
      both fds forced binary before paddle imports** so paddle's logging can't corrupt the
      stream; (c) **crash surfacing** — a dead child closes the pipe → short read raises →
      client raises `RuntimeError` with the exit code (never hangs); a `READY` startup
      handshake turns engine-load failure into a spawn-time error; a per-image exception is
      an error frame and the worker keeps serving (one bad page ≠ dead engine); (d) **client
      side stays torch-safe** — `ocr_worker.py` module level is stdlib-only, the paddle
      import lives in `main()` reached only as `python -m pii.core.ocr_worker <tier>`
      (regression-tested). The existing torch-stub trick (`ocr_paddle._stub_torch`) runs
      inside the worker via `_engine`, keeping paddleocr's modelscope import happy.
      **Step 3 — leak-gate parity.** `score --modality image --ocr-backend paddle:v6_medium`
      through the worker on seeds 42 + 123 (rendered image corpora). Result: **both PASS
      (zero critical misses)**. Baseline for comparison: Tesseract **s42 FAILED** (2 critical
      leaks: `AU_TFN` 565 431 023 fuzzy, `PERSON` ISLA FERGUSON) and **s123 PASSED**. Paddle
      is parity-or-better on both — strictly better on s42. The report's thesis reproduced at
      the pipeline level: paddle's clean OCR fixed the *structure-damage* leaks, not just
      glyphs — s42 PERSON_COMMA 12% → 100%, PERSON_REVERSED 31% → 100%, PERSON_PARTICLE 90% →
      100%, AU_DRIVERS_LICENCE 75% → 100%. Residual non-gated per-form misses (s123
      PERSON_COMMA 88%, PERSON_REVERSED 89%, LOCATION_SHORT, ADDRESS_BARE) are the
      pre-existing detection-layer gaps in TODO.md, unchanged — not OCR-caused, not
      regressions.
      **Step 4 — degradation-tier check: WAIVED** (Sergei, 2026-07-17). The degradation
      instrument (noise/skew/JPEG) does not exist yet and Sergei directly instructed to
      retire Tesseract from the codebase now; the waiver is deliberate. Engine ranking under
      degradation is deferred to bake-off round 2 (Surya/VLM), which will re-benchmark on the
      degradation tier when it lands.
      **Step 5 — removal.** Deleted the Tesseract adapter path from `pii/core/ocr.py`
      (`ocr_image`, `_lines_from_tesseract`, `_ensure_tesseract`, `_TESSERACT_DEFAULT`, the
      `shutil` import, the edge-pad workaround); `OcrResult`/`assemble`/`get_ocr` kept — they
      were always the seam, never Tesseract-specific. `pytesseract` removed from
      `pii/requirements.txt` (`paddleocr` added; the `paddlepaddle` wheel stays unpinned,
      chosen per machine); it is NOT uninstalled from the env. `tesseract` removed from
      `OCR_BACKENDS`; `get_ocr`, `strip_image`, and every `--ocr-backend` default (CLI,
      `pii_eval score`/`ocr-report`, `score_image`) flipped to `paddle`. Docs updated:
      `pii/README.md`, `pii/core/ARCHITECTURE.md` (new worker-isolation + retirement
      decisions; the "Tesseract operational profile" section kept and marked HISTORICAL),
      umbrella/cli/root doc pointers, `pii_eval/README.md`. Historical Tesseract records in
      DONE.md and `reports/` are untouched.
      **Env/wheel state:** unchanged from the bake-off — `paddlepaddle-gpu 3.3.1` (cu126
      class), `paddleocr 3.7.0`, `paddlex 3.7.2`, `torch 2.13.0+cu130`, `pytesseract 0.3.13`
      still installed but now unused by the code. No wheels installed/swapped for this task.
      **Tests:** removed the Tesseract-specific tests (`_lines_from_tesseract`, the
      `ocr_image` / `strip_image` Tesseract e2e); rewrote `TestGetOcr` (paddle is default,
      `tesseract` now an unknown backend); converted the render OCR-readable test to paddle
      (gpu-marked). New `tests/pii/core/test_ocr_worker.py`: 10 model-free tests (framing
      round-trip, `_serve` happy/bad-image/exception-non-fatal via fake streams, client
      happy/per-image-error/startup-failure/dead-worker via inline `python -c` children, and
      a torch-free-import check) + one gpu+slow real-paddle-worker e2e. Dual-coverage rule
      honored: the worker crash/isolation behaviour has both pytest coverage and the leak-gate
      probe above. Fast suite 138 passed / 7 deselected; the gpu worker e2e passed (17 s).
      **Open watch item:** both models hold VRAM during pipeline runs (worker paddle + parent
      GLiNER2 on one 11 GB GPU) — fine for page renders, first OOM lever is
      `text_det_limit_side_len`; carried into the knobs TODO.)*
- [x] Policy for GLiNER2's numeric-ID *guesses* (2026-07-14, length-heuristic discussion).
      Diagnostic on the tier-1 corpus: nearly every short false positive is GLiNER2 labeling
      a numeric-ID type that layer-1 already owns with a checksum — `'42'` as AU_BANK_ACCOUNT,
      `'K3EN5L'` / `'TAS 2628'` as AU_TFN. A LOCATION-style char-length floor is the wrong
      instrument (TFN FPs are non-numeric junk; the real fix is format/digit-count) AND must
      NOT be applied to PERSON or ORGANIZATION — real short surnames (Wu, Ng) and bank
      acronyms (NAB, ANZ, BHP) live there, so a floor is a leak risk / pointless respectively
      (confirmed with Sergei). Cleaner single lever than N per-class floors: constrain
      GLiNER2's numeric-ID emissions — either drop those labels (layer-1 validates them) or
      route each guess through its layer-1 checksum recognizer before it may strip.
      *(2026-07-22 — SHIPPED as identifier post-validation, driven by review issue #10:
      on the real dd24ae14 NAB statement GLiNER2 labeled letter+10-digit bank receipt
      references semi-randomly as TFN (8), driver licence (4 + the 'Australian Credit
      Licence 230686' phrase — review other-finding #1) and passport (2); a bogus 22-digit
      AU_BANK_ACCOUNT guess on the Amplify statement also over-extended a credit card via
      `_merge_overlaps` (issue #6's recorded side effect). Implementation:
      `gliner2_recognizer.IDENTIFIER_VALIDATORS` — per-type validators run where the
      2026-07-14 account floor ran; checksum arithmetic extracted from
      `invalid_recognizers.py` into the shared `pii/core/checksums.py`. Rules: AU_TFN
      9 digits + mod-11 (legacy 8-digit passes structurally — no reliable public checksum
      variant, and layer-1's 9-digit pattern can't cover them, so demotion could leak a
      real one while an FP merely over-strips); AU_MEDICARE 10-11 digits, first digit 2-6,
      mod-10; AU_BANK_ACCOUNT 5-16 digits (floor + BSB-prefixed cap); PASSPORT ≤ 9 digits;
      AU_DRIVERS_LICENCE ≤ 10 alnum chars. Disposition — Sergei's option (b): shape-correct
      checksum failures DEMOTE to `*_INVALID` and join the shadow-recognizer findings;
      structurally impossible guesses plain-drop; under the `ignore` tier demotion is off
      (`Gliner2Recognizer(demote_invalid=False)`, wired from `invalid_identifiers`).
      Last-4 stance settled: masked disclosures ('card ending 1234') are NOT strip-worthy —
      digit floors drop them, consistent with layer-1; CARD_LAST4 keep-probe added per the
      2026-07-15 corpus note, alongside REFERENCE_NUMBER (letter+10-digit receipt shape,
      both a guaranteed loan-doc probe and a txbank statement pattern) and DIGITS_OVERLONG
      (22-digit run). The loan template now always renders a trustee line (trust-name
      presence in the corpus was coincidence-dependent on pool draws and the added rng
      consumption shifted seed 42 past it). Verified: dd24ae14 fresh-map run shows ZERO
      junk identifier detections (was 14) with every legit detection intact; Amplify shows
      the 22-digit account and licence-phrase junk gone, BPAY Ref still CARD. Trade-off
      accepted: an unlabeled, ungrouped OCR-mangled real TFN in free text is now dropped —
      indistinguishable from the junk population; labeled/grouped/context cases remain
      covered by the shadow recognizers. Tests: 7 model-free validator tests in
      `tests/pii/core/test_gliner2_floors.py` (checksum keeps/demotions, demote-off wiring,
      digit caps, licence/passport structure); fast suite 189, model suite 8 incl. tier-1
      gate — all green.)*
- [x] **OCR perception layer + PP-StructureV3 backend + `debug ocr` diagnostics** *(2026-07-24 —
      design + rationale in ARCHITECTURE.md "OCR perception layer"). A rethink-the-problem
      session with Sergei: the flat `OcrResult` is too thin to reason about grouping, so the OCR
      output became a typed engine-neutral hierarchy `OcrPage → OcrBlock → OcrLine → OcrWord`
      (+ `OcrFrame`), with the linearization/offset concern split out into `RecognizerInput` /
      `linearize`. Brainstorm decisions as they settled: standard OCR hierarchy (not a
      paddle-specific shape); perception carries no char offsets (an offset is a (page, assembly)
      property — we intend multiple trial linearizations, "feed the recognizer per block" the
      leading hypothesis); block mandatory / `block_id` total (orphan lines → own synthetic
      block, never dropped = never leak); `region_box` per-word; names — `OcrBlock` (not
      `OcrRegion`, which already means "line" in the paddle code), `origin: detected|synthetic`,
      `conf_scope` dropped in favour of `conf: float|None`. Presidio's tokenizer vs our word
      split confirmed orthogonal (geometry vs the lemma context enhancer).

      **PP-StructureV3 adopted** (interactive install session): the stack was mostly present
      (paddleocr 3.7, paddlex 3.7.2, paddlepaddle-gpu 3.3.1); it needed the `paddlex[ocr]` extra
      — installed the 9 missing benign deps explicitly (einops/ftfy/latex2mathml/lxml/openpyxl/
      premailer/scikit-learn/scipy/tiktoken; dry-run confirmed additive — nothing touched
      paddle/torch/opencv). First construction failed because scipy (new in the tree) does
      `issubclass(x, torch.Tensor)` and the `_stub_torch` `Tensor` was an `_Anything` instance,
      not a class → fixed the stub to present `Tensor` as a real empty class. Layout models
      (`PP-DocLayout_plus-L`, `PP-DocBlockLayout`) downloaded into `models/paddlex`; the stale
      5.3 GB `~/.paddlex` from Oct-2025 experiments is untouched/reclaimable. Config: lean
      (table/formula/seal/chart/orientation off), OCR sub-models pinned to v6_medium.

      **Key measured finding — no line→block linkage.** On the ANZ policy page (9 OCR lines, 4
      layout blocks) PP-Structure's parsing blocks carry only `content` + `bbox` + a
      `num_of_lines` count; `child_blocks` is empty for text blocks. So line→block is
      reconstructed by geometric containment — which reproduced the reported `num_of_lines`
      exactly (2, 2, 4, 1) with no orphans on that page. `child_blocks` (table cells) matters
      only if table-structure recognition is enabled, which we don't — tables come through as
      normal OCR lines under a `kind="table"` block (revisit later).

      **Transport:** chose to *not* special-case debug in-process; `get_ocr_page` mirrors
      `get_ocr` (wheel-selected — worker on GPU, in-process on CPU) so debug and the future strip
      migration share one implementation and debug exercises the release transport. Worker
      generalized to a spec dispatch (`_resolve`: bare tier → OcrResult, `page:<tier>` /
      `structure` → OcrPage), `worker_page` added, pool shared; the strip `OcrResult` path is
      untouched. Live end-to-end validated: parent stays torch-free, PP-Structure runs in the
      worker, pickled `OcrPage` back with the right block structure; `pii debug ocr` text and
      overlay run end-to-end on the ANZ page (overlay 1241×1754).

      Modules added: `ocr_page.py`, `linearization.py`, `ocr_ppstructure.py`, `ocr_debug.py`,
      and `paint.py` (the `Segment`/`paint_segments`/fill/frame drawing toolkit extracted from
      `image_mode` so the OCR-only debug path doesn't pull the analysis stack — `image_mode`
      re-exports the names, strip/eval untouched); `ocr_paddle.py` / `ocr_worker.py` / `ocr.py`
      / `pdf_mode.py` (`rebuild_pdf`) / `cli` extended. `debug ocr` does PDFs end-to-end: all
      pages by default, `overlay` to a `.pdf` reconstructs a fresh image-only PDF via
      `rebuild_pdf` (strip's reassembly, not redacted — near-PII). Verified live on real
      statements (`sensitive/statements/1/`): PP-Structure clusters PII into coherent blocks
      (a whole address block, a BSB/account/names block), tags the balance summary `table`, and
      splits the ANZ legal line into `footer` — real support for per-block feeding. Tests (all
      model-free): `test_linearization.py`, `test_ocr_ppstructure.py`, `test_ocr_debug.py`,
      `test_pdf_mode.py::rebuild_pdf`, `TestResultToPage` / `TestGetOcrPage`, debug-CLI guards;
      fast suite 241 green (was 216). Strip migration onto `OcrPage` / `RecognizerInput` and the
      per-block feeding experiment are in TODO.)*

- [x] Root-cause orphaned OCR lines under PP-StructureV3 *(2026-07-25: investigation only — the
      knob was found, measured and deliberately **not** adopted; `_layout_thresholds` in
      `ocr_ppstructure.py` is the documented seam, overriding nothing. Trigger: the whole
      right-hand summary panel of a real Bank of Melbourne statement
      (`AmplifyBusiness-…-24Sep2023.pdf` p1) arrived as 22 one-line synthetic blocks.

      **Not our linkage.** `_assign` already falls back from centre-containment to
      largest-overlap, so an orphan means the line overlaps *no* block at all: PP-DocLayout
      emitted nothing over that panel. Re-running detection at a low cut showed it had, in
      fact, seen it — `text` 0.383 (top group), `text` 0.336 (shaded group), `table` 0.334
      (panel only), `table` 0.394 (whole header band) — all discarded.

      **The cut is per-CLASS, not the flat 0.5 the float knob implies.**
      `paddlex/configs/pipelines/PP-StructureV3.yaml` ships
      `threshold: {paragraph_title: 0.3, text: 0.4, seal: 0.45, rest: 0.5}` plus
      `layout_nms: True`, `layout_unclip_ratio: [1.0, 1.0]` and a per-class
      `layout_merge_bboxes_mode`. That dict predicts every survivor and casualty on the page
      (`text` 0.408/0.433/0.495 kept, `text` 0.383 dropped by 0.017, `table` 0.404 and 0.394
      dropped, `footer` 0.443 dropped) — confirmed in effect. Nothing rescales the score:
      `nms()` only drops boxes, so the low confidence is the model's own opinion, plausibly
      because `PP-DocLayout_plus-L` resizes every page to 800×800 with `keep_ratio: false`
      (a 1653×2337 page is squeezed ×0.48 wide but ×0.34 tall, so 25px key/value rows land at
      ~8px). Raising render DPI therefore cannot help.

      **Head-to-head, 31 pages of `sensitive/statements/`** (orphan lines / detected blocks —
      block count is the coarsening guard): shipped **59 / 393**; `text` 0.33 **19 / 398**;
      `text` 0.30 17 / 394; `text` 0.33 + `table` 0.40 18 / 388. Relaxing `text` alone removes
      68% of orphans while block count goes *up* — no page loses half its blocks. The flat
      float is the trap: it replaces all 20 classes at once (lowering `table` while *raising*
      `paragraph_title` from 0.3), which is why a flat sweep reads non-monotonically —
      0.5→0.35→0.3 gave 24→27→12 blocks on p1, the 12 being a band-wide `table` swallowing the
      address block and the whole payment slip merged into one 33-line `table`. Merge modes
      and `layout_unclip_ratio` move nothing on their own (unclip 1.5/2.0 left 19/18 orphans);
      `layout_merge_bboxes_mode` is however *non-monotone* — admitting a box can delete an
      existing one — and `thr 0.3 + "small"` recovers the tight panel-only `table` (16 lines)
      instead of the band. With `text` 0.33 the panel becomes two ordinary `text` blocks and
      p1 orphans go 22 → 2 (`MR SERGEI KULIK`, a footer code), verified end-to-end through
      `pii debug ocr`.

      **Upstream (checked 2026-07-25, paddlex 3.7.2 = latest, paddleocr 3.7.0):**
      `PP-StructureV3.yaml` on `develop` is identical to ours, and the linkage code is
      unchanged — OCR→layout conversion is still guarded by `if len(layout_det_res["boxes"])
      == 0`, so there is still **no per-line orphan rescue** upstream (the nearby loop is the
      reverse case: a *block* with no matched OCR gets cropped and re-recognized). Our
      synthetic-block net stays load-bearing. Correction to the 2026-07-24 note above:
      `child_blocks` is not unused — it is populated by
      `update_{doc_title,paragraph_title,vision,region}_child_blocks` for title↔body and
      region grouping, never line↔block, and `get_child_blocks()` *clears* the list when read.
      Geometric containment remains the only route to line→block.

      **Table structure recognition would not change blocks** (asked 2026-07-25): the table
      pipeline is called with `use_layout_detection=False` and is handed `layout_det_res`
      (`pipeline_v2.py`), blocks are still built one-per-detection, and it receives a
      `deepcopy` of `overall_ocr_res`, so neither our blocks nor our lines move. Its only
      effect is `block.content = pred_html` for `table` blocks — which our adapter never reads
      — and it *skips* `update_text_content`, leaving `num_of_lines` at its `__init__` default
      of 1 and thus blinding our containment cross-check on exactly those blocks. Four extra
      models for a net negative; stays off.)*

- [x] **Second layout backend `doclayout:v3` (PP-DocLayoutV3) + layout bake-off** *(2026-07-25 —
      design in ARCHITECTURE.md "Second layout backend"; numbers in
      [reports/2026-07-25-layout-bakeoff-doclayoutv3.md](reports/2026-07-25-layout-bakeoff-doclayoutv3.md)).
      Sergei's question: does a newer layout model detect statement tables better than the one
      PP-StructureV3 ships with? Answer on the 31-page real corpus: yes, decisively — table
      blocks 24 → 45, orphan lines 117 (6.2%) → 20 (1.1%), 4.5 vs 4.7 s/page.

      **Built:** `pii/core/ocr_doclayout.py` (PP-DocLayoutV3 blocks via
      `paddleocr.LayoutDetection` + lines via a direct `PaddleOCR` call), wired through
      `get_ocr_page`, `OCR_PAGE_BACKENDS`, the worker spec `doclayout:<model>` and
      `--ocr-backend`. **Adopted as the default** on these numbers (Sergei, same day);
      `ppstructure` stays selectable as the baseline, and strip — still on
      `get_ocr`/`OcrResult` — is untouched by the switch. Two shared
      extractions came out of it: `ocr_page.build_layout_page` (line→block containment, orphan
      synthetic blocks, emission order — the "never drop a line" invariant now in ONE place for
      both layout backends) and `ocr_paddle._result_lines` (paddle result → lines, used by the
      line-only path via `_result_to_rows`, by PP-Structure's `overall_ocr_res`, and here).

      **The measurement trap, worth remembering:** the first run had V3 *losing* (383 orphan
      lines; zero blocks on four `d11` pages). Root cause — standalone `LayoutDetection` with no
      threshold falls back to `draw_threshold: 0.5` out of the exported `inference.yml`
      (`layout_analysis/predictor.py:164`), a *visualization* default, while PP-StructureV3
      hands plus-L a tuned per-class dict. Tuned-vs-untuned. Fixed by `_shipped_knobs`, which
      lifts `threshold`/`layout_nms`/`layout_unclip_ratio`/`layout_merge_bboxes_mode` from the
      pipeline config that NAMES the model (newest first, `PaddleOCR-VL*.yaml` → V3 gets 0.3 +
      NMS + unclip + merge modes). Matching by model name is also what makes the index-keyed
      dicts safe to reuse — they are keyed by that model's own class list.

      **Threshold sweep** (V3, other knobs shipped): 0.1 → 447 blocks/54 tables/2 orphans;
      0.2 → 501/48/2; **0.3 → 537/45/20**; 0.4 → 484/35/205; 0.5 → 403/29/385. Block count is
      non-monotonic — below 0.3 more candidates survive to be merged by NMS + `union`/`large`
      modes, so blocks grow bigger and fewer while coverage improves. Recall cliff between 0.3
      and 0.4; shipped 0.3 kept (judging over-merge needs block ground truth we lack).

      **Other findings:** reading order is the result list's ORDER, not the `order` field —
      paddlex sorts by the model's reading-order column then blanks `order` for every
      `SKIP_ORDER_LABELS` entry (`table`, `image`, `header`, `footer`), so ranking by it would
      sort every statement table last (regression-tested). Line counts differ by 6 across the
      corpus: PP-Structure's internal OCR feed fragments lines (`'Pa'` + `'Page 1 of'` vs
      `'Page 1 of 1'` on d08.p1), every difference favouring the direct call. V3 also emits
      per-block `polygon_points` (not stored — `page_to_dict` doesn't serialize block polygons,
      so filling the field would break the JSON round-trip) and per-block scores (stored as
      `OcrBlock.conf`). Model `PP-DocLayoutV3` downloads into `models/paddlex`; it is a
      dynamic-graph safetensors model, ~0.1 s/page on the 2080 Ti, torch-free (paddlex's own
      transformers port) so the worker isolation is unchanged. 13 new tests (model-free
      fixture captured off the same ANZ page as the PP-Structure fixture).)*

- [x] **`OcrLine.box` must contain its glyph ink — the two layout backends disagreed**
      *(2026-07-27, Sergei: "line bounding boxes became too tight after changing the backend".
      Reproduced on p2 of `Statements - 1114.pdf` at 200 dpi; design now in ARCHITECTURE.md
      "A line box contains its glyph ink".)*

      **Root cause — a word-box *source* difference, not a layout-model geometry change.**
      `OcrLine.box` was the union of the line's WORD boxes. Which boxes those are depends on
      whether the paddle result carries fragments: `ocr_ppstructure._normalize` flattens the
      pipeline result to `rec_texts/rec_scores/rec_boxes/rec_polys` and drops
      `text_word`/`text_word_boxes`, so `_region_words` falls back to `_interpolate`, whose
      words tile the full detection region — the line box equalled the region box *by accident*.
      The doclayout path goes through `ocr_paddle._predict`, which passes
      `return_word_box=True`, so real fragment boxes exist and are used — and those are inset
      from the glyph ink (already documented on `painted_boxes_for_span`, measured 2026-07-21).
      Switching the default backend therefore surfaced the inset on the `OcrPage` path for the
      first time; nothing got tighter, the loose box had just been an artefact of interpolation.

      **Measured** (walk outward from each box edge while ink continues, so a neighbouring
      row can't contaminate — `scratchpad/ink2.py` method), 53 lines on that page:

      | | lines losing ink | mean spill L,T,R,B |
      |---|---|---|
      | `doclayout` line box, before | **50 / 53** | 5.2, 0, 3.5, 0 |
      | `doclayout` word `region_box` | 0 / 53 | 0, 0, 0, 0 |
      | `ppstructure` line box | 0 / 53 | 0, 0, 0, 0 |
      | `doclayout` line box, after | **0 / 53** | 0, 0, 0, 0 |

      Purely horizontal (vertically the fragments match the region exactly), up to 8 px — about
      half a glyph at 200 dpi, so the first and last characters were sliced.

      **Fix:** one helper, `ocr_page._line_box(words)` = union of the word boxes *with* their
      region boxes, called by both `build_page` and `build_layout_page`, so every backend
      produces the same rectangle for the same line. Union rather than region-alone because of
      the ea9e056 case — paddle sometimes emits a region that does not contain its own words
      (a footer line in `ServletRetrieve (6).pdf`), and `painted_boxes_for_span` already clamps
      against the word extent for exactly that reason; the line box can now never end up
      narrower than today's. **Verified end-to-end:** ink loss 50/53 → 0/53, and all 45
      distinct lines on the page now have byte-identical boxes under `doclayout:v3` and
      `ppstructure` (before: all 45 differed).

      **Blast radius was diagnostics-only** — painting grows runs out to `region_box`
      independently, `_assign` is called with the raw detection region box *before* the line
      box is computed, and strip is still on `get_ocr`/`OcrResult`. But it sits on the seam
      strip is about to migrate onto. 9 new tests: `tests/pii/core/test_ocr_page.py` (new file
      for the shared builders — ink containment, banded multi-region rows, glyph-tight
      backends, the stale-region guard, orphan/synthetic-block agreement) plus a
      fragments-vs-interpolated equality test in `test_ocr_paddle.py`; the doclayout
      `test_words_carry_line_region_box` assertion was a tautology
      (`w.region_box == line.box or w.region_box is not None`) and now asserts the real
      equality.

- [x] **Per-block recognizer feeding (`--feed blocks`) + the strip path onto `OcrPage`**
      *(2026-07-27 — step 2 of Sergei's session plan; design in ARCHITECTURE.md "Per-block
      recognizer feeding", full evidence in
      [reports/2026-07-27-per-block-feed-bakeoff.md](reports/2026-07-27-per-block-feed-bakeoff.md)).*

      **Sergei's calls before implementation:** measure **full** isolation (separate analyzer
      calls per block, not the cheaper `RECORD_SEPARATOR` sentinel, which would leave
      Presidio's context enhancer reaching across boundaries); wiring the strip path onto
      `OcrPage` is in scope; and keep it **dumb** — every block is a unit, no grouping
      heuristics, "we'll start from plain per-block and see".

      **Built:** `linearization.linearize_blocks(page)` (one `RecognizerInput` per block,
      block-local offsets, blocks in first-line-appearance order) and
      `linearization.rebase(inputs)` (concatenate + report each part's offset;
      `rebase(linearize_blocks(page))` is byte-identical to `linearize(page)`, asserted).
      `RecognizerInput` gained `block_id`; the placing loop became `_assemble`, shared by both
      linearizations. `image_mode.strip_from_page(image, page, …, feed=)` is the new strip
      seam over `OcrPage` — detect per part, rebase spans *and* invalid findings into
      page-global offsets, then one painting pass, so placeholder numbering and every
      downstream consumer are indifferent to the feed. `strip_image`/`strip_pdf` take `feed`
      and route flat-vs-page; CLI `strip --feed {page,blocks}` with `--ocr-backend` widened to
      the `OcrPage` backends; `pii_eval score --feed` likewise, with `score_image.reread_engine`
      pinning the *read-back* to the flat default tier so the measuring instrument stays
      constant while the strip side varies. A block with no recognized characters is skipped
      as an analyzer call but still occupies its place in the rebase.

      **Measured on the real corpus** (11 docs / 31 pages / 172 authored truth entities,
      `score --modality pdf`), three configs — the middle one exists to separate perception
      from feed:

      | | backend / feed | critical leaks | wall |
      |---|---|---|---|
      | a | `paddle` / page (today's default) | 9 | 301 s |
      | b | `doclayout:v3` / page | 12 | 401 s |
      | c | `doclayout:v3` / **blocks** | **8** | 402 s |

      **The feed is worth −4 leaks against its own control at zero time cost** (AU_BSB recall
      67 → 100%, ADDRESS 95 → 100%, PERSON_JOINT 43 → 71%, LOCATION 0 → 50%; +1 institutional
      AU_ABN over-strip, the known keep-list gap). Wall time is flat despite ~17× more
      analyzer calls per page: OCR dominates, and a quadratic-attention encoder is roughly
      indifferent between one long window and many short ones. The predicted cost of isolation
      — a label in one block no longer promoting a value in the next — is real and pinned by a
      unit test (`BSB` + `014-936` in separate blocks detect nothing; one page detects both),
      but did not bite on the corpus: V3 puts those panels inside one `table` block, while the
      interference the feed removes (a header panel sharing an attention window with 40
      transaction rows — the documented GLiNER2 word-order interference) is everywhere.

      **Perception alone regresses (a → b, +3 leaks), and that decided the default.** Lines
      inside a block are emitted in `(top, left)` order, so a multi-column header panel
      interleaves: on `d11.p2` the account value is emitted *before* its own label
      (`': 162-097111-4' / 'THE DIRECTOR' / 'Account Number' / '23JUN22' / …` — all 15 lines in
      one correctly-detected `table` block), so `AuAccountNumberRecognizer`'s context promotion
      never fires. The flat path only gets this right because `_rows` bands side-by-side
      detection regions into one visual line. Same defect as review issue #8a from the other
      side.

      **Default flipped to `doclayout:v3` + `--feed blocks` the same day** (Sergei's call on
      these numbers, after the recommendation had been to hold): `strip --image`/`--pdf`, the
      `strip_image`/`strip_pdf` signatures and both eval scorers, so the harness measures what
      ships. Net against the previous default that is 8 critical leaks vs 9 — the `d11`
      account leak and 2 extra ORGANIZATION over-strips are accepted knowingly as
      perception-level debt, to be repaid by intra-block column structure rather than by
      reverting the feed. The flat `OcrResult` path stays reachable behind an explicit
      `--ocr-backend paddle --feed page`, and the two reassembly-contract tests in
      `test_pdf_mode.py` pin it there. **Verified after the flip** by re-running both tiers
      with no flags: the PDF tier reproduces config `c` byte-for-byte, and the synthetic image
      tier is a wash on leaks (6 → 6, composition shifted) with CONTEXTUAL_ID 25 → 50%,
      LOCATION/LOCATION_SHORT 75 → 100% and invalid-identifier noise 2 → 0 — single-column
      renders give layout structure little to bite on, as expected.

      15 new tests (`test_linearization.py` block/rebase contract incl. page-global line
      numbers and empty-block handling; `test_image_mode.py` `strip_from_page` under both
      feeds incl. the isolation-cost pin; `test_pdf_mode.py` the routing, both feeds and a
      guard that the default never reaches `get_ocr_page`). Fast suite 291 green.

- [x] **presidio 2.2.363 → 2.2.364; ABN leading-zero checksum re-sync** *(2026-08-08, prompted
      by Sergei spotting "Fix AU ABN accepting some invalid leading-zero numbers" in the
      upstream notes)*. Verified in the shipped wheel, not just the changelog:
      `AuAbnRecognizer.validate_result` replaced `abn_list[0] = 9 if abn_list[0] == 0 else
      abn_list[0] - 1` with the plain ABR `abn_list[0] - 1`.

      **The fix does not do what its own upstream comment claims.** That comment asserts a
      leading 0 "makes the weighted sum non-zero mod 89 and correctly fails". It does not: with
      −1 in the lead the sum can still land on 0 mod 89. Measured over 400k random leading-zero
      11-digit values — old 4530 accepted (1.1325%), new 4579 (1.1447%), **overlap 0**. The
      false-positive class is not removed, it is *replaced* by a disjoint set of nearly equal
      size. Over 200k non-leading-zero values the two agree exactly (0 differences), so no real
      ABN is affected — real ABNs cannot lead with 0, and `pii_eval.au.abn` can only emit first
      digits 1-9 (`need // 10 + 1`, `need` ≤ 88).

      The upgrade's real risk was ours, not upstream's: two copies of the *old* arithmetic
      (`pii/core/checksums.py:abn_checksum`, feeding `InvalidAuAbnRecognizer`; and its
      `pii_eval/au.py:abn_valid` mirror, whose comment read "as presidio computes it"). AU_ABN
      and AU_ABN_INVALID partition the 11-digit space, so leaving them unsynced splits every
      leading-zero value into one of two buckets — presidio-accepts/we-say-invalid (stripped
      *and* spuriously reported invalid, benign) or presidio-rejects/we-say-valid (**neither
      stripped nor collected — a silent leak**, since AU_ABN_INVALID is not in
      `DEFAULT_STRIP_ENTITIES` and only feeds `InvalidFinding` reporting). Both copies synced to
      the plain subtract-1.

      Coverage per the dual-coverage rule: `test_abn_checksum_tracks_presidio_exactly` pins our
      checksum against the live `AuAbnRecognizer` over 4k values plus four discriminating
      literals (`06700094948`/`00238288185` pass only ≤ 2.2.363; `08737167868`/`01039931582`
      only ≥ 2.2.364) — it fails on 40 of those inputs under the old logic, so it is not
      vacuous, and it will catch any future upstream drift rather than a snapshot of one side.
      Plus an end-to-end seam test and a corpus probe (`au.abn_leading_zero`, 4 per tier-1 run,
      100% stripped). Full suite 303 green (fast + slow + model); wheel diff showed 26 changed
      analyzer files (registry loader, `entity_recognizer`, phone/date recognizers) with no
      fallout, anonymizer effectively untouched.

      **Incidental finding — the tier-1 gate is seed-fragile on PERSON.** The probe initially
      drew from the shared `pool.rng`, which shifted every downstream draw and re-rolled the
      whole seed-42 corpus; the gate then failed on a GLiNER2 PERSON miss (1 leaked, 1 partial)
      unrelated to ABN. On *unmodified* code the gate already fails at seeds 2, 3 and 7 and
      passes at 42 and 1 — so seed 42 passing is partly luck, and any change that perturbs the
      draw sequence re-enters the lottery. Also confirmed `CONTEXTUAL_ID` sits at 0% recall at
      every seed but is not in `build.CRITICAL`, so it never trips the gate. Fixed here by
      deriving the probe's Random from already-drawn values (`biz.abn ^ acct.number`) instead of
      consuming the shared stream — new probes should be additive, or every historical eval
      number becomes incomparable. Widening `CRITICAL` / de-flaking PERSON recall is open work.

- [x] **One-pass VLM: evaluated on the real corpus, and layer 0 shipped opt-in**
      *(2026-08-08; full record in
      [reports/2026-08-08-vlm-oneshot-qwen36.md](reports/2026-08-08-vlm-oneshot-qwen36.md),
      current design distilled into [ARCHITECTURE.md](ARCHITECTURE.md) "Layer 0")*. Closes the
      "One-pass VLM pipeline" TODO item; the follow-ups it spawned are back in
      [TODO.md](TODO.md).

      **The item's premise was stale.** It assumed a "Qwen-VL class" grounding model; Qwen has
      since folded vision into the main line, so Qwen3.5 (Feb–Mar 2026) and **Qwen3.6**
      (Apr 2026, 27B dense + 35B-A3B, Apache 2.0) are natively multimodal. Compatibility was
      predicted from GGUF headers before downloading 28.6 GB: the mmproj declares
      `clip.projector_type=qwen3vl_merger` and the model `general.architecture=qwen35`, i.e.
      Qwen3.6 reuses the Qwen3-VL vision tower — so it loads on any llama.cpp with
      `clip_graph_qwen3vl`. b9968 has not got it; **b10326 has**. Also surveyed and not pursued:
      Moondream 3 (MLX-only), Molmo 2 (emits *points*, not boxes).

      **Detection is excellent.** 31 real pages, 445 findings, zero parse failures. It caught
      the account number that leaks in the shipping default (`d11.p2`, the `(top,left)`
      line-order defect), the Qantas loyalty ID of issue #7 that no class covers, a card number
      buried mid-sentence in prose, and a vehicle registration never mentioned in the prompt —
      while correctly leaving Westpac's ABN, `13 22 66` and AFSL numbers alone. One coherent
      recall gap: mailing-house control codes under the address block (two banks). Barcodes are
      *not* a model failure — they are graphics, invisible to a pixels-first reader, and remain
      the existing barcode TODO.

      **Determinism: the Surya blocker is answered.** Surya 2 was disqualified because three
      temperature-0 runs gave 6/3/5 leaks, with single-slot serving recorded as untried. With
      `-np 1`, three runs give byte-identical finding sets on both a 2B and the 27B.

      **Grounding is the weak half, and it decided the architecture.** 64.9% of boxes fully
      covered at an 8 px pad; the failure is *stochastic* (same value, same layout, correct on
      p2 and wrong on p4), so padding and calibration cannot fix it, and neither position nor
      glyph size shows a trend to calibrate against. Asking for boxes additionally costs 7.4% recall
      corpus-wide. Hence PaddleOCR stays and supplies geometry in production.

      **Prompt tuning mattered more than anything else measured.** v1 (14 classes mirroring
      `PLACEHOLDER_PREFIXES`, plus a "do not report the issuer" carve-out) missed 3 values on one
      page; v2 (5 coarse classes, no carve-outs) missed 1; v5 (+ naming "policy, reference and
      claim numbers", + "identifiers live in headings too") was clean. Three lessons: recall is
      bounded by the vocabulary you name; coarse classes generalize *better*, not worse; and a
      structural hint did **not** substitute for a concrete noun — the policy number came back
      only once `policy numbers` was named explicitly.

      **Measurement caution worth carrying forward.** Six scorer iterations produced confident
      wrong answers about the model — unanchored match windows (correct boxes scored 3%), PDF
      font boxes instead of glyph ink (65% on perfect boxes), the wrong occurrence of a repeated
      value (0% on three correct boxes, reading as "16% catastrophic"), prefix-matching a logo
      against an unrelated URL, and dot leaders inflating word rects. Five made the model look
      worse than it is. Confirm any grounding claim by cropping the predicted box and looking at
      it. Separately: a dense statement's output looked mangled (`Sk Busines`,
      `Olga and Sergei Kuli L2724656893`) — those strings are **verbatim in the source**, which
      truncates narrative fields to fixed width.

      **Performance** (M1 Max, Q8_0, mains power): ~176 s/page — ~130 s image ingestion, decode
      11 tok/s against a ~14 tok/s memory-bound ceiling. `-fa on` and `-ub 2048` gave *no*
      improvement. Battery throttling doubles everything. OCR is 1.4 s/page warm, so overlapping
      it with the VLM call would save <1% and was declined.

- [x] **Retire `ocr_debug.py` for an end-to-end debug mode** *(2026-08-11, Sergei's call:
      "retire the half-stale ocr_debug.py... instead I need an e2e debug mode... independently
      turnable on/off")*.

      `pii debug ocr` and `pii/core/ocr_debug.py` are gone, with the `debug` CLI namespace, the
      round-trippable OcrPage JSON/text dumps (consumed by nothing but their own test), and
      `pdf_mode.rebuild_pdf` — the only caller of which was that command. Replaced by
      `pii/core/debug_overlay.py` + `strip --debug=<layers>` (`ocr`, `layer-0`, `locate`,
      `layer-1`, or `all`; `--debug-out` overrides the derived base path), which annotates the
      page a real strip run processed and writes it beside the output, **one file per layer**.

      Five decisions worth keeping:

      - **Attached to `strip`, not a command of its own.** Everything it draws is a by-product
        of a run that already paid minutes per page for the model. A standalone command pays
        twice and, worse, shows a re-run rather than the run that produced the output — the
        exact way `debug ocr` went stale (it could only ever show perception, never a detection).
      - **Drawn on the cached raster inside sweep 2**, never on a re-render, for the same reason
        the page cache exists: the model's `bbox_2d` lives in the coordinate space of the pixels
        it was shown.
      - **`layer-1` draws the merged plan with provenance** (`L0` / `DOC` borrowed from another
        page / `L1` pattern-only) rather than layer 1's own hits in isolation, because the merged
        plan is what is actually painted. Compared against `layer-0` it shows the pipeline's two
        characteristic moves directly: an `IDENTIFIER_GENERIC` under an `AU_TFN L0` is layer 1
        refining a coarse class; a `… L1` with nothing under it is the deterministic recall floor
        catching what the model missed.
      - **`layer-0` and `locate` are separate layers** *(Sergei, mid-review: "I thought that
        level-0 is VLM alone with its rough boxes")* — and he was right. The first cut chipped
        the placement tier onto the model's box, which files the LOCATOR's verdict under layer
        0's name; worse, where the model gave no box (the whole `--geometry ocr` regime) that
        layer silently fell back to drawing located span geometry, so its rectangles were not
        layer 0's at all. Split: `layer-0` is the model's class on the model's box and nothing
        else (empty under `--geometry ocr`, which is the truth about that regime), `locate` is
        the resolved span chipped with its tier (`exact`/`squash`/`fuzzy`/`box`/`dup`). The split
        pays for itself twice — the two rectangles over one value ARE the "search constraint, not
        paint geometry" invariant, and an unplaced finding now reads as a `layer-0` box with no
        `locate` box over it, i.e. by absence, with no tier word needed for it.
      - **One file per layer, never one page carrying all of them** *(Sergei, after seeing a real
        overlay: "The output is cluttered if enabled more than 1 layer")*. Combined, four layers
        on a statement page collide into noise, and the pair most worth comparing (the model's
        box vs the pixels painted) overlaps by construction. `DebugSpec.paths` inserts the layer
        name before the extension, so the set sorts together and each file says what is in it.

      `ImageStripResult` now carries the whole `LocateResult` (`placements`), which is what the
      `locate` layer draws. `paint._frame` grew `chip="none"` for the OCR word boxes — a chip on
      every word would bury the page under its own labels; the per-layer files removed the need
      for the above/below chip stacking the first cut had. Model-free tests in
      `tests/pii/core/test_debug_overlay.py` (layer selection, per-layer isolation by colour,
      provenance, the tier geometry fallbacks, the unplaced-draws-nothing rule, path naming) plus
      the strip_pdf per-layer companion tests; verified end-to-end on a synthetic eval page and a
      2-page PDF.

- [x] **Invert the organization policy into a configurable keep list** *(2026-08-11, Sergei:
      "keep the filter, but make it more generic: only do not redact organizations that match a
      regular expression, the expression itself should be configurable, so live outside of the
      code")*.

      **The leak that prompted it, measured before touching anything.** On page 2 of a real
      4-page statement, `SK BUSINESS TRUS` — the holder's own trust, printed into a fixed-width
      narrative field that ate the final T — survived three times. The investigation split the
      borrowed path in half and found the matcher innocent: `locate_borrowed` returned all three
      spans (518-534, 677-693, 886-902) via the fuzzy tier shipped in f75b0c0. They died one
      step later, in `_in_strip_plan`: ORGANIZATION was kept by default and `is_private_entity`
      needed a legal-form marker to strip, so `is_private_entity('SK BUSINESS TRUS')` was False
      where `'SK BUSINESS TRUST'` was True. The document already knew better — the full form was
      `ORG_1` in the map from another page. The control on the same page: `OLGA KULIK`, borrowed
      by the identical mechanism, WAS painted, because PERSON has no keep policy.

      The old rule required evidence to STRIP, and a real page destroys evidence while keeping
      the identifying name. Its sibling failure was structural too and had been documented as a
      known limit: an OCR-fused span (`SK ... TRUS ANZ HIGHETT`) rode the institution keep-list
      to safety. Requiring evidence to KEEP cannot fail that way — a mangled fragment cannot
      fake presence on a list someone wrote down.

      Shipped: `pii/core/entity_keep.py` + `data/entity_keep.txt` (institutions and common AU
      merchants, Sergei's call over an institutions-only default), `--entity-keep FILE` /
      `$PII_ENTITY_KEEP`, `PiiPipeline(entity_keep=…)`. The legal-form marker table is retired
      entirely.

      **Generalized to any entity type in the same change** *(Sergei: "In future we might need
      extend org_keep to entity_keep", then "think 1300 tel numbers")*. A bank's `1300` support
      line is detected as PHONE_NUMBER and pseudonymized exactly like a customer's mobile.
      Done now rather than later because the keep file is a hand-maintained user-facing artifact
      and its format should not migrate under him: `[ENTITY_TYPE]` sections, unsectioned lines
      meaning ORGANIZATION. The shipped `[PHONE_NUMBER]` section is present but **commented
      out** — on a business account the holder's own 1300 line is as identifying as their
      company name, so enabling the range is a per-document-set decision.

      Two consequences worth recording. ORGANIZATION stopped being a special case: it joined
      `DEFAULT_STRIP_ENTITIES` and `_in_strip_plan` now has ONE rule for every class (on the
      strip list, and not exempted by value), with `--strip-orgs` expressed as data
      (`EntityKeep.without("ORGANIZATION")`) rather than a second code path. And the audit that
      followed found two rationales this change had made stale — `grouping.CLASS_PRIORITY` put
      ORGANIZATION last "because it is the one class layer 0 emits that is KEPT by default"
      (now cosmetic: the tie-break decides a placeholder label, not whether anything is
      redacted), and `AtfTailRule` claimed the org policy would strip its span "as a private
      entity — 'atf'/'trustee' are marker words" (the rule survives for the other half of its
      job: CREATING a span over a fragment layer 0 may not report).

      The cost is deliberate: an unlisted merchant now becomes `ORG_n`. It lands on the eval's
      over-strip axis, which is reported but not gated (the acceptance gate is recall-only), and
      the harness scores against its OWN keep list (`pii_eval/entity_keep.txt`, kept in sync
      with the generator's merchant pool by `tests/pii_eval/test_entity_keep_covers_corpus.py`)
      so the axis measures the tool rather than the overlap between two lists.

      Debug follow-up in the same change: `ImageStripResult.skipped` carries the ranges the keep
      list exempted, and the `layer-1` overlay draws them in slate chipped `skipped`. That state
      appeared on NO layer before — which is precisely why three printings of a truncated trust
      name were invisible on the overlay that was supposed to explain them.

      **Second iteration, and the reason it was needed: the inversion alone did not fix the
      leak.** The first version exempted a whole span whenever the keep list matched anywhere
      inside it, and a verification run on the real statement showed `SK BUSINESS TRUS` still
      readable — the `skipped` overlay above is what found it. Layer 0 does not report the trust
      as a value at all; it reports the whole narrative field as one organization,
      `SK BUSINESS TRUS ANZ HIGHETT LOAN`, which contains `ANZ`. (Not a regression: the old
      policy kept that string too, via its keep-list-wins-ties clause, and `org_policy.py`'s
      docstring had named this exact case as a limit it could not fix. The inversion closed the
      truncated-marker half and never reached the fused-span half.)

      Fix (Sergei's call over a coverage-ratio threshold and a needle-aware exception): a keep
      match exempts **only what it covers**, and the rest of the span strips around it —
      `apply_keep` returns parts rather than a boolean. Verified end to end on the source
      document: all three printings now read `FROM ORG_n ANZ ORG_m`.

      That shredded text elsewhere, which a run of the same document made obvious: `www.anz.com`
      became `ORG_15.ANZ.ORG_16`, `ANZ App` became `ANZ ORG_11`, and the ORG placeholder count
      went 6 -> 24. Two guards followed (both Sergei's call): the match grows to its
      whitespace-delimited token, and a remainder under `_KEEP_REMAINDER_MIN` (4) alphanumerics
      is left alone. 24 -> 17, with the debris gone and the leak still fixed. The remaining 17
      are layer-0's own findings (it types phrases like 'issued by' as organizations), not split
      residue.

- [x] **A layer-0 repetition loop is silently indistinguishable from a clean page — LEAK**
      *(found 2026-08-12 while benchmarking; fixed 2026-08-12)*. Under greedy decode the model
      can enter a repeating state and emit the same entry until `max_tokens`; the array never
      closed, `parse_findings` returned `[]`, and `image_mode.read_page` treated that as "no
      findings on this page". Layer 0 is the ONLY detector for PERSON / ADDRESS / ORGANIZATION,
      so such a page emitted no name, address or organization redaction at all while layer 1
      still found the checksummed identifiers — output that looks plausibly redacted. Rate
      ~1 in 70 real pages. Full write-up of the discovery in
      [reports/2026-08-12-mac-inference-speed.md](reports/2026-08-12-mac-inference-speed.md);
      the design that resulted is in [ARCHITECTURE.md](ARCHITECTURE.md).

      **The fix was already on the wire.** `choices[0].finish_reason` is `"length"` on
      truncation and `VlmDetector._ask` discarded it. `read_response` now splits every reply
      three ways (clean / truncated / malformed) and only a closed array counts as a clean
      page. The counters ride to the caller on `Incomplete`, under the same rule as
      `unlocated`: a warning alone is deduplicated by Python's default filter, so the second
      looped page of a run was silent.

      **Refinement over the original plan: the truncated output is SALVAGED.** The plan filed
      in TODO recorded only detection, on the grounds that a grammar-guided loop truncates just
      as unparseably. But the elements *before* the cut are real detections, and discarding
      them is the larger half of the loss on a dense page — the model emitting 250 findings and
      running out of budget mid-array contributed zero. `_extract_array` now cuts at the last
      top-level comma and returns what completed, with `complete=False`. Identical entries
      collapse on that path only, because a loop's occurrence counts are worthless and
      `locate_borrowed` recovers genuine repeats mechanically — without the collapse one
      hallucinated looped value arrives as hundreds of separate "unredacted detection"
      warnings and buries the report it should be raising.

      **Measured on the reproducible specimen** (`bench_p0_ov10_s0.png`, Qwen3-VL-8B-Q8_0,
      b10326, seed 42): the loop reproduces exactly as recorded — 4096 output tokens,
      `finish_reason='length'` — both with the grammar and without it, confirming the
      form-vs-length note. Salvage recovers **38 distinct findings (3 PERSON, 3 ADDRESS,
      2 ORGANIZATION, 30 identifiers) where the previous code kept 0**, and the dedup collapses
      the loop's repeats (38 findings, 38 distinct). One reservation worth recording: what the
      run would have gone on to report after the cut is unknowable, so a salvaged page is still
      reported as a hole, not as a success.

- [x] **Constrain layer-0 output with a grammar instead of parsing whatever comes back**
      *(Sergei, 2026-08-12: "I was thinking about adding a BNF grammar to the requests,
      llama.cpp support them"; shipped 2026-08-12)*. Three GBNF grammars matching the three
      prompts, on the per-request `grammar` field of `/v1/chat/completions`. Design and
      rationale in [ARCHITECTURE.md](ARCHITECTURE.md); what the build taught us:

      - **`grammar` IS honoured on the OAI-compatible chat endpoint** (llama.cpp b10326) — it
        falls through the "copy remaining properties" path in the params parser. Verified, not
        assumed: a trivial `root ::= "yes" | "no"` forces "yes" out of "Say hi.", and each of
        the three real grammars turns a prompt explicitly demanding prose plus a markdown fence
        into a parseable JSON array.
      - **`\\` inside a GBNF character class is REJECTED by this build** ("failed to parse
        grammar"), so json.gbnf's `string` rule cannot be transcribed verbatim. Bisected to the
        character: `[\\]`, `[a\\]`, `[^\\]` all fail while `[\x5C]`, `[\x5Cbfnrt/"]` and
        `[^"\x5C\x7F\x00-\x1F]` all parse. The grammars therefore spell a literal backslash
        `\x5C`, which is also the more portable choice. A test pins it so nobody "restores"
        json.gbnf's spelling from upstream.
      - **A/B on a clean page: an exact no-op.** Same synthetic page, grammar on vs off — 29
        findings both, identical type histogram (6 ADDRESS / 2 DOB / 16 identifiers / 3 ORG /
        2 PERSON), and **652 output tokens to the token**, i.e. a byte-identical sampled
        sequence. Pass 2 likewise: 1170 tokens, 29/29 boxed, both ways. So on well-behaved
        output the model was already producing exactly this shape and the constraint changes
        nothing; the wall-clock difference between the two runs (26.8s vs 18.3s) is llama.cpp's
        per-image prefill cache warming on the second call, not a sampler cost. The full
        corpus-wide A/B against the 445-findings / 350-distinct baseline was therefore judged
        not to be needed to ship, and remains available if the picture changes.
      - One-pass boxes shape checked separately on a real page (35 findings, 1735 tokens,
        `finish_reason='stop'`), which is what exercises the bounded-integer rule against real
        coordinates.

- [x] **A checksummed identifier was invisible to layer 1 unless its groups were separated by
      exactly ONE space or ONE hyphen — LEAK** *(Sergei, 2026-08-12, on a double-spaced ABN
      coming back as an ACN: "is an outright bug"; fixed same day)*. Every pattern in
      `recognizers.py` spelled its separator `[- ]`. Measured with a VALID value in every cell:
      single space, single hyphen and no separator worked; **double space, tab, en-dash, NBSP,
      dot and newline were detected by NOTHING** — not the valid class, not the `*_INVALID`
      shadow — across TFN, ACN, ABN, Medicare and BSB alike.

      This is the same shape as the split-ownership failure that retired Presidio on 2026-08-09,
      arrived at from the other end: not two rules disagreeing, but one separator class narrower
      than the text OCR produces. A scanned statement in fixed-width columns emits double spaces
      routinely. Severity in practice is DEGRADED rather than leaked on the image and text paths
      — layer 0 names the value anyway, so it strips as `IDENTIFIER_GENERIC` with no checksum,
      no shadow and the wrong placeholder class — but layer 1 is specified as *"a deterministic
      recall floor under a stochastic detector"*, and this was a hole in exactly that floor.

      **`*` was the obvious fix and is a trap; measured, not argued.** Sergei asked what would
      break. `[- ]*` matches ZERO separators, so "grouped" stops meaning grouped and collapses
      onto bare digit runs: `\b\d{3}[- ]*\d{3}\b` is just `\b\d{6}\b`. On the text corpus at the
      production threshold that turned every six-digit number into a BSB candidate (30 -> 44)
      and let bare runs inherit the in-span score their `*_INVALID` shadow is not entitled to —
      the evidence the `likely` tier exists to require — taking invalid findings from 117 to
      **201**. `[- ]+` and the shipped class both cost **exactly zero** on the same corpus.

      Shipped: `_SEP` / `_SEP_OPT`, `{1,3}` of `[- ‐-― \t NBSP]`, applied to TFN, Medicare (valid
      and malformed), ABN, ACN, credit card and BSB, plus the mirror-image account-after-BSB
      lookbehinds so a widened BSB cannot emit a span with no account beside it. **A newline is
      deliberately excluded**: it would let two columns of an OCR-linearized page join into one
      candidate with only the checksum in the way, and a TFN's mod-11 passes 1 run in 11. Corpus
      after the change: 138 valid / 117 invalid, per-class identical to before.

      Two things left open in [TODO.md](TODO.md): the `1`/`I` confusion still lets the ACN
      capture an ABN and narrow the span, and `pii_eval/au.py` still emits only single-space
      forms — which is why this bug and its 2026-08-09 twin were both invisible to every corpus
      run ever made, and both were found by hand on a real document.

- [x] **OCR repair from the PDF's own text layer, and the font traceback that rides it**
      *(2026-08-18; asked for by Sergei 2026-08-14 — "an OCR repair pass for PDFs with text —
      match text blocks from the PDF and OCR and repair broken symbols" — and again 2026-08-18
      with the font half and the debug colouring)*. Design in
      [ARCHITECTURE.md](ARCHITECTURE.md); `pii/core/text_layer.py`,
      `tests/pii/core/test_text_layer.py`.

      **The specimen.** `ServletRetrieve (6).pdf` p1: OCR reads the account number `018057571`
      as `O18057571` and the footer ABN as `32 O09 656 74O`. Neither matches any rule — a
      five-to-ten digit run cannot start after a letter — so both survive a `--layer0 off` run
      unredacted. Verified end to end after the change, same document, same flags:

      | | with `--text-repair off` | with it on (default) |
      |---|---|---|
      | p1 ABN | not detected | `AU_ABN 1.00 '32 009 656 740'` — checksum PASSES |
      | account number, p1 and p2 | not detected | `AU_BANK_ACCOUNT '018057571'` |
      | detections | 40 | 43 |

      Those three are the whole difference — the diff of the two `--report`
      outputs contains nothing else, in either direction.

      **Corpus run** (11 reference statements, first 2 pages each, 300 dpi): 4,798 OCR words,
      4,198 aligned to a text-layer word, 3,941+ readings confirmed, **21 repaired, every one
      of them correct** — `O18057571`->`018057571` (x2), `O09`->`009`, `74O`->`740`,
      `2O23`->`2023`, `ÁBN`->`ABN`, `396`->`395`, `$120.47cR`->`$120.47CR`, typographic
      apostrophes, a restored `$` on six amounts, `013795_1BMR`->`013795_1__BMR`. Reading
      agreement per page runs 90-99%; the page-level guard never fired on the corpus.

      **What the design iterations killed, in order.** Each of these produced a WRONG repair on
      a real page before the gate that stops it was added:

      1. *Independent per-word best-overlap pairing* (the shape the TODO item proposed).
         Off-by-one drift across a whole line on the FIRST page measured — `AND`->`ADVISE`,
         `ADVISE`->`US`, `US`->`PROMPTLY`, `PROMPTLY`->`OF`, eight in a row, between two
         IDENTICAL word sequences whose OCR boxes were interpolated. Only the similarity gate
         rejected them (24 of 188 pairs lost). Replaced by per-line sequence alignment: 188 of
         188, and the same run confirms instead of failing.
      2. *Applying merges.* With merges applied, 1:N pairs produced `31`->`-31`, `O`->`&O`,
         `Please`->`lPlease` (a bullet glyph), `BUSINESS`->`WBUSINESS`, `571.33`->`$571.33`:
         a text word that squashes to NOTHING joins any pair for free — the same failure class
         as the locator's "every piece of a wrapped match must earn a character of the needle".
         Merges are now aligned (they keep the line in step) and never applied; every valuable
         repair in the corpus is 1:1.
      3. *Two-way overlap as the extent guard.* `185871` -> `185871` + 100 leader dots, and
         `30-743-3257` -> the same + 30 dots, from a table of contents. Squash drops the dots,
         so the two are at distance ZERO; and overlap does not separate them either — 0.36 for
         the worst leader against 0.41 for a REAL repair (`74O`) whose OCR box was interpolated.
         The dots really are on the page, which is why the geometry cannot see it. Fixed by a
         separate extent gate on raw length: a repair changes what a token says, never how much
         of the page it covers.
      4. *Preferring the text layer unconditionally.* `014-936` -> `014­936`: the ANZ
         statement's text layer renders the BSB separator as a SOFT HYPHEN, which would delete
         it from the `[ -]` class and unmatch the rule. A text layer can be worse than the OCR
         — the reason PDFs are treated as images at all. Non-graphic characters are refused.
      5. *A page guard counting all four gates.* Drift depresses the geometry gate without the
         text layer being at fault, so a page whose OCR merely interpolated some boxes could
         have repair disabled outright. The guard counts reading agreement alone.

      **Digit-for-digit repairs are allowed** (Sergei, 2026-08-18, on `396`->`395`, overlap
      0.75/0.93): the standing "a digit read as another digit is a different value" rule
      answers *are these the same value*, while here the alignment and the geometry have
      already answered *is this the same printed word*.

      **Rotation.** `get_text` returns UNROTATED page coordinates while `get_pixmap` applies
      `/Rotate`: on a 90° page `x * dpi/72` puts a word at x=104 where the ink is at x=1041.
      `page.rotation_matrix * Matrix(s, s)` verified against rendered ink at 0/90/180/270. A
      shifted CropBox needs nothing — `page.rect` is normalized to the origin (also measured,
      against the TODO item's guess that it would need handling).

      **Fonts: the embedded font was tried and rejected.** pymupdf extracts them, but 8 of the
      11 fonts on `ServletRetrieve` p1 are Identity-H CID subsets and Pillow renders
      `PERSON_1` through them as zero-height nothing (`getbbox` -> `(0, 37, 160, 37)`): a
      filled box with an invisible label. `FontSpec` describes the face instead and `paint.py`
      resolves it to a system one. The PDF serifed FLAG is unusable — measured across the
      corpus, `ArialMT` and `Helvetica` each appear with it both set and clear in different
      documents, and `FrutigerLTPro`, `Roboto`, `MyriadPro`, `MuseoSans` and `Gotham` are all
      flagged serifed, while not one true serif face appears; bold/italic/mono flags are right
      (`Arial-BoldMT`=16, `Courier`=8, `Calibri,Italic`=6).

      **The eval could NOT see this**, contrary to the TODO item's claim that it could:
      `pii_eval/render.py` builds corpus PDFs with Pillow's PDF writer from page images, so
      they carry no text layer and repair is a no-op on them. Recorded as a follow-up in
      [TODO.md](TODO.md); covered meanwhile by 23 pytest cases, one per gate plus the
      end-to-end pair through `strip_pdf`.

## Evaluation

- [x] **Tier 1 — synthetic corpus, text tier** (image tier iteration 1 below; degradation
      still open — see [TODO.md](TODO.md)): local generator with Faker + custom AU providers (TFN and
      Medicare with valid check digits, BSB/account, ABN/ACN, PayID), fake statement templates
      and transaction CSVs. Ground truth known by construction → automatic precision/recall;
      the fast iteration loop, fully shareable. Sergey will supply a few
      unclassified-by-construction
      example documents to serve as layout/format references for the generator's templates.
      *(2026-07-12: text tier done — `pii_eval/` package: checksum-valid AU providers, seeded
      persona pool, legacy-statement + loan-application + transaction-CSV templates with exact
      ground-truth spans, recall-first scorer with zero-critical-miss gate. Found and fixed:
      un-hyphenated/hyphenated/labeled account-number forms in transaction descriptions leaked
      (recognizer patterns extended), NER spans crossing CSV cell sentinels crashed csv_mode
      (now clamped per cell), presidio 2.2.362 rejects ACNs with check digit 0 (keep ≥ 2.2.363).
      Current: all pattern entities 100% on two seeds; PERSON 98–100% — GLiNER misses rare
      reversed-caps and "D & D Duncan" joint forms; those plus contextual identifiers are the
      layer-3 LLM-audit backlog. GLiNER now runs on CUDA (~25× faster). PDF/image tier +
      degradation pipeline still pending.)*
      **Received 2026-07-12** — a set of reference documents in `sensitive/statements/`
      (gitignored; never commit, email, or upload — cloud-LLM analysis in-session only).
      Good layout diversity: multiple major-bank statement formats, home-loan and business
      account variants, a plain-text legacy format, and an insurance certificate; at least
      one has a **broken text layer**, confirming the render-as-image rationale.

- [x] Text-tier corpus coverage audit + known-fail-mode probes *(2026-07-15: audited the
      generator against the open corner-case inventory; three gaps were measurable-now and
      got probe truth types (PERSON_JOINT convention: distinct row, not in `build.CRITICAL`),
      the rest were filed as TODO notes (pseudonym-consistency scoring, masked last-4 forms,
      metadata coverage, no-context short suburbs). Added: **`LOCATION`** bare-town mentions
      and **`LOCATION_SHORT`** real 3-letter suburbs (Kew/Ayr/Hay — the `LOCATION_MIN_CHARS=4`
      sacrifice) in the loan notes; **`ADDRESS_BARE`** street-only lines ("RENT 53 MILES ST")
      and **suburb-suffixed merchants** ("EFTPOS WOOLWORTHS NEWTOWN") as whole
      keep-ORGANIZATION spans in transaction descriptions; PO Box postal addresses; and
      `Business.trust` (previously generated but unused) wired in as statement account
      holders / loan trustee lines — decided with Sergei: trusts are business entities, so
      keep-ORGANIZATION despite the surname stem. Real-suburb vocabulary (`TOWNS`,
      `SHORT_SUBURBS`) added to personas.py because Faker's en_AU fabricates city names —
      fine for ADDRESS format signal, wrong for gazetteer/NER-knowledge probes. First
      numbers (seed 42, regenerated — older score logs are not comparable): gate still
      PASS, PERSON 66/66; LOCATION 4/4; LOCATION_SHORT 4/4 **but** verified rescued by the
      GLiNER2 ADDRESS pass on sentence context at near-threshold score (Kew 0.433 vs 0.4),
      not by the floored location pass — fragile, contextless short suburbs still exposed;
      ADDRESS_BARE 11/12 (the documented miss class reproduces); ORGANIZATION 35 kept /
      21 over-stripped — the trust and merchant-suburb probes bite, giving the
      overlaps-merging task its metric; PERSON_JOINT 1/6, PERSON_REVERSED 4/6 on the
      reshuffled draws. Testbench counterparts (edge cases get BOTH a pytest and a corpus
      probe — working agreement 2026-07-15): `test_known_hard_forms_present_and_not_gated`
      (generator), `test_kept_org_does_not_shield_nested_address` (the wart, model-free),
      the 'Kew' floor case in `test_gliner2_floors`, and the `model`-marked
      `test_real_ner_short_suburb_rescued_by_address_pass`.)*

- [x] **Tier 1 — image tier, iteration 1: paired rendered corpus + re-OCR survival scorer**
      *(2026-07-16: Sergei's proposal — instead of waiting for the reportlab templates,
      print the existing text corpus onto images. That makes the first image corpus nearly
      free AND creates a **paired corpus**: same content, same `truth.json`, two modalities,
      so any score delta is attributable to exactly two causes — OCR errors, or structure
      the text path exploits that pixels don't carry. `pii_eval/render.py`: Pillow + Windows
      system TTFs, content-sized white pages, per-doc font+size (20–26 px) from an RNG
      seeded by the corpus seed (recorded in `manifest.json`, which also points back at the
      source text corpus — no truth duplication). Font variety per Sergei; fixed-column docs
      (legacy statements, CSVs rendered as column-aligned tables) draw from a monospace pool
      since their layout IS the whitespace, loan docs mix in proportional fonts.
      `pii_eval/score_image.py`: each page through the real image pipeline (OCR → detect →
      paint), then the painted output is **OCR'd again** and every truth entity scored by
      value survival in the redacted image — value-based, not span-based (offsets are
      meaningless through pixels). Matching is OCR-tolerant and recall-first: exact
      normalized containment, else confusion-squashed containment (0/O, 1/l/I, 5/S, 8/B...),
      else banded edit-distance for values ≥8 squashed chars — fuzzy survivors count as
      LEAKED (`~ocr` column); values squashing under 4 chars match exactly only (3-letter
      suburbs would false-leak at distance 1). Invalid-injection axes and the critical gate
      carry over. CLI: `render` subcommand + `score --modality image`; 11 tests in
      `tests/pii_eval/test_render.py`.
      First side-by-side, seed 42 (text → image): both predicted delta *classes* confirmed,
      but the identifier leaks were root-caused post-run by OCR probes and the mechanism is
      NOT digit misreads — the digits survived intact in all three; what OCR broke is the
      **shape and layout that pattern recognizers key on**. (1) AU_TFN 100% → 86%:
      Tesseract collapsed one space ('565 431 023' → '565 431023'), which matches neither
      TFN pattern (`\d{3}\s\d{3}\s\d{3}` / `\b\d{9}\b`) — so the mod-11 checksum never even
      ran — and the label misread 'TFN:' → 'TEN:' killed the context rescue too (gate FAIL,
      correctly; flagged by the fuzzy matcher). AU_DRIVERS_LICENCE 100% → 75%, different
      mechanism: Tesseract segmented the form's label/value columns into separate BLOCKS,
      assembling '36629946' ~26 lines away from 'Driver licence:' — the bare digit run lost
      its context boost and fell below threshold. The originally predicted
      digit-misread/checksum-break class remains expected once the degradation tier lands;
      the clean renders leaked via shape and layout instead.
      (2) **Cell isolation doesn't exist in pixels**: PERSON_REVERSED 94% → 31%,
      PERSON_COMMA 100% → 12% — the RECORD_SEPARATOR window boundaries that fixed
      reversed-name interference are a text-path structure; OCR text of the rendered names
      doc has none, so pre-fix interference returns. One canonical PERSON also leaked there
      (ISLA FERGUSON, exact). Bonus artifact class: OCR merged adjacent statement columns
      into digit runs that tripped the invalid-identifier detectors (2 noise findings on
      legacy_00.png) — unproducible in the text tier. ORGANIZATION over-strip 7 → 12
      (column structure lost). Accepted limitation (README note + TODO item): whole-value
      survival has no `partial` axis — a value with any word painted out scores `stripped`
      even if a fragment stays readable, which is why ADDRESS_BARE (57% → "100%") and
      CONTEXTUAL_ID (0% → "100%") apparently improved; a token-level axis needs occurrence
      disambiguation first (surname stems recur inside kept business names). Remaining
      image-tier work (degradation pipeline, reportlab layout source, bbox truth) stays in
      [TODO.md](TODO.md).)*

- [x] **Retire the segmenter layer; layer 0 becomes the default detector** *(2026-08-09,
      Sergei's call, taken on the VLM report's numbers —
      [reports/2026-08-08-vlm-oneshot-qwen36.md](reports/2026-08-08-vlm-oneshot-qwen36.md).
      Current design in [ARCHITECTURE.md](ARCHITECTURE.md) "OCR perception layer" and
      "Layer 0"; this record is the before/after.*

      **What went.** `ocr_doclayout.py` (PP-DocLayoutV3) and `ocr_ppstructure.py`
      (PP-StructureV3) with their tests; `OcrBlock`, `OcrLine.block_id`, `build_layout_page`,
      `_assign` and the orphan-clustering machinery; the per-block recognizer feed
      (`linearize_blocks`, `rebase`, `--feed` on both `pii` and `pii_eval`); and — a separate
      redundancy the segmenter had been hiding — the entire flat path (`get_ocr`,
      `OCR_BACKENDS`, `ocr.OcrWord`, `OcrResult`, `assemble`, `worker_ocr`, `make_paddle_ocr`,
      `ocr_image_paddle`, `result_to_ocr`, `strip_from_ocr`), whose
      `boxes_for_span`/`painted_boxes_for_span` were a near-line-for-line duplicate of
      `RecognizerInput`'s. ~1,700 lines of source and tests. The three layout model
      directories under `models/paddlex/official_models` (`PP-DocLayoutV3`,
      `PP-DocLayout_plus-L`, `PP-DocBlockLayout`, 375 MB) were deleted with them.

      **Why.** The whole segmenter existed to reconstruct page structure a VLM reads
      natively, so once layer 0 became the detector it had no consumer. It was also never a
      net win on its own axis: on the 31-page real corpus `doclayout:v3 + --feed blocks`
      scored 8 critical leaks against the line-only path's 9 — it *traded* leaks rather than
      removing them — and the repayment plan for its known regression (table-cell structure →
      per-cell feeding → a perception-hierarchy change) was a large programme. Retiring it
      also closes six open TODO items and restores `_rows` column banding, which fixes the
      `d11.p2` account-number leak that was live in the shipping default (the panel's label
      and value land on one assembled line again, so context promotion fires). The adapters
      are one revert away in git history — the same disposition as Tesseract and Surya.

      **Stale-doc correction found on the way.** `TODO.md` claimed the flat `OcrResult` path
      was "the only configuration that still bands columns into visual rows". It was not:
      `ocr_page_paddle` → `build_page(_result_to_rows(...))` goes through the same `_rows`
      banding. The flat path was reachable-only-by-routing, not behaviourally distinct, which
      is what made deleting it free.

      **Layer-1 refinement, built in the same change** (the TODO item "Layer-1 refinement of
      VLM findings", step 2, previously designed-not-built). `PiiPipeline.merge_detections`
      runs an ordinary layer-1 pass over the same OCR text and merges it with the located
      layer-0 spans; `_rank` gained a middle tier so a specific class outranks
      `IDENTIFIER_GENERIC` outranks the `*_INVALID` shadows. That yields refine/validate/union
      with no new merge machinery. Layer-0 spans are also put through `_in_strip_plan` first —
      found while wiring it: the VLM path had been stripping **every** ORGANIZATION,
      ignoring the kept-ORGANIZATION policy, because `strip_from_vlm` never consulted the
      pipeline at all. Merchant and bank names are kept again, and `--strip-orgs` works.

      **Seam fix, also found while wiring it.** `strip_pdf`'s VLM branch re-entered
      `strip_image`, which resolves its own OCR engine — so the engine was resolved per page
      and the pdf-mode test seam was silently bypassed (a monkeypatched fake never applied and
      real paddle ran). The detector/geometry dispatch now lives once in
      `image_mode.strip_rendered_page`, which takes an already-resolved engine; `strip_pdf`
      resolves it once per document and skips it entirely under `--geometry vlm`.

      **Defaults flipped:** `--detector vlm --geometry ocr` for `strip --image`/`--pdf` and
      for `pii_eval score --modality image/pdf`; `--ocr-backend` collapses to the paddle tiers
      and defaults to `paddle` (v6_medium). Text and CSV input has no page image, so it
      resolves to `layers` — only an explicit `--detector vlm` on text input is an error.
      Consequence accepted knowingly: image/PDF runs now need a llama-server and cost minutes
      per page. `pii_eval`'s read-back engine is pinned to the default tier regardless
      (`reread_engine`), because the model under test must not be its own scorer.

      **Ported rather than deleted:** `pii_eval/ocr_report.py` moved off the flat API to
      `get_ocr_page` + a local `_Word` record (the perception layer carries no per-word
      confidence — paddle scores lines), closing the migration debt TODO.md had recorded;
      `tests/pii/core/test_ocr.py` was deleted as a full duplicate of `test_linearization.py`.
      Fast suite 266 green (was 316 before ~50 tests went with the deleted modules), and it
      dropped from 23 s to 6.7 s once the pdf-mode seam stopped loading real paddle.)*

- [x] **Box-guided location: the two-pass hybrid** *(2026-08-09, Sergei's call. Current design
      in [ARCHITECTURE.md](ARCHITECTURE.md) "Layer 0"; this record is the reasoning and the
      before/after. Closes the "Hybrids that deliberately use the VLM's own boxes" item, which
      had been postponed the same day.)*

      **The trigger.** A review of how VLM detections and OCR spans were matched turned up
      five failure modes in `locate()`, and four of them shared one root cause: the search was
      page-wide with no positional constraint. A short identifier squash-matching inside a
      monetary amount (`"4000"` inside `$14,000.00` — verified, the old locator paints the
      amount and leaves the real value legible); a repeated value claiming the wrong
      occurrence; a nested finding (`"John"` reported after `"John Smith"`) either colliding
      with `taken` or hunting for an unrelated John; and unlocatable values, which were warned
      about but not counted anywhere a caller could see — and Python's default warning filter
      deduplicates an identical message from the same line, so a second page with the same
      residue was silent.

      **The reframing that shaped the design.** Sergei proposed making the boxes first-class
      with an overlap-gated locator and a Levenshtein tier. The refinement taken was that a
      box's *primary* value is as a **search constraint**, not as fallback geometry — and that
      this is safe precisely where painting is not. The measured box failures (64.9% fully
      covered, p90 inward clip 63.9 px, the two-pass one-character shift) are all *painting*
      failures: painting tolerance is zero pixels, localization tolerance is about half a word,
      so a box clipped by 60 px still names the right region unambiguously. The same signal
      that is unusable for pixels is decisive for disambiguation.

      That also settles the fuzzy-matching question the old invariant had closed. "No fuzzier
      than the squash" was justified by *global* search; under a box the justification
      evaporates, so the rule became **fuzzy matching is permitted exactly where a box
      constrains the candidate set**.

      **Levenshtein vs. a confusion table — Sergei's correction, and it was right.** The first
      proposal leaned on a confusion-class fold (the measured `0→@`, `J→3`, `1→2`, `4→8`,
      `W→H` pairs from the 2026-07-17 fidelity sweep). Sergei's objection: a substitution table
      fails when the damage is to a character it does not list, and cannot express a *dropped*
      character at all, whereas edit distance still yields a useful metric. The resolution is
      weighted Levenshtein — the table is a **discount inside** the DP, never a gate in front
      of it, degrading to plain Levenshtein wherever it is silent. The motivating case proves
      the point on its own: the top measured confusion is `0` read as `@`, and `@` does not
      survive the alphanumeric squash, so that damage reaches the matcher as a *deletion* that
      no confusion table of any size could catch.

      **Two passes, not one.** Boxes are requested by a second call (`VlmDetector.localize`)
      rather than added to the detection prompt, because the report measured single-pass boxes
      at −7.4% recall corpus-wide (350 → 324 distinct values over 31 pages). Pass 1 is
      byte-identical to the values-mode prompt, so the 445/350 baseline is preserved by
      construction. Affordable because image prefill is cached per image: ~16 s for a second
      pass against the ~130 s the image itself cost. The report had already measured two-pass
      as boxing more tightly than one-pass (1.24× vs 1.41× ink).

      **What landed.** `pii/core/fuzzy.py` (weighted Levenshtein, confusion discount, budget)
      and `pii/core/locator.py` (candidate scoring + the three geometry tiers), both new;
      `VlmDetector.localize` + `_LOCATE_PROMPT` + `attach_boxes` in `vlm.py`;
      `PiiPipeline.strips_value` so the kept-ORGANIZATION policy reaches tier 3 (without it the
      default run paints over every bank logo the model boxes); `box_geometry` and `unlocated`
      on `ImageStripResult`/`PdfPageResult`, reported by the CLI regardless of `--report`;
      `--geometry hybrid` as the new default with `ocr`/`vlm` kept as instruments, plumbed
      through `pii_eval score` too.

      Candidate scoring replaced the strict tier ladder mid-design: strict tiers have an
      ordering pathology where a displaced box's local match beats a correct global exact
      match. Ranking every candidate by `(has_overlap, kind, overlap, -distance)` avoids it and
      gives the guarantee that made the change safe to adopt — **with no box every overlap is
      zero and the ranking collapses to kind-then-position, which is exactly the pre-box
      behaviour**, so nothing that located correctly before can regress. The old `locate()` was
      deleted rather than kept beside it, to avoid two copies of the squash-matching rule.

      Tier 2 (box overlaps OCR words, text matches only fuzzily) was added to the sketch, which
      had only OCR-span-or-model-box: where OCR misread the value we still hold exact glyph
      geometry for the words we misread, so most of what the sketch would have sent to the
      padded-box fallback keeps exact geometry instead. Tier 3's box is padded by 0.6× its
      height — scaled rather than fixed because the residual two-pass error is a
      one-character displacement, which scales with the font — and unioned with any word it
      substantially covers, so a box clipping mid-word is completed by that word's own box.

      **First real run (2026-08-09, 2 pages of the insurance certificate, Qwen3.6-27B Q8_0 on
      the Mac at b10326).** End-to-end clean: 21 entities, layer 1 refining the coarse classes
      into `AU_TFN`/`AU_ABN`/`AU_DRIVERS_LICENCE`/`EMAIL_ADDRESS`/`PHONE_NUMBER`, tier 3 firing
      twice, and a re-OCR of the output confirming every probed value gone from the pixels.
      Three findings came out of it, two of them recorded as TODO items: a tier-3 paint does
      not suppress a later identical finding, so the "NOT redacted" line raised a false alarm
      (safe direction, but it is the line that must be trustworthy); a value wrapped across
      lines/columns falls to tier 3 instead of matching, costing exact geometry rather than
      recall; and — the significant one — **the image prefill is not reused between the two
      passes**, so the hybrid costs ~2× per page instead of the ~+9% the design assumed. That
      last one invalidates a premise taken from the 2026-08-08 report and is recorded against
      the serving/tuning item, where it now sits alongside the page-conveyor idea.

      **Still not measured.** The A/B (`--geometry hybrid` vs `ocr` on the 31-page real corpus)
      is the open TODO item that follows this one. In particular the size of the tier-3 residue is
      unknown, and it decides whether tier 3 earns its complexity or the disambiguation is
      carrying the whole change.

      **Two holes closed by self-review before hand-off**, both in the same direction —
      geometry that carries no information must not be allowed to look like a redaction. A
      zero-area model box padded for tier 3 becomes a small rectangle at an arbitrary spot,
      which would be painted and *counted* while covering nothing; it now reports the value as
      unplaced instead. And a box covering most of the page is not a constraint, so allowing
      the fuzzy tier under it would smuggle back exactly the page-wide edit-distance search
      the design forbids; above 40 covered words the fuzzy tier is withdrawn while
      exact/squash and overlap ranking continue.

      Fast suite **308 green** (was 266: +12 `test_fuzzy.py`, +23 `test_locator.py`, +7 net in
      `test_vlm.py` — 11 added against the 4 retired `locate()` tests, which moved to
      `test_locator.py` as the no-box cases they now describe); heavyweight suite 8 green.)*

- [x] **Layer 0 for text and CSV — the Qwen3.6 text modality** *(2026-08-09, Sergei's call;
      step 1 of the three-step GLiNER2/spaCy/Presidio retirement scoped the same day — the
      order and rationale are in [TODO.md](TODO.md)'s direction note, the design in
      [ARCHITECTURE.md](ARCHITECTURE.md) "Layer 0".*

      **Why it had to come first.** Retiring GLiNER2 leaves text and CSV with no PERSON,
      ORGANIZATION, ADDRESS or DATE_OF_BIRTH detection at all — that is the retired `--no-ner`
      regime, which was removed 2026-07-15 as unsafe. Image/PDF was already covered by layer 0;
      text was not, because it has no page image. So the replacement is built and measurable
      *before* the incumbent is deleted, and no input mode is ever without a semantic detector.

      **What shipped.** `text_llm.py` — the same model and the same five coarse classes reading
      the document string; `text_mode.py` — the text counterpart of `image_mode` (detect →
      locate → `apply_plan`); `locator.locate_in_text` — placement without geometry;
      `pipeline.apply_plan` split out of `PiiPipeline.strip` so both detectors splice a plan
      identically. `csv_mode` delegates detection to `detect_text`, so a detector serves CSV
      for free; its per-column batching is untouched (the guarantees it buys — no placeholder
      straddling a cell, date/amount columns byte-identical — are structural, independent of
      which detector runs). `strip_csv` and `strip_text` now return a `TextStripResult`
      carrying `unlocated`, because a detection that cannot be placed must be *counted* and not
      only warned about (Python's default filter deduplicates a repeated warning).

      **Three design points that differ from the vision path**, all consequences of holding the
      source text rather than pixels. (1) *No geometry leg at all* — the model quotes from the
      string it was handed, so location is a search, not a reconciliation. (2) *The prompt asks
      for DISTINCT values only*, and every occurrence is then found mechanically — exact, free,
      complete, and it does not degrade with document length the way asking a model to
      enumerate does. (3) *Nested findings are not suppressed*: the image locator must stop
      "John" from hunting a different John because each finding needs its own box, whereas in
      text a second John SHOULD be marked, and the overlap is unioned by `_merge_overlaps`.

      **Windowing, and why the overlap is cheap.** Findings are located against the whole text,
      not the window that produced them, so a value cut in half by a boundary only has to
      survive intact in *one* window to then be marked everywhere. That makes the overlap a
      recall backstop rather than a correctness requirement; windows cut on line boundaries to
      make intact survival the common case. 4000/400 chars, calibrated by analogy to a page
      (the only unit that has been measured) — a sweep is unspent work.

      **Squash fallback carries a length floor (4 chars) that exact matching deliberately does
      not.** Squash collapses separators, so it matches across word boundaries; on a page that
      is held in check by the model's box, and here there is no box. Exact matching keeps no
      floor at all — real 2-char surnames (Wu, Ng) and 3-char organizations (NAB, ANZ) exist,
      per the 2026-07-14 no-floor decision.

      **Found by the new tests, not by review:** `PROMPT.format(document=...)` raised
      `KeyError: '"text"'` — the prompt contains a literal JSON example, so treating it as a
      format template makes every brace a field. Fixed by dropping the template entirely
      (`build_prompt` concatenates) rather than by doubling braces, which would have left the
      trap armed for the next edit of the output shape.

      **Deliberately still opt-in.** `--detector vlm` now works on text and CSV, but the text
      default stays `layers` until the A/B against GLiNER2 is scored (TODO item; it gates the
      deletion). Both regimes are runnable from `pii_eval score --detector`, so the comparison
      is same corpus, same layer 1, semantic detector as the only variable.

      **Found while wiring the A/B, and it would have invalidated it.** ARCHITECTURE said
      layer 0 replaces layer 2, but `merge_detections` runs the whole registry and GLiNER2 is
      unconditionally in it — so `--detector vlm` has been unioning layer 2 in all along, on
      image/PDF too, since the 2026-08-09 flip. Verified directly:
      `PiiPipeline().merge_detections([], 'Olga Kulik paid rent to Sergei Kulik')` returns two
      PERSON spans from no layer-0 findings at all. Left as-is in production (recall-safe, and
      it resolves itself when GLiNER2 goes — the open question is recorded in
      [TODO.md](TODO.md)), but the eval harness now builds `PiiPipeline(ner=False)` whenever a
      layer-0 detector is in play, so the comparison differs by the semantic detector alone.
      `ner=False` is an instrument, not a regime: a user-facing patterns-only mode is the
      retired `--no-ner`, removed 2026-07-15 as unsafe.

      Fast suite **344 green** (was 308: +18 `test_text_llm.py`, +16 `test_text_mode.py`,
      +2 `test_registry_policy.py`). The corpus probe half of the dual-coverage rule is the
      A/B run itself.)*

- [x] **Retire layer 2: GLiNER2 and the `--detector` flag deleted** *(2026-08-09, Sergei's call
      on the A/B — [reports/2026-08-09-text-layer0-vs-gliner2.md](reports/2026-08-09-text-layer0-vs-gliner2.md).
      Step 2 of the three-step GLiNER2/spaCy/Presidio retirement; current design in
      [ARCHITECTURE.md](ARCHITECTURE.md) "Layer 2 (GLiNER2) retired".*

      **What decided it.** Seeds 42/123/7, layer 1 held constant, semantic detector as the only
      variable (the layer-0 arm builds without the NER model — see the confound note in the
      previous record). Layer 0 equal or better on every class and seed; `PERSON_REVERSED`
      89/95/95% -> **100/100/100**; ORGANIZATION over-strips 32->29, 30->25, 40->28; s42's gate
      flipped FAIL -> PASS. The only ADDRESS "regression" (100% -> 83% on every seed) was read
      back from the output and is a scoring artifact: on fixed-column locality lines the model
      returns `NEW KAYLAMOUTH` and `NSW 2926` as the two values they are, both strip, and the
      column padding between the placeholders is what the span-coverage scorer counts as
      uncovered.

      **Accepted losses, recorded rather than fixed** (Sergei, on review): the colliding-surname
      case that fails s7 in both arms — a surname that is also a banking word (`... PERSON_5 FEE`)
      is not worth further precision engineering — and the two invalid-identifier regressions,
      both now TODO items: the *context* tier lost its only source (GLiNER2's post-validation
      demoted shape-correct checksum failures; the shadows do not collect those at `likely`), and
      layer 0 strips a checksum-failed identifier under `IDENTIFIER_GENERIC` regardless of
      `--mask-invalid-identifiers`.

      **What was deleted.** `gliner2_recognizer.py` (495 lines) and its two test modules; the
      `--detector` flag from both the CLI and `pii_eval`; the layers path in `image_mode`
      (`strip_from_page`, `_needs_ocr`) and its `detector=None` branches everywhere;
      `PiiPipeline(ner=...)`, which existed for one afternoon as the A/B instrument; the
      conftest `sys.modules` GLiNER2 shim and its `stub_ner` switch; the "Experiments — GLiNER2
      tuning" TODO section (55 lines, all of it hypotheses about a component that no longer
      exists); `gliner2` from requirements.

      **The detector is now REQUIRED at every strip entry point** (`strip_text`, `strip_csv`,
      `strip_image`, `strip_pdf` — keyword-only, no default). That is the substantive design
      choice in this step rather than a mechanical consequence: with layer 2 gone, a call that
      omits a detector would silently become the patterns-only `--no-ner` regime retired
      2026-07-15 as *unsafe*, so it must not be reachable by forgetting an argument.
      `PiiPipeline.detect` stays public as a **layer** — it is what `merge_detections` consumes —
      never as a way to strip a document. `tests/pii/core/test_registry_policy.py` was rewritten
      around the new invariant: no registry entry may claim ADDRESS or DATE_OF_BIRTH, and PERSON
      only from the mechanical `JointNameRecognizer`.

      **Follow-through the retirement earned.** `PERSON_REVERSED` promoted into
      `pii_eval.build.CRITICAL` — the 2026-07-15 record said "when the residual closes, promote
      it", and it closed at 100% on three seeds. The tier-1 gate test now fails with an
      explanatory message rather than a raw `ConnectionRefusedError` when no server is up.

      **Consequences worth knowing.** Every input mode now needs a llama-server, the tier-1 gate
      included — there is no offline path left, accepted knowingly. And `csv_mode`'s sentinel
      keeps only one of its two jobs: it still blocks pattern matches across cells, but it is no
      longer an attention-window boundary.

      **The torch consequence was claimed too early and is WRONG — corrected same day.** This
      record originally said the pipeline no longer imports torch and that `ocr_worker.py` might
      therefore be retirable. Sergei asked for the retirement; the "verify, do not assume" check
      the TODO item demanded is what caught it. GLiNER2 was the only *direct* torch consumer,
      but spaCy's `thinc` ships a PyTorch shim and imports real torch eagerly, so
      `import spacy` / `import thinc` / `import presidio_analyzer` each leave torch in
      `sys.modules` with `cuda.is_available() == True`, and so does `PiiPipeline()`. The
      paddle-GPU DLL conflict is untouched and the worker stays. Its retirement moves downstream
      of the Presidio/spaCy step, which is what actually removes thinc.

      Fast suite **329 green** (was 344 before the deletion: -33 GLiNER2 test modules, -2 the
      `ner=False` instrument tests, +4 new registry-policy and CSV-clamping tests, +16 net from
      earlier in the session). Test conversions worth noting: `test_image_mode.py`'s six
      `strip_from_page` call sites now go through `strip_from_vlm` with an empty finding list,
      which is exactly equivalent (layer 1 supplies the whole plan) and is what those assertions
      were always about.)*

- [x] **Retire Presidio and spaCy; the detection engine is ours** *(2026-08-09, step 3 of the
      three-step retirement. Design in [ARCHITECTURE.md](ARCHITECTURE.md) "Presidio and spaCy
      retired"; this record is the before/after.)*

      **What we were actually using** was a regex loop, a three-way validation hook, a context
      boost and a threshold. What it cost was a mandatory spaCy NLP engine, ten recognizers we
      never stripped on running on every call (US SSN/bank/passport/ITIN/licence, NHS, crypto,
      MAC, medical licence, DATE_TIME), and split ownership of every checksummed identifier.
      `engine.py` (~190 lines) + `detection.py` (~40) replace `AnalyzerEngine`,
      `RecognizerRegistry`, `PatternRecognizer`, `RecognizerResult` and
      `LemmaContextAwareEnhancer`.

      **The split ownership was a LIVE LEAK, and it is what made this urgent.** Presidio owned
      the valid classes with SPACE-only patterns; our shadows owned the invalid ones with
      `[- ]`. A hyphen-grouped **valid** TFN/ABN/ACN/Medicare (`123-456-782`) therefore matched
      Presidio not at all and was dropped by the shadow *for passing its checksum* — detected by
      nothing, in all four classes. Verified directly before the merge. The corpus could not see
      it: `pii_eval/au.py` only ever emitted space-grouped forms, so the gate had never had a
      chance. `ChecksumRule` now matches once, extracts digits once, calls the checksum once and
      branches, so the halves cannot disagree — there is no second implementation to disagree
      *with*. `pii/core/checksums.py` becomes the single source of truth, closing the standing
      "stop duplicating Presidio's checksum arithmetic" item by deletion (its proposed fix,
      delegating *to* presidio, was inverted by the retirement).

      **Behaviour preserved deliberately**, because every score in `recognizers.py` was tuned
      against it: validation overrides the pattern score (True -> 1.0, False -> drop, None ->
      keep), context boost +0.35 floored to 0.4 capped at 1.0, duplicate spans collapse to the
      highest score. Pinned in the new `test_engine.py` rather than left implicit.

      **Behaviour deliberately CHANGED, twice.** (1) Context matching went from spaCy lemmas to
      a 60-char window searched for the term as a substring — which is what the 2026-07-15 spaCy
      source review concluded ("keep label/context matching char-level") after finding that
      `a/c` fragments into three tokens while `TFN:123456782` stays one, so the label never
      surfaced as a token either way. (2) Labels are matched as LOOKBEHINDS, so the span is the
      value alone. The old shadows matched labels in-span and got away with it — an invalid
      candidate is reported, not aliased — but for a valid identifier a span covering
      "TFN: 123 456 782" keys the pseudonym map on a different string than a bare occurrence,
      forking one TFN into TFN_1 and TFN_2 inside a document. Caught by the existing
      placeholder-consistency test, which is exactly what it was written for.

      **Harvested rather than reimplemented**: Presidio's email regex (it handles punycode/IDN
      labels) with `tldextract` validation kept, and its credit-card brand-prefix pattern. IBAN
      dropped the ~90-entry per-country format table for a generic shape plus ISO 13616 mod-97 —
      mod-97 is what actually validates an IBAN and AU documents carry them rarely. `phonenumbers`
      is now a direct dependency instead of reaching it through Presidio. `regex` stays, and is
      load-bearing: the account-after-BSB and labeled-identifier patterns use variable-length
      lookbehind that stdlib `re` cannot compile.

      **Two operational facts about the dependency, moved here because ARCHITECTURE no longer
      has a home for them.** Presidio *shipped* AU_TFN/AU_MEDICARE/AU_ABN/AU_ACN (MIT, no paid
      tier involved), but its default registry config
      (`presidio_analyzer/conf/default_recognizers.yaml`) listed every country-specific
      recognizer with `enabled: false` — only generic + US recognizers ran, so the four AU
      classes silently never fired unless registered explicitly, which `pipeline.py` did from
      2026-07-12. And the version floor was real: 2.2.362's ACN validator rejected every ACN
      with check digit 0, and 2.2.364 changed the ABN validator's leading-zero handling (record
      above). Both pins die with the dependency — `checksums.py` owns that arithmetic outright
      now, and its only remaining mirror is `pii_eval/au.py` under a coupling test.

      **Then the paddle worker went too.** Making the process torch-free is what finally
      satisfied the precondition recorded that morning — see the next record.

      Fast suite **360 green** (was 329: +16 `test_engine.py`, +22 `test_checksum_rules.py`,
      -11 the deleted worker-protocol module, plus ports). Dual coverage on the leak: the
      regression tests above AND a corpus probe (`au.hyphenate`, emitting a hyphen-grouped ABN)
      so the eval can see the class of bug that hid this one.

- [x] **Retire the paddle worker subprocess** *(2026-08-09, immediately after the chassis swap.
      Sergei asked for it earlier the same day; the "verify, do not assume" clause on the TODO
      item is what stopped it going out wrong.)*

      **The first attempt was based on a false premise.** The GLiNER2 record claimed the pipeline
      had become torch-free because GLiNER2 was "the only torch consumer in the repo". It was
      the only *direct* one. spaCy's `thinc` ships a PyTorch shim and imports real torch eagerly,
      so `import spacy` / `import thinc` / `import presidio_analyzer` — and therefore
      `PiiPipeline()` — all still left torch in `sys.modules` with `cuda.is_available()` True.
      Measured, not guessed. The worker was kept and the retirement re-recorded as downstream of
      the Presidio/spaCy step.

      **After step 3 the premise holds.** Verified before deleting anything: the full analysis
      stack plus in-process GPU paddle in ONE interpreter, on the 2080 Ti (Compute Capability
      7.5), correct OCR output, ~11 s cold. A second finding fell out of that run — `image_mode`
      and `text_mode` still imported `RecognizerResult` from presidio, which alone was enough to
      drag torch back into the image path. Both now import `Detection`; the subprocess import
      test in `test_registry_policy.py` was widened to cover the front-ends, not just
      `PiiPipeline`.

      **Gone**: `ocr_worker.py` (253 lines), its framed stdio protocol, its test module, and the
      "routing is by wheel" branch in `get_ocr_page` — OCR is in-process on either wheel.
      **Kept, and load-bearing**: the torch guard in `ocr_paddle._engine` (now the *only* thing
      between paddle-GPU and torch — it turns a re-introduction into a clear error rather than a
      WinError 127 crash), the modelscope torch stub, and the lazy package inits. The worker is
      one revert away in git history, the same disposition as Tesseract, Surya and the layout
      backends.)*

- [x] **Document-wide entity grouping — a page stops being the unit of truth**
      *(2026-08-11. Reported by Sergei from CLI use: "on multipage documents some entities are
      detected on one page and missed on another". Design agreed over three rounds before any
      code; the distilled result is in [ARCHITECTURE.md](ARCHITECTURE.md) "A page is not the
      unit of truth", the invariants in [../CLAUDE.md](../CLAUDE.md).)*

      **The defect had two faces, and only one of them was reported.** Across pages: layer 0
      reads one page at a time, `strip_pdf` streamed, so page 1's findings no longer existed
      when page 4 was painted and nothing could notice the disagreement. *Within* a page:
      `locate_findings` places one span per finding, so a value printed three times and named
      once was painted once — found while writing the tests, and it means single-page `--image`
      gained from this change too. The existing `test_hybrid_geometry_runs_a_second_pass_and_uses_it`
      had encoded the second face as expected behaviour (two identical values on the page, one
      span asserted); it now asserts both are painted and that the box still decides which one
      the model's own finding claims.

      Shipped as three stages — read all pages (`image_mode.read_page`), group
      (`grouping.py`), redact all pages (`locator.locate_borrowed` beside the unchanged
      box-guided `locate_findings`). No extra model calls: the cost is one OCR pass and one
      disk round-trip per page against ~300 s of model time.

      **Design points that took argument rather than code:**

      - *Grouping decides the class and the report, not recall.* Every constituent is searched
        independently, so the flat variant set produces the spans. This is what bounds the
        clustering rule's blast radius to a mislabel, and it is why the Levenshtein threshold
        turned out to be a low-stakes knob.
      - *Cache the raster, don't re-render* (Sergei: "re-rendering is a design smell"). The
        stronger reason than memory: the model's `bbox_2d` is in the coordinate space of the
        pixels it saw, so a second render only *assumes* it reproduces the first.
      - *Two confusion tables.* `fuzzy.CONFUSION_PAIRS` mixes cross-class pairs with
        digit↔digit ones (`1↔2`, `4↔8` — both from the MEASURED set, not folklore). Discounting
        those is right for the locator, where a box pins the region, and wrong for identity,
        where nothing does: `…4936` and `…8936` would merge. `IDENTIFIER_CONFUSION_PAIRS`
        derives the cross-class subset so a refresh of the measured table cannot leave a stale
        copy. Sergei asked the question that produced this ("but then what about the
        confusions? O<->0, etc?") after accepting stricter matching for digits.
      - *Pure majority vote, both directions* (Sergei's call, over a proposed monotonic variant
        that could only add redactions). If `PII_COMPANY` wins 10-to-1 the odds are it is a
        company, and refusing to relabel forks one value into two placeholders. Accepted
        consequence: this is the first mechanism in the tool that can un-redact something a
        per-page run would have redacted — hence the vote tally in the `--report` group table,
        which is an audit surface rather than decoration. Ties go to class priority, ordered so
        ORGANIZATION (the one kept class layer 0 emits) can never take one.
      - *Borrowed matching stays exact-or-squash*, per the standing rule that fuzzy is
        admissible only under a box, plus an alphanumeric word-edge guard — exact matching has
        no length floor by design (Wu, Ng, NAB, ANZ), which is safe under a box and unbounded
        document-wide (`Wu` inside `Would`).

      **One bug caught by its own test**: `fuzzy.distance` gained a `cost` parameter for the
      second table, but the DP body still called `substitution_cost` directly, so the strict
      table was silently ignored —
      `test_measured_digit_confusions_do_not_discount_identifiers` failed on the first run and
      named the cause exactly.

      **Not fixed, and not claimed**: the tier-3 cry-wolf item in [TODO.md](TODO.md) ("a
      box-only paint does not suppress a later identical finding"). It was expected to fall out
      of this and does not — that case has no OCR text on the page at all, so the borrowed pass
      cannot see it either.

      **Text and CSV untouched**: they already locate every occurrence document-wide, so
      grouping would only add cross-window class consistency there. Worth its own measured
      change. `locate_in_text` also still carries the unbounded-needle hazard the new
      `locate_borrowed` guards against; logged rather than fixed here, to keep the text-tier
      numbers out of this change.

      Testbench: `tests/pii/core/test_grouping.py` (clustering, the two tables, the vote and
      its tie-break), borrowed-matching cases in `test_locator.py`, cross-page and
      cache-lifetime cases in `test_pdf_mode.py`, the two-way vote and multi-occurrence painting
      in `test_image_mode.py`. 388 green, model-free. **Corpus half of the dual-coverage rule
      still owed** — `--modality pdf` needs a real corpus, so a synthetic multi-page probe has
      no home today; the real-corpus run is the measurement that matters and had not been made
      at the time of writing.

- [x] **The "NOT redacted" line stops crying wolf on the tier-3 residue**
      *(2026-08-11, immediately after the grouping work, which was expected to fix this and
      did not — the borrowed pass searches OCR text and this case has none.)*

      The case, from the first hybrid run (2026-08-09): the model boxes a value OCR cannot
      read, so it paints at tier 3 — which leaves no char span. The prompt asks for every
      occurrence, so the model reports the same value again with no box of its own; nothing
      marks that one redundant (containment in a char span is the only test there is), and it
      lands on `unlocated`, printed as an outright leak although the pixels were painted.
      Observed on the insurance page with an address; re-OCR of the output confirmed nothing
      leaked.

      The original item asked for a semantics decision first and then answered it ("the second
      is honest and cheap") — Sergei cut through the framing: *why exactly cannot we change the
      message?* Nothing could; it was a settled preference recorded as an open question.

      `Placement.value_painted_elsewhere` is set by `locator._mark_painted_elsewhere` for any
      unplaced finding whose squashed value was painted anywhere on that page, and
      `_report_geometry` gives that group its own line. Three properties held deliberately:

      - **Not a suppression.** Containment in a char span is *positional* evidence and may
        suppress a later finding; value identity is not, because two occurrences of a value can
        genuinely sit in two places. So the second line does not say "safe" — it says an
        identical value was painted elsewhere and the operator must check whether this is the
        same printing or a second, still legible one. The tool cannot decide it.
      - **Still counted.** The finding stays on `unlocated`, per the standing invariant that an
        unplaceable detection must keep reaching the caller as a count. The new list is a
        subset, not a diversion.
      - **Both warnings survive** as separate `RuntimeWarning`s, so the deduplication argument
        for counting rather than only warning is unaffected.

      Regression tests in `test_locator.py` reproduce the insurance-page shape (a boxed value
      over unreadable pixels plus an unboxed twin) and its negative (an unplaced value painted
      nowhere).

- [x] **Multi-page synthetic corpus — the cross-page path becomes measurable**
      *(2026-08-11, Sergei: "the next step would be to improve the corpus to have multiple
      pages (1-3 is enough)". Both scoping questions agreed: templates change for real
      (baseline reset accepted), and the total stays near 20 pages.)*

      Documents now span 1-3 pages. The design pivot is **where the page break lives**: it is
      a form feed emitted by the templates (`build.Doc.page_break`), i.e. a character in the
      SOURCE TEXT, so pagination is described once and the text tier, the image tier and the
      PDF tier cannot disagree about it — and annotation offsets are untouched, so truth needed
      no new concept at all. The alternative considered and rejected was repeating a header as
      render-time furniture: that would put PII on the image that is not in the text and break
      the paired-corpus attribution the image tier exists for. CSVs are the one exception —
      a form feed inside a CSV breaks the parse — so their tables are cut by row count with the
      COLUMN header repeated, which is furniture carrying no PII.

      **Pagination alone would not have measured anything.** The symptom is a value detected on
      one page and missed on another, so the corpus has to REPEAT entities across pages:
      `legacy_statement` grew a continuation header reprinting the account number on every page
      and the holder in title case, against a caps form on page 1. That pair is deliberate — it
      is the case-folded comparison in `grouping.py` under test, and a corpus printing one form
      only would pass whether or not the folding worked. The page-1 caps occurrence is
      unconditional (`HELD BY:`) because the pre-existing addressee line is an rng draw, which
      would have made the probe a two-in-three lottery. `loan_application` splits 1+1 with its
      own continuation header. The name-forms statistics doc stays one page: every row is a
      different person by construction, so pages there buy nothing and cost minutes each.

      `render` writes one PNG per page and assembles them into a PDF per document, so
      `--modality image` (page at a time, no cross-page knowledge — the control) and
      `--modality pdf` (the two sweeps) run over **identical pixels** with grouping as the only
      variable. `score_pdf` grew a loader for the second corpus shape, discriminated on who owns
      the truth: a real corpus carries its own hand-authored `truth.json`, a rendered one's truth
      belongs to the text corpus it came from.

      Seed 42: 12 docs / **26 pages** (statements 3, loans 2, transaction CSVs 1-2, names 1) —
      above the ~20 target; the lever if a run is too slow is `--docs`, not the page shapes.

      **A reporting bug fell out of building this.** The first end-to-end run (real OCR, stubbed
      detector naming values on page 1 only) reported 2 borrowed spans per continuation page,
      but one of them was the account number — which layer 1 catches by its own label anyway and
      was never going to leak. `borrowed` now counts against what the page would have redacted
      ALONE (`merge_detections` over its own layer-0 placements, layer 1 included), so the number
      means "coverage that exists only because other pages were read". It dropped to 1 per page:
      the holder's name, which layer 1 has no detector for. Re-OCR of the output confirms the
      account number survives nowhere.

      Testbench: pagination, header repetition and the caps/title-case pair asserted in
      `test_generate.py`; form-feed splitting, uniform page rasters, CSV row splitting and the
      PDF assembly in `test_render.py`.

      **Baseline reset, knowingly**: the text-tier corpus changed shape, so tier-1 numbers from
      before this date are not comparable. Seeds must be regenerated (`generate` + `render`).

- [x] **Fuzzy matching for borrowed values — truncation and OCR damage**
      *(2026-08-11, reported and fixed the same day. Design argued with Sergei before any code;
      the distilled rule is in [ARCHITECTURE.md](ARCHITECTURE.md) "A page is not the unit of
      truth", the invariants in [../CLAUDE.md](../CLAUDE.md).)*

      **The specimen.** A real run's `pii_map.json` carried `"sk business trust": "PERSON_5"`
      while `SK BUSINESS TRUS` **leaked** on the same document. Both certain tiers of
      `locate_borrowed` fail structurally: the needle is a strict SUPERSTRING of what the page
      prints, so `find` misses in exact space and in squashed space alike. It was filed as a
      watch item that morning and hit within hours, which retired the "measure before designing"
      position it had been filed under.

      **Two mechanisms, one fix.** A page differs from a known value either because the DOCUMENT
      truncated it to a fixed-width field, or because OCR damaged it. An anchored prefix rule
      (the first proposal) covers only the former; weighted edit distance covers both, since
      truncation is deletions at the end. Sergei pushed for the general form — *"we can cover
      more cases with fuzzy matching... we don't want to match 'sk' everywhere, but we don't want
      to miss 'sk business tru' and 'sk 6usiness trust' either"* — and it is the simpler design:
      one mechanism instead of two.

      **Why edit distance is admissible here at all**, against the invariant that forbids it:
      the objection ("page-wide edit distance always finds something, somewhere, wrong") was
      argued for `locate_findings`, where placements COMPETE — a needle landing wrong
      over-paints there AND leaves the real occurrence unclaimed, a leak plus an over-strip.
      Borrowed needles do not compete: every occurrence is marked independently, nothing is
      consumed, so a spurious match is purely additive over-strip. The needle is corroborated
      too — already detected and already located elsewhere in this document. The rule stands
      unchanged for `locate_findings`; the invariant in `pii/CLAUDE.md` was rewritten rather
      than dropped.

      **Two bugs the tests caught, both in the first run:**

      - *Bucket order let a worse match win.* Runs are bucketed by squashed length and were
        scanned in ascending order, so `BUSINESS TRUS` (3 edits) claimed the region before
        `SK BUSINESS TRUS` (1 edit) was even tested — purely for being shorter. Candidates are
        now collected and claimed CLOSEST FIRST.
      - *The strict table did not actually forbid a digit swap.* `identifier_substitution_cost`
        prices digit-against-digit at infinity, but edit distance simply routes around it with a
        delete plus an insert for exactly 2.0 — and a 10-character needle's budget was 2.0, so
        `8936117499` still matched `4936117499`. Fixed by capping identifier budgets at 1.5,
        **derived rather than tuned**: below the cost of the detour, so no budget can pay for it.
        What that costs is truncations of 2+ characters on identifiers specifically; one-character
        truncations and any number of cross-class confusions (0.25 each) still match, which are
        the cases that occur on identifiers.

      **The corpus had no probe for this, and the one that looked like it was not.**
      `ORGANIZATION_ATF` prints `ATF DECKER FAMILY TRU`, but the full form in the same document
      is `DECKER INVESTMENT TRUST` — a *different* derived name, not a truncation of anything the
      model would have named, and correctly not matched. Verified rather than assumed. A real
      probe was added instead: the statement continuation header reprints the account name
      truncated by two characters (`ORGANIZATION_TRUNCATED`, its own truth type per the
      known-hard-form convention). It isolates this mechanism and nothing else — the truncation
      also removes the legal-form marker `org_policy` keys on, so layer 1 cannot rescue it.

      **Verified end to end on real pixels**: with the model naming only the full
      `KOCH MANAGEMENT PTY LTD` on page 1, page 2's `KOCH MANAGEMENT PTY L` is matched through
      real PaddleOCR output.

      **Cost measured, then reduced 2.7x** (Sergei: *"~6 ms per needle kinda sucks. is it
      levenshtein implementation that slows things down or something else?"* — it was). Profiling
      put the whole cost in `fuzzy.distance`: 25k calls at 55 us each, ~0.3 us per DP cell of
      which 0.14 us was the `lru_cache`d `substitution_cost` FUNCTION CALL. The run index is
      built once per page (3.2 ms) and is not a factor. Two fixes, both inside the DP:

      - the substitution table is read as a per-row dict instead of a call per cell
        (`CONFUSION_COSTS` / `IDENTIFIER_COSTS`, built once at import; digit-against-digit is
        materialized as 90 explicit infinities so the lookup stays a single `.get`) — 55 -> 37 us;
      - the DP computes only the **diagonal band** of width `ceiling`, since reaching a cell
        `|i-j|` off the diagonal costs that many indels — 37 -> 20.6 us, and it scales with
        length where the table fix does not.

      Net: 6.9 -> 2.6 ms per needle. The rewritten loop is pinned against the textbook recurrence
      over 400 random pairs x both tables x four ceilings, because a wrong edit distance here
      changes what gets redacted silently; `substitution_cost` survives as the readable statement
      of the semantics with a test asserting the tables agree with it.

      **A prefilter was measured and rejected, and the reason is worth keeping**: the obvious
      character-presence bound ("characters of the needle absent from the run > budget") is
      **unsound** — a confusion substitution costs 0.25, so a missing character can be paid for at
      quarter price, and the filter would reject real matches. The sound version counts only
      characters with NO confusion partner (adfkmnpqrxy — every digit has one), which do cost a
      full 1.0 to lose; it rejects 15% on realistic needles and does not earn its complexity.
      Revisit if the text-only regime lands and a page drops to seconds.

- [x] **The two-pass split stops paying for the image twice — a llama.cpp fix, not a `pii/` one**
      *(2026-08-13. Full record in
      [reports/2026-08-13-qwen36-ssm-prompt-cache.md](reports/2026-08-13-qwen36-ssm-prompt-cache.md);
      the distilled rule is in [ARCHITECTURE.md](ARCHITECTURE.md) "Detection and grounding are
      separate model passes", the invariant in [../CLAUDE.md](../CLAUDE.md).)*

      **The premise that turned out to be false.** The two-pass regime was adopted on the
      strength of "a second pass on a page already seen costs ~16 s because llama.cpp caches
      image prefill" — measured, correctly, on Qwen3-VL-8B. It does not survive the move to
      Qwen3.6: every `localize` call was re-projecting the whole page, ~50 s of vision tower,
      on every page of every document. Nothing reported an error; the only symptom was the
      wall clock, which is why it stood for days.

      **Root cause is the architecture, not the platform.** Qwen3.6 is hybrid SSM+attention
      (`ssm.*` keys in the GGUF, both the 27B dense and the 35B MoE). Recurrent state is a
      running summary: extendable, but not truncatable back to an arbitrary earlier position.
      So when the longest common prefix ends mid-sequence — which is exactly where it ends when
      two requests share an image and differ in the text after it — the server cannot roll back
      and discards the entire prefix. A CUDA control on Qwen3-VL reused 8586 of 8596 tokens in
      the identical request shape, which isolates architecture from backend.

      **The escape existed and was unreachable.** Context checkpoints are the designed answer
      for memory that cannot do partial removal, and they did not help: the only useful
      checkpoint position is immediately after the image, and upstream suppressed checkpointing
      on exactly that iteration (`do_checkpoint = do_checkpoint && !has_mtmd`). The next
      opportunity came after the trailing text, by which point the position had advanced past
      the divergence, so the checkpoint was created, rejected and erased on every request
      regardless of `-ctxcp` or `-cms`. Removing the suppression is a two-line change and lands
      exactly one extra checkpoint. Measured 95x on the second request with byte-identical
      replies.

      **Shipped in the serving layer, not in `vlm.py`.** The alternative — restructuring pass 2
      as a multi-turn continuation — was measured to work equally well (885 ms) and rejected:
      it carries pass 1's prompt and reply into pass 2's context, so localization quality would
      need re-gating, where the server patch is transparent to the model. Deployed by
      repointing every `serve.sh` at the local build and adding `-ctxcp 4`; `/opt/llama.cpp`
      retired. Smoke test over 4 real pages: localize prefill 59 s → ~0.5 s on every page,
      ~237 s saved on one document. Not yet compared against the stock server — the outstanding
      check is a determinism diff over the corpus, not a recall score.

- [x] **A value that wraps inside one column of a two-column page was reachable by no search**
      *(2026-08-13. Distilled design in [ARCHITECTURE.md](ARCHITECTURE.md) "A value is one span
      or several"; invariant in [../CLAUDE.md](../CLAUDE.md); corpus probe `ADDRESS_WRAPPED`.)*

      **The specimen.** Page 2 of an insurance certificate (`116832820_7_...`), reported by
      Sergei: layer 0 found the postal address, spanning two lines, with a box tight around
      both, and OCR read both lines perfectly — and the locator placed it `kind="box"`, tier 3.
      Open as a TODO since the first `--debug` run (an address and a vehicle description, both
      landing on tier 3 with the text plainly on the page); the guess recorded there —
      "the linearized page interleaves other column content between the value's parts" — was
      right, and the two fixes it proposed were both rejected on measurement, see below.

      **Cause, and it is not in either input.** `ocr_page._rows` bands a page VISUALLY, which is
      what puts a label beside its value and is load-bearing for context promotion. The page is
      two cards side by side, so every band holds both, and the assembled string reads:

          7 | Start date 13 March 2024 12:00am AEST Postal address 24 Stacey Dr
          8 | Expiry date 12 March 2025 11:59pm AEST Carrickalinga SA 5204

      Forty characters of the left-hand card sit between `Dr` (ends 471) and `Carrickalinga`
      (starts 511). Exact misses; squash misses, because the interloper is alphanumeric and the
      squash only collapses separators; `_fuzzy_windows` built contiguous word runs, so any
      window holding both halves also held the expiry date and blew the length guard. Replayed
      against the real OCR with a *perfect* box, all three transcriptions the model might return
      (`, ` / space / newline between the halves) resolved to tier 3, and `locate_borrowed`
      returned nothing at all. `test_a_value_wrapped_across_lines_is_one_span` had passed
      throughout — its fixture is single-column, where squash bridges the newline.

      **It cost both directions on the one page.** Tier 3 pads by 0.6x box height, and a
      two-line-tall box padded that far, painted after the plan, swallowed the phone number's
      own correctly-placed `PHONE_NUMBER` box — the Contact number row came out blank. And the
      same address printed again at `Usually parked at` (lines 19/20, wrapped identically) was
      matched by nothing and stayed fully legible in the output.

      **Fix: the needle drives the search, not a scan of the page.** Inside a box there is only
      one column, so a wrapped match is assembled per line — each line contributing one run of
      whole words that continues the needle exactly. Two shapes were built and the first was
      wrong:

      - A flat box-local assembly (covered words joined into one string, scanned for the
        needle) fixes the perfect-box case and *breaks* on a clipped one. Slack has to come from
        somewhere: at the outer ends only, a box that drops the word at the END of the value's
        first line (`Dr`) leaves it interior to the assembly and unreachable; per-line slack
        splices `11:59pm AEST` into the seam and kills the match outright. Measured on the real
        page — a box clipped by 12 model-units resolved to `24 Stacey` + `Carrickalinga SA`,
        which is a partial paint and therefore a leak where tier 3 had at least over-painted.
      - The needle-driven walk has no such tension: a run only ever *starts* where the needle's
        next character does, so it can be offered the whole line and pick nothing up from it.
        Per-line slack becomes free, and the clipped box recovers the whole value.

      The flat assembly survives as the fuzzy tier's window source (character-for-character the
      old page slice wherever nothing is spliced in, and now able to span a wrap as well).

      **One value, one placeholder.** The halves are separate ranges of the page string, so they
      are separate `Detection`s; `Detection.full_value` carries the whole value as the pseudonym
      key, or one address forks into `ADDRESS_1` and `ADDRESS_2`. `_merge_overlaps` takes the
      winning member's key with the rest of its identity.

      **`locate_borrowed` needed the same tier** or the second printing keeps leaking, and with
      no box the constraint is geometric: consecutive lines whose pieces share an x-column —
      verified to be the load-bearing guard by disabling it, at which point `24 Stacey Dr` in
      the left card joins `Carrickalinga SA 5204` in the right. Squash-equality only there:
      unanchored plus wrapped plus fuzzy is three liberties at once. Additive, not a fallback,
      for the same reason the fuzzy tier is.

      **A regression the first cut shipped, caught on the specimen (same day).** A word of
      pure punctuation squashes to nothing, and `need_sq.startswith("", at)` is true at every
      position — so such a word joined any piece anywhere for free, and a piece of ONE of them
      consumed none of the needle while still counting as a proper prefix. The walk then
      carried it to the next line, where the real value completed the match. Every needle
      claimed whatever stray glyph sat on the line above it: `Sk Management Victoria Pty Ltd`
      took the hyphen out of the heading `Policy number - 116832820 07`, `Mr Sergei Kulik` took
      a card's `?` help icon, and both were painted and given placeholders of their own
      (`ORG_3 = "-"`). Found by Sergei asking why layer-1 boxes had no layer-0 counterpart —
      they had none because they were neither: they were borrowed. The risk was noticed while
      designing the walk and judged negligible; it is not. Every piece must earn at least one
      character of the needle.

      **One ranking change fell out of it.** `_place` ordered free candidates by overlap
      magnitude before edit distance, which hands a clipped box to whichever candidate fits
      *inside* it — a truncation of the value beating the whole of it. Being in the box at all
      is the positional agreement; how much of it a candidate fills is not. Exact and squash
      score distance 0, so this only ever reorders the fuzzy tier.

- [x] **Skipping layer 0 entirely — `--layer0 off`** (Sergei, 2026-08-14). A mechanism to not
      run the semantic detector at all, asked for with three uses: a fast dry run, less
      sensitive data where speed is the priority, and debugging layer 1 in isolation. The
      current design is in [ARCHITECTURE.md](ARCHITECTURE.md) ("Skipping layer 0 is an
      explicit, reported downgrade"); this is the record of how it was decided.

      **It is the `--no-ner` capability, and the 2026-07-15 ruling was narrowed rather than
      reversed.** That ruling ("its name leaks made it unsafe") had been restated in five
      places as "a strip entry point always takes a detector — patterns-only must not be
      reachable by omitting an argument". Re-reading it before implementing anything: what
      made `--no-ner` unsafe was not that patterns-only output can exist but that it was
      reachable *silently*, and the harm is an operator who believes a document was
      semantically redacted when it was not. So the rule today is that patterns-only must not
      be reachable **by omission or by accident, and must never be silent**. Sergei's call on
      the narrowing, and on the scope: `strip` as well as `analyze` — full end-to-end, just
      skipping the VLM — because use case 2 produces a real output document.

      **The seam is a detector object (`vlm.NullDetector`), not a `layer0=False` parameter.**
      Considered and rejected: threading a boolean through `strip_text` / `strip_csv` /
      `strip_image` / `strip_pdf` changes four signatures and re-creates the exact
      reachable-by-omission risk the invariant exists to stop. A detector that answers nothing
      leaves every entry point *unchanged* — they still require a detector, so the invariant
      survives literally — and the degeneration was already correct and already tested:
      `merge_detections([], text)` reduces to `PiiPipeline.detect`, pinned since 2026-08-09 by
      `test_layer1_alone_when_the_detector_finds_nothing`. Total core change: one ~10-line
      class plus a `layer0` class attribute on each of the three detectors.

      **The image/PDF path was the pleasant surprise.** With no findings, `read_page` still
      OCRs and linearizes, and the back end still paints and reassembles, so the mode becomes
      OCR → linearize → layer 1 → paint with no code changes at all. That is regime 3 of the
      TODO's vision/text switch item minus the text detector. Measured on a one-page synthetic
      PDF: **36 s wall, of which ~30 s is the paddle model load**, against the ~300 s/page a
      vision run costs — the speedup is real because the cost was model prefill, not OCR.
      The `layer0` attribute is a string (`vision`/`text`/`off`) rather than a boolean or a
      type check precisely so those two planned switches extend it without touching its readers.

      **Three guardrails, built with it.** (1) `--geometry vlm` is refused in combination:
      that path never runs OCR, so with layer 0 silent there is no text for layer 1 either and
      the run would write an unredacted copy of the input — the one failure mode an operator
      cannot see. (2) The warning is ungated by `--report`, because what a run did not look
      for is not a reporting detail, and it is printed *before* the results so a plausible
      list of redacted identifiers is never read innocently. (3) The debug findings listing
      records `summary.layer0`, which is what disambiguates zero — an empty listing otherwise
      reads as "the model found nothing", the same confusion `DetectorResult.incomplete`
      exists to prevent one level up. Sergei ruled that `map.json` deliberately does NOT carry
      it: the map is the rehydration contract, and run provenance belongs in the debug artifacts.

      **Verification.** Fast suite **565 passed** (was 553: +4 `test_vlm.py`, +2
      `test_text_mode.py`, +5 `test_cli.py`, +1 `test_debug_overlay.py`; one existing
      findings-summary assertion updated for the new key). The CLI test that proves the point
      stubs nothing — `test_layer0_off_needs_no_model_server` would fail if a server were
      contacted, so passing is the assertion. The cost is pinned as a test rather than left to
      prose: `test_layer0_off_redacts_identifiers_and_leaves_names` asserts the TFN goes and
      "Olga Petrova" and "14 Bourke St" stay. End-to-end smoke runs (text, analyze, and a
      1-page PDF with `--debug all`) confirmed the warning, the refused geometry combination,
      `summary.layer0 == "off"`, and a `map.json` carrying only layer-1 classes.

      **Refinement the same day, after Sergei asked whether layer-0 debug output is worth
      generating at all under the flag.** It is not, and the reason is stronger than
      redundancy: `_layer0_segments` and `_locate_segments` both iterate `PageDebug.placements`,
      so with no detector `--debug all` wrote two overlays that were *unannotated copies of the
      original page*, plus an empty findings listing. The debug set is near-PII by its own
      warning (`_debug_note` says to keep it local, like the map file), so those were two extra
      unredacted copies of the source document carrying no diagnostic information — a liability,
      not clutter. `DebugSpec` gained a `findings` flag and `debug_overlay` a
      `drop_layer0_layers` helper; `_debug_spec` in the CLI applies both once, so `--image` and
      `--pdf` inherit one decision instead of two conditionals.

      Distinct from the empty `layer-0` overlay under `--geometry ocr`, and the difference is
      why this is not a general "never draw an empty layer" rule: there layer 0 RAN and only its
      boxes are missing, `locate` is populated, and the emptiness is the truth about that regime.

      Sergei left the explicit-request case to judgement (skip silently or warn); chosen: skip
      with a note naming the dropped layers, always — including under `all`, where the note is
      what explains a two-file list against the four that were asked for. If every requested
      layer needed layer 0, no debug output is written at all rather than a blank render.
      `summary.layer0` was kept but re-justified: with the off case no longer writing a listing,
      its job is naming the modality (`vision`/`text`) for the switches in TODO.md.

      Verified end-to-end on the same 1-page PDF: 2 overlays (`ocr`, `layer-1`), no
      findings.json, and the note printed. Fast suite **573 passed** (+8: 4 CLI unit tests over
      `_debug_spec`/`_debug_note`, 4 in `test_debug_overlay.py`; the summary test written earlier
      that day was reframed from the "off" case onto modality naming).

- [x] **Corporate licence numbers moved from kept to stripped** (Sergei, 2026-08-14, "for now,
      can be reconsidered later"). `AU_AFSL` and `AU_CREDIT_LICENCE` are now in
      `DEFAULT_STRIP_ENTITIES` and pseudonymize as `AFSL_n` / `ACL_n`. Design in
      [ARCHITECTURE.md](ARCHITECTURE.md) under the keep-list decision.

      **The change was blocked on something that had been invisible while the class was kept:
      both patterns matched their own LABEL.** The span for `AFSL 233714` covered the word
      `AFSL`. Harmless while nothing was replaced; a bug the instant it strips, and exactly the
      one the standing invariant records — the pseudonym map would key on `"AFSL 233714"`, so an
      unlabelled occurrence of the same number forks into `AFSL_1` and `AFSL_2`, and the output
      loses the word that says what the number is (`Advice under AFSL_1`). Rewritten as a
      lookbehind, matching the idiom every other labelled rule already used (TFN, Medicare, ABN,
      ACN, card). Verified across all five label spellings — `AFSL n`, `AFSL number n`,
      `ACL no. n`, `Australian Credit Licence n`, `Financial Services Licence n` — each yielding
      a digits-only span with the label left standing.

      **Kept reversible by construction**, because the decision is explicitly provisional. They
      retain their own entity classes rather than folding into a generic identifier, so a report
      still discriminates them from `AU_DRIVERS_LICENCE` — the reason the rules exist at all —
      and re-keeping them is an `[AU_AFSL]` section in an operator's `--entity-keep` file.
      Pinned by `test_a_corporate_licence_is_reversible_by_the_keep_list`, which writes the
      section through the real file loader rather than constructing an `EntityKeep` by hand.

      Placeholder prefixes `AFSL` / `ACL` (Sergei's pick; `acl` is already an accepted label
      alias in the pattern). Without entries in `PLACEHOLDER_PREFIXES` the fallback is the raw
      entity type — `AU_AFSL_1`, off-style beside `TFN` / `ABN` / `ACN`.

      Dual coverage per the standing rule: `test_corporate_licence_numbers_detected_and_kept`
      was inverted and renamed (it now asserts the digits go and the labels stay), and the
      pii_eval probes in `templates_text.py` moved to `strip_expected=True` with the probe value
      narrowed to the bare number to match the new span. In `tests/pii_eval/test_generate.py`
      both types moved from the keep-probe loop to the strip loop, deliberately NOT gated: a
      provisional decision must not become a release blocker. Fast suite 574 passed.

      Noted while measuring this, NOT acted on (Sergei: "we'll come back to it later"):
      `JointNameRule` fires on two-initial brands followed by a capitalised word — `P&O Cruises`,
      `H&M Stores`, `R&D Team`, `Q&A Session`, `M & S Food` all detected as PERSON, and
      `Paid H&M Stores 42.00` strips to `Paid PERSON_1 42.00`. The rule's guards (single-letter
      sides, case sensitivity, corporate-word rejection, corporate-tail lookahead) hold against
      `Smith & Jones`, `Marks & Spencer`, `Johnson & Johnson` and `AT&T Wireless`. Over-strip,
      not a leak, and the shipped keep list cannot reach it — its sections are ORGANIZATION,
      PHONE_NUMBER and ADDRESS, while these surface as PERSON. A `[PERSON]` keep section does
      fix it (verified). Also stale and left alone: ARCHITECTURE.md's joint-name section still
      describes a shared-surname pattern @0.45 that was removed 2026-07-21 (issue #4) and given
      to layer 0 — the rule has one pattern today.

- [x] **`JointNameRule` deleted; joint names are DERIVED from known people** (Sergei,
      2026-08-14, *"it is impossible to implement with regexps only without an external
      knowledge... start from scratch"*). New module `pii/core/derived.py` — layer 1, pass 2.
      Current design in [ARCHITECTURE.md](ARCHITECTURE.md); this is how it was arrived at.

      **What the old rule actually did, measured before deleting it.** It fired on every
      two-initial token followed by a capitalised word: `P&O Cruises`, `H&M Stores`, `R&D Team`,
      `Q&A Session`, `M & S Food` all detected as PERSON, and `Paid H&M Stores 42.00` stripping
      to `Paid PERSON_1 42.00`. Its guards held only against the cases they were written for
      (`Smith & Jones`, `Marks & Spencer`, `Johnson & Johnson`, `AT&T Wireless` — all correctly
      untouched). The shipped keep list could not recover any of it: its sections are
      ORGANIZATION, PHONE_NUMBER and ADDRESS, while these surfaced as PERSON — a `[PERSON]`
      section does fix it (verified), which is what made the class of failure obvious.

      **Three corrections from Sergei during design, each of which changed the code:**

      1. *"this should be done as a second pass of level 1"* — my first proposal put the pass
         inside `merge_detections` as a consumer of layer-0 output. Wrong framing: layer 1 may
         grow a PERSON source of its own (an NER recognizer, an allow/deny list), and a rule
         reaching for the VLM's findings specifically would need rewriting that day. Pass 2
         consumes DETECTIONS, blind to which layer produced them. `merge_detections` remains the
         only production caller because it is the only place both span sets exist.
      2. *"surname is usable and can be converted to PERSON"* — I had written that an initials
         form decomposes to nothing usable (`E Moore` names nobody). True of the constituents,
         false of the surname: `E & J MOORE` proves MOORE is a person's surname, so a bare
         `MOORE` in a transaction line strips, which nothing else in the stack catches. Widened
         while implementing: EVERY joint form contributes its surname, not just the initials one
         — keying it to the form gave `E & J MOORE` better bare-surname recall than
         `Emily and John Moore`, which tells us strictly more. Backwards, so both contribute.
      3. *"surnames can be multiple consecutive words"* — already satisfied by using the longest
         common TRAILING word sequence rather than the last word, which was chosen for a
         different reason (the literal reading "any word in both" breaks on `John Smith` +
         `John Brown`, sharing `John` and hunting for `S & B John`). Verified on
         `Emily and John van der Berg` → surname `van der Berg`, and the initials form
         `J & E VAN DER BERG` found from it.

      **A contract point a test flushed out.** `parse_joint("R&D Team")` returns a parse rather
      than None, and that is correct: the function answers "what joint form is this value",
      never "is this a person" — a detector already decided that. Deciding personhood at that
      level is precisely what the deleted rule attempted and could not do. The protection lives
      one level up, where nothing calls it on a value no layer detected.

      **Placeholders.** `PERSON_JOINT` → `JOINT_n`, its own class because the span names two
      people at once and PERSON would assert a third identity for two humans. Each surface form
      still takes its own placeholder (`JOINT_1` for the header form, `JOINT_2` for the initials
      form) — consistent with how every other class keys the map on the value, and the
      alternative would have rehydration restore a different surface form than the document had.
      Noted as a deliberate choice, not an oversight.

      Verified end-to-end through `strip_text` with layer 0 naming only the header couple and
      the two merchants:

          STATEMENT - account holders JOINT_1
          14 Jul  OSKO P12345678 JOINT_2 RENT
          15 Jul  Loan Repayment JOINT_3
          16 Jul  Direct debit PERSON_1
          17 Jul  Paid ORG_1
          18 Jul  Transfer to ORG_2

      `test_registry_policy` tightened from "PERSON only via JointNameRule" to "no registry rule
      claims ADDRESS, DATE_OF_BIRTH or PERSON". `test_joint_names.py` rewritten around
      `merge_detections` (the production path; `pipeline.strip` is pass 1 and shows no joint
      names). Fast suite **592 passed**.

      **Left open — the corpus and the critical gate.** `personas.couple()` synthesizes the
      second partner by overwriting their surname, so the two full names do not reliably appear
      in the document: sampled seed 42, `E & J MOORE` has `ERIC MOORE` / `JOSEPH MOORE` present
      and resolves, `R & E ROCHA` has the surname appearing exactly once — inside the joint form
      itself — and cannot. `PERSON_JOINT` is one of two gated probes, so this is a gate decision
      and it is Sergei's: (a) make the couple's full names appear in the document (a real
      statement header names both holders) and keep the probe gated, adding a NON-gated probe for
      the evidence-less form so the loss is measured; or (b) leave the corpus and drop
      `PERSON_JOINT` off the gate. No `pii_eval` change made pending that call.

      **Corpus and gate, settled (Sergei chose (a), 2026-08-14).** `Pool.holders` is now a
      STABLE account-holder couple for the whole run, printed in full in the statement header
      (`JOINT ACCOUNT: ERIC SMITH AND CASSANDRA SMITH`, ground-truthed PERSON_JOINT because the
      value IS one and layer 0 reads it as a single span), and `txbank.description` draws its
      joint forms from that couple instead of a fresh `pool.couple()` per transaction line. So
      the initials form in the transactions is derivable by construction rather than by luck,
      and `PERSON_JOINT` stays in the critical gate. It is also what a real joint account looks
      like — the previous corpus put an initials form in the text whose constituents appeared
      nowhere, which no reader would call realistic.

      **The evidence-less case is measured, not deleted.** New non-gated probe
      `PERSON_JOINT_NO_EVIDENCE`, built from `Pool.unknown_couple()` — a surname drawn from a
      reserved list (`UNRELATED_SURNAMES`) and checked against every person in the pool, so the
      probe means the same thing on every seed instead of sometimes colliding with a real person
      and quietly becoming detectable. `strip_expected=True` and expected to FAIL: it is a leak,
      just an accepted one, and marking it kept would hide it from the scorer entirely.

      Verified on a regenerated seed-42 corpus, stripped with layer 0 naming only the header
      joint form:

          JOINT ACCOUNT: JOINT_1
          20NOV22 OSKO PKTUPXPS87 JOINT_2 RENT              <- E & C SMITH, DERIVED
          04JUL22 ONLINE ... J & D KOWALCZYK                <- no evidence, survives

      Fast suite 592 passed.

- [x] **An AFSL number labelled the way real footers label it was detected by nothing**
      *(2026-08-14, found by Sergei on `116832820_7_Insurance_Certificate.pdf` p2)*. The page
      footer carries two licence numbers — `AFS Licence No 285571` (the product issuer) and
      `AFS Licence 241411` (the managing agent) — and layer 1 matched neither. `AuAfslRule`'s
      label lookbehind accepted the acronym (`afsl`) or the full words
      (`(australian )?financial services licen[cs]e`), but not the half-and-half spelling that
      is arguably the most common one in print: `AFS` abbreviated, `Licence` spelled out. Fixed
      by widening the label to `(?:financial\s+services|afs)\s+(?:licen[cs]e|lic\.?)`, the
      second half on Sergei's call so the licence word may be abbreviated too (`AFS Lic 285571`,
      `AFS Lic. No 285571`); the label stays a lookbehind, so the span is still the digits
      alone. `lic` is admissible only because it is anchored to `afs` / `financial services` —
      it cannot fire on a bare `lic` elsewhere in a footer.

      **Mirrored onto `AuCreditLicenceRule`** (Sergei, same day), which now takes
      `credit\s+(?:licen[cs]e|lic\.?)` with `lic` anchored to `credit` the same way. No
      specimen prompted it: the two rules label the same kind of number in the same kind of
      footer, so a spelling one accepts and the other does not is a gap waiting to be found on
      a document rather than a distinction anyone intended — and the sibling docstring claims
      they share a label rule, which was about to stop being true.

      **The interesting part is why it survived a rule that already had dual coverage.** Both
      the pytest case and the `pii_eval` probe were written against the same single spelling
      (`AFSL <digits>`), so the test and the corpus agreed with each other and with the regex,
      and no run could disagree with any of them. Structurally identical to the `_SEP` failure
      of 2026-08-12 (`pii_eval/au.py` only ever emitted single-space groupings, so a
      double-spaced valid TFN matched nothing) and found the same way — on a real document, not
      by the harness. The recurring lesson is narrower than "add coverage": *a probe that
      exercises one surface form of a labelled or separated pattern measures the regex against
      itself.* Both new cases therefore add a spelling rather than swapping the old one.

      Dual coverage: `test_afsl_matches_the_half_abbreviated_label_a_real_footer_prints` pins
      six spellings (the two real specimens, the two abbreviated-word forms, and the two that
      already worked — so the widening cannot cost them) and re-asserts the label survives in
      each new form; `test_credit_licence_abbreviates_its_label_like_its_afsl_sibling` does the
      same four ways for the sibling; `templates_text.py` gains two `AU_AFSL` probes
      (`Product issuer AFS Licence No <n>`, `underwriter AFS Lic <n>`) and one
      `AU_CREDIT_LICENCE` probe (`broking under Credit Lic <n>`). The plural forms
      (`AFS Licences <n>`, `Australian Credit Licences <n>`) are deliberately still not matched
      — they do not label a single number.

      Verified end-to-end on the source PDF (`--layer0 off`, so layer 1 alone through the real
      OCR path): `AFSL_1 = 241411`, `AFSL_2 = 285571` in the map, where before the run produced
      no `AFSL` section at all. Under a full run the two were rescued by layer 0 as
      `IDENTIFIER_GENERIC` (`ID_2` / `ID_3`) — stripped, but unrefined and only because the
      model saw them. Fast suite 594 passed.

      Noted on the same page, NOT acted on (Sergei: accept as is): the policy number
      `116832820` clears the TFN mod-11 checksum, so `AuTfnRule`'s bare `\b\d{9}\b` pattern
      types it `AU_TFN` at 1.0 — the documented ~1-in-11 rate, landing on a real value. It
      strips either way; only the placeholder class is wrong.

- [x] **A label reaches its value by being near it ON THE PAGE — the character window is
      replaced by visual attachment** *(Sergei, 2026-08-14; design in ARCHITECTURE, the item it
      closes was written the same day in TODO.md)*. Built as one change rather than staged, on
      Sergei's call ("I'd aim for all 1-4 from the beginning. It feels the right and honest
      thing to do, and more generalizable"), with the attachment mode left switchable
      (`--context-attach window|layout`) so one corpus could be scored both ways and a
      regression attributed to a cause rather than to a release.

      **What was built.** `Layout` protocol + `Context`/`Attachment` records in `engine.py`;
      `TextLayout` (left-only, per Sergei — "let's only [use] left proximity for text now") and
      the retiring `WindowLayout` beside it; `PageLayout` in the new `layout.py`; the shared
      separator/filler vocabulary in the new `labels.py`; `attach=NEAR|STRICT` on `Pattern`;
      the label spellings of nine rules moved out of regex lookbehinds into `context`.

      **Four design points that only emerged while building it, all of them from real data:**

      - **The left band is a word COUNT, not a distance.** Measured on the specimen page
        (300 dpi, 34 px lines, 2396 px wide): the true label `Statement Enquiries` sits 462 px
        from its value and the false promoter `cheque` 748 px from its own, so no threshold
        separates them — but the false promoter is *nine words* back and the true one is two.
        A distance limit was written first and deleted.
      - **The `above` band selects REGIONS, not words.** Per-word x-overlap admits only the
        label word directly overhead, so `Account Number` above a short value contributes
        `Account` alone and a wrapped four-word licence label never assembles. OCR detection
        regions are already the visually-grouped unit and `PlacedWord.region_box` carries them.
      - **STRICT gates but does not boost.** A lookbehind's evidence is already priced into the
        pattern's declared score; boosting on top would double-count it. Not boosting is what
        made converting nine lookbeheads **score-neutral**, which is the only reason a change
        this wide could be trusted without re-tuning every rule.
      - **A label spelling is a stem, and must begin at a word boundary.** Open-ended stems keep
        the lists short (`account` covers `Accounts`, `afs lic` covers `AFS Licence`, and the
        gap is measured from the end of the label's own WORD so `ence` never lands in it), but
        `ac` — a real a/c-family form — would then match inside `across` and report `Across` as
        a label. Hence `labels.Exact` for spellings too short to be stems.

      **Measured, layer 1 alone, window vs layout on the same inputs.** Text corpus (seed 42,
      the eight generated documents): recall on strip-expected identifier truths **unchanged at
      75/90** — the same fifteen misses either way, all pre-existing (drivers licences have no
      layer-1 rule) — while false positives fell **54 → 39**. Real documents end to end
      (`--layer0 off`, real OCR): `AmplifyBusiness-…-24Sep2023.pdf` lost exactly three map
      entries, `ACCOUNT 013795`, `ACCOUNT 13 22 66` and `BSB 457 141` (the first two are the
      reported bug, the third the BSB-inside-an-ABN false positive that has its own TODO item),
      with the other eleven unchanged; `116832820_7_Insurance_Certificate.pdf` came out
      **identical**, both AFSL numbers included, which is the check that the migrated licence
      rule still works through OCR.

      **The `above` band proven on a rendered page**, not only in unit geometry: a corpus
      document rendered monospace, OCR'd, and detected both ways —

          window  ACCOUNT_LABELLED_ABOVE   84593961  STRIPPED  <- window 'Account'
          window  REFERENCE_ACROSS_COLUMN  794022    STRIPPED  <- window 'cheque'   (the bug)
          layout  ACCOUNT_LABELLED_ABOVE   84593961  STRIPPED  <- above  'Account'  (the band)
          layout  REFERENCE_ACROSS_COLUMN  794022    kept

      Note the first row: the window got that one right *by luck*, the label happening to fall
      inside 60 characters. The band gets it right by rule.

      **Dual coverage.** `tests/pii/core/test_layout.py` (new, model-free — a page is a handful
      of `PlacedWord`s laid out by hand) pins the bands, the region unit, the two-line vertical
      reach, DPI-independence and strict attachment over a page; the attachment section of
      `test_engine.py` pins the strengths, the word floor, score neutrality and the audit
      record; `test_checksum_rules.py`'s helper now runs through the **Analyzer**, because since
      this change `rule.detect()` is half a labelled rule and testing against it would report a
      labelled candidate with no label anywhere near it. Corpus: `ACCOUNT_LABELLED_ABOVE`
      (strip-expected, non-gated — an image-tier hit and a known text-tier miss, since text mode
      is left-only by decision) and `REFERENCE_ACROSS_COLUMN` (keep-expected — 3/3 wrongly
      stripped under the window, 0/3 under the band).

      **The audit surface.** `Detection.attachment` reaches `--report`, so a promoted span says
      which word promoted it and from where (`AU_BANK_ACCOUNT 0.50 '0007 3111 4' <- left
      'Account'`). Diagnosing the original false positive meant reconstructing a 60-character
      window by hand; that is the failure this closes. `_merge_overlaps` carries the winner's
      attachment (and `recognizer`/`pattern`) onto the merged span, or the plan would arrive at
      the report with the provenance stripped off.

      Fast suite 619 passed. Accepted residual, named because it needs a specific layout rather
      than being hypothetical: a two-column line whose LEFT column ENDS in a label word, beside
      a right-column value with fewer than `word_floor` words before it, still attaches.

- [x] **The nearest label wins, and a match that straddles a column is not one value**
      *(Sergei, 2026-08-14, on `statement.pdf` p2 — "the 'above' would've been a better match as
      the 'left'")*. Two refinements to the attachment work committed the same day, both from
      one page.

      **What the page does.** Two columns: `Macquarie Transaction Account Statement` over
      `From 1 January 2022 to 30 June 2022` on the left, `Enquiries` over `133 174` on the
      right. Layer 1 matched **`2022 133 174`** as one grouped account number — the year of the
      date range plus the enquiries phone — and promoted it off `Account`, which sits above the
      LEFT half of that span. Measured: the gap inside the "value" is **1411 px against a median
      word gap of 33 px on that line, and a line height of 42** — 34 line heights.

      **Nearest wins, across bands** (Sergei's call; I had left-priority from his earlier
      instruction and asked whether this changed it). It is the rule already used *within* a
      band — the closest label is the one that introduces the value — and a fixed left-then-above
      order contradicts it as soon as a bogus left label outranks a good one overhead. Distance
      is edge to edge in line heights, kept per WORD rather than per band because a band holds
      several labels; centre-to-centre would make a long label read as distant merely for being
      long. Where a layout has no geometry there is one band and no distances, and the
      tie-breaks reproduce plain reading order — so text and CSV are untouched. Measured over
      the corpus and the specimen pages: **zero detection differences**, which is the expected
      shape of the change. It alters which label is CREDITED, and on the real pages that is
      visibly better — `13 22 66 <- left 'Enquiries'` where the window had credited a credit
      card's `Number`, and `13 33 22 <- left 'Phone'`.

      **`Layout.contiguous` rejects a span whose internal gap exceeds three line heights.** The
      deeper finding is why no existing guard could have caught this: `linearize` joins every
      word with ONE space, so the separator classes in `recognizers.py` — bounded at 1-3 spaces
      in 2026-08-12 precisely to stop two columns joining into one candidate — are structurally
      blind on the OCR path. A column gap and a word space are the same character. Only geometry
      knows, so the check lives with the layout and runs before scoring. The constant is not
      delicate: a printed space is 0.8 line heights and the specimen's column jump was 34.

      Considered and rejected: making `linearize` emit gap-proportional whitespace so the
      existing `{1,3}` guards start working by themselves. It would change the page string that
      every offset, needle and layer-0 text pass is built on, for a fix a span-level predicate
      makes without touching anything.

      **What it did NOT fix**, stated because it looks like it should have: the phone `133 174`
      is still undetected on that page. libphonenumber never offers it as a candidate while the
      preceding `2022 ` is glued to it — the same refusal that hid `13 22 66 8am-8pm` on the
      Amplify statement — so there is no candidate for the geometry to rescue. Layer 0 covers it
      in a full run.

      Dual coverage: four tests in `test_layout.py` (nearest wins across bands; a closer LEFT
      label still beating one overhead, so the rule is distance and not a new preference; a
      straddling match rejected; an ordinary word space kept). Corpus: `YEAR_ACROSS_COLUMN`, a
      keep-probe left column ENDING in a number beside a right column BEGINNING with one — the
      geometric sibling of the existing `AMOUNT_COLUMN` probe, which guards the same failure in
      text with a lookahead. Verified on a rendered, OCR'd corpus page:

          window  YEAR_ACROSS_COLUMN       2022    STRIPPED   (the bug)
          layout  YEAR_ACROSS_COLUMN       2022    kept
          window  REFERENCE_ACROSS_COLUMN  794022  STRIPPED
          layout  REFERENCE_ACROSS_COLUMN  794022  kept

      Fast suite 623 passed.

- [x] **The character window is retired: visual attachment is the only notion of "near"**
      *(Sergei, 2026-08-18 — "Yes, please go ahead")*. Closes the item written on 2026-08-14
      when the geometric attachment landed switchable. `--context-attach`, `WindowLayout` and
      `CONTEXT_WINDOW_CHARS` are deleted in one commit, so no run can silently take the old
      path afterwards; `PiiPipeline` loses its `attach` parameter and `layout_for` its branch.

      **It rests on the measurement already recorded above** (2026-08-14, layer 1 alone, both
      ways on the same inputs): text corpus recall unchanged at 75/90, false positives 54 → 39,
      `AmplifyBusiness-…-24Sep2023.pdf` losing exactly three map entries — all three false
      positives — and `116832820_7_Insurance_Certificate.pdf` coming out identical. Nothing was
      re-measured to make the flip; what was measured is the blast radius of the deletion.

      **The `Analyzer`'s no-layout default is now `TextLayout`, and that is the substantive part
      of the deletion.** `analyze(text, threshold)` fell back to `WindowLayout` regardless of
      `--context-attach`, so any caller that forgot to hand over a layout ran the retiring rule
      even in `layout` mode. No production path did (`image_mode.py` and `text_mode.py` both
      pass one; `PiiPipeline.plan` has no callers at all), but the trap was live, and deleting
      the class is what closed it. The replacement default loses column reasoning visibly — a
      caller holding a page must pass its `PageLayout` — rather than falling back to a rule
      nobody chose.

      **The blast radius, measured before touching the tree** by running the fast suite with the
      default forced to `layout` through a pytest plugin: **621 passed, 2 failed**, both of them
      tests pinning the retiring rule.

      - `test_context_window_is_bounded` died with `CONTEXT_WINDOW_CHARS`, as intended. Replaced
        rather than dropped, because what it pinned — that attachment is BOUNDED — still holds
        under the band: `test_a_label_beyond_the_word_floor_does_not_reach` asserts a label five
        words back on the same line promotes nothing.
      - `test_context_promotes_across_lines_on_the_whole_page` was **a fixture artifact, not a
        regression.** Its comment said "one line above" but its pixels put the value at
        `top=110` with 20 px lines — five line heights down, outside `V_ABOVE`. Under the window
        those coordinates were decorative, since a 60-character lookback crossed any gap. Fixed
        by moving the value to an actual next line (`top=40`), where the promotion still fires,
        and the five-line-gap case became its own test asserting it does NOT
        (`test_a_label_far_above_its_value_does_not_promote`) — so the fixture that was mute
        about geometry now pins both sides of the band.

      **Finding: every corpus run ever made has scored the retiring path.** None of the three
      eval scorers (`pii_eval/score.py`, `score_image.py`, `score_pdf.py`) passed `attach` when
      constructing `PiiPipeline`, so they all took the `window` default — including the tier-1
      gate. The 2026-08-14 both-ways numbers were taken by hand, layer 1 alone; nothing has ever
      scored layout with layer 0 in the loop. The scorers needed no edit — they inherit the new
      default — so from this commit on, every corpus run scores what production runs.

      **The tier-1 gate has NOT been run against this change**: no llama-server was reachable at
      commit time (neither `sergei-macbook-pro:8080` nor `localhost:8080`, `$PII_VLM_URL`
      unset), and the gate needs one for layer 0. It is the one check this record cannot claim.
      What it would add over the evidence above is the layer-0 half: the gate asserts zero
      CRITICAL misses, and `ACCOUNT_LABELLED_ABOVE` — the one entity this change costs on the
      text tier — is deliberately not a gate member, so the expected result is a pass.

      **Accepted, and unchanged by this commit:** on the text tier a label directly ABOVE its
      value no longer reaches it, where the window sometimes crossed the line break and got it
      right by luck. Left-only for text is Sergei's 2026-08-14 call, the image tier attaches it,
      and `ACCOUNT_LABELLED_ABOVE` keeps scoring the gap on every run. The vertical band that
      would close it is the surviving TODO item.

      **Verified end to end on both specimens** (`--layer0 off`, real OCR, `--pdf`): each
      reproduced its existing `.pii_map.json` — the ones Sergei's `run_pii.bat` had produced
      *with* `--context-attach=layout` — **byte for byte**. The certificate still reports
      `AU_AFSL 0.70 '241411' <- left 'AFS Licence'` and the same for `285571`. So the deletion is
      behaviour-preserving against the mode it makes mandatory, on real documents and not only
      in the suite.

      Also updated: the flag's paragraph in `pii/README.md` (rewritten as a statement of how
      attachment works, keeping the `--report` example), the `engine.py` row and the
      invalid-identifier `context` tier in `core/ARCHITECTURE.md` — which still described the
      evidence as "a context term in the 60-character window" — and a new ARCHITECTURE paragraph
      recording that geometry-free input gets the left band alone. `run_pii.bat` (untracked,
      Sergei's) passes `--context-attach=layout` and must drop the flag.

      Fast suite 624 passed.

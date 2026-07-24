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
      per-word conf recorded per error. Distilled into ARCHITECTURE.md ("Tesseract
      operational profile") and the `ocr.py` docstring.)*

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

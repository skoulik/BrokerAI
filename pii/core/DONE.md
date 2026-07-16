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
      First side-by-side, seed 42 (text → image): both predicted deltas confirmed.
      (1) **OCR-broken checksums**: AU_TFN 100% → 86% — one misread digit broke the mod-11
      check, Presidio rejected the value, it survived readable; caught only by the fuzzy
      matcher (gate FAIL, correctly). AU_DRIVERS_LICENCE 100% → 75%, same digit-run class.
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

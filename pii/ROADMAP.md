# Phase 1 Roadmap — PII stripping tool

This is the Phase 1 roadmap of the [BrokerAI revival](../ROADMAP.md) — the standalone,
local PII-stripping tool that lives in this `pii/` directory (eval harness in `../pii_eval/`).
It holds the full Phase 1 task list and its completed-task engineering records, split out of
the root roadmap to keep that document readable. Architecture and design decisions (the *why*)
are recorded in [../ARCHITECTURE.md](../ARCHITECTURE.md); usage is in [README.md](README.md).

Goal: locally strip personally identifiable information from documents so the stripped version
can be shared with cloud models. Prefer **pseudonymization with a consistent local mapping**
(`John Smith → PERSON_1` everywhere) over blank redaction, so cloud answers can be rehydrated
locally and analytical utility is preserved.

Input types to support:
- [x] Plain text *(2026-07-12: `pii/` package — see its README)*
- [ ] Images (scans, screenshots) — OCR with word-level bounding boxes, redact by painting
      over pixel regions
- [ ] PDFs — **treat as images**: render pages → OCR → redact pixels → reassemble PDF.
      Rationale: financial-sector PDFs often have junk/broken text layers, and rebuilding from
      pixels also eliminates the hidden-text-layer leak class entirely.
      *Decide later:* belt-and-braces variant that additionally scans any existing text layer
      to catch text the OCR misses (detection only — output still comes from pixels).
- [x] Bank transaction lists (CSV / statement tables) — column-aware handling.
      *(2026-07-12: CSV mode done — per-cell detection, `--columns` filter; statement tables
      from the image path still pending.)* Descriptions
      contain personal names, PayID emails/phones, BSB/account refs; these reveal spending
      patterns and allow re-identification. Keep merchant names (analytical value), strip
      person names — zero-shot NER labels (GLiNER2) distinguish person vs organization.
      Consistent pseudonyms per counterparty so patterns survive but identity doesn't.

Detection pipeline (layered — no single layer catches everything):
1. Pattern recognizers via Presidio, with **custom Australian entities**: TFN, Medicare number,
   BSB + account number, ABN/ACN, AU phone/address formats, PayID.
2. NER: GLiNER2-PII (~1.2 GB, runs anywhere) as Presidio engine.
3. Local-LLM audit pass: "does this still contain anything identifying?" — catches contextual
   identifiers NER misses ("the borrower's wife, a dentist in Wagga Wagga").

Tasks:
- [x] Standalone module/CLI, separate from the RAG app (shares the local model server)
      *(2026-07-12: `pii/`, layers 1–2 working: Presidio + custom AU recognizers, GLiNER.
      Findings: Presidio's AU recognizers need explicit registration; overlapping PII spans
      must be merged not ranked, or partially-covered spans leak; GLiNER needs per-line and
      de-capitalized passes for all-caps statement lines. LLM audit layer still pending.
      CPU-only torch is slow (~1 min/page-ish) — install CUDA torch for the 2080 Ti when it
      matters.)*
- [x] Consistent pseudonym mapping store + rehydration of cloud responses
      *(2026-07-12: JSON store, document-order numbering, case-insensitive value matching.)*
- [ ] Configurable strip-entity selection — let a run choose which data types to strip
      (e.g. names and addresses only). The pipeline already takes a `strip_entities` set
      internally; needs CLI exposure (`--entities` / named profiles) and documentation.
- [ ] OCR engine choice — *decide later:* Tesseract vs PaddleOCR vs Surya/docTR vs a local VLM
      (e.g. Qwen-VL class) doing OCR+PII detection in one pass. Start by benchmarking on real
      bank statements/scans.
- [ ] Metadata scrubbing on all output formats
- [ ] Barcode masking: mailing barcodes on statements (Australia Post 4-state, and 1-D codes)
      encode the delivery address/customer ref — text-based detection can't see them, so
      detect and paint over barcode regions in the image pass (observed on several of the
      reference examples)
- [ ] Overlaps merging algorithm — define and document. Interesting areas: how the weights are 
      combined (max, average, bayesian/aposteriori), what if winning classes of overlaps
      do not agree, should we merge at all in some cases.
- [ ] Log checksum-invalid identifiers. If an identifier candidate passes the detectors, but
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
        concrete input to the overlaps-merging task above).
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
- [x] Evaluate GLiNER2 (https://github.com/fastino-ai/GLiNER2) — why it exist, what it adds
      or improves compared to GLiNER, is it maintained, what license/usage terms.
      Result (2026-07-12): unified schema-driven extractor from Fastino (GLiNER lineage),
      Apache 2.0 incl. the PII model (fastino/gliner2-privacy-filter-PII-multi), actively
      maintained, open training code (fine-tuning on our synthetic corpus is possible).
      Implemented as selectable layer-2 backend (`--ner-backend gliner2`, see
      pii/gliner2_recognizer.py for tuning quirks). Tier-1 eval: PERSON 100% (== GLiNER),
      ~4.7x faster, no ALL-CAPS/context weaknesses; weaker on multi-part AU addresses
      (fragments them into street/suburb spans — pipeline-level adjacent-span merging,
      see overlaps task above, would close most of the gap) and 3 extra ORGANIZATION
      over-strips. Decision (Sergei, 2026-07-12): GLiNER2 is the default layer-2 backend;
      `--ner-backend gliner` keeps the old model available for comparison.
- [ ] LoRA adapter for Australian addresses on GLiNER2 — close the multi-part address
      fragmentation gap at the model level (GLiNER2 ships open training code and
      load_adapter(); pii_eval's generator can produce the training pairs). Revisit after
      the overlaps-merging task lands, which should already close most of the gap.
      *(2026-07-14: priority further reduced — the max_width=12 lift below closed the
      one-line-address fragmentation on tier-1; LoRA now only matters if real-world
      wide spans score poorly, or for the '53 MILES SUBWAY'-style bare street-line
      recall misses.)*
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
- [ ] Ablation: are the address workarounds still needed at max_width=12?
      Postponed (decision 2026-07-14) until the tier-1 corpus has more and more
      varied address examples — 12 ADDRESS spans from a handful of templates is
      too thin a basis for removing belt-and-braces protections. When picked up,
      fold it into the labels-per-pass experiment below (same mechanics: rerun
      the eval with the extra address passes disabled).
- [ ] Experiment: labels-per-pass (schema partitioning). Label competition
      suppresses sibling scores (documented in pii/gliner2_recognizer.py — the same
      span scores 1.0 alone vs 0.49 among siblings); addresses already get dedicated
      passes. Question: does everything benefit from isolation? Grid to evaluate on
      tier-1, per-class P/R + layer-2 latency:
      (a) all-in-one (current baseline, minus the address passes),
      (b) full isolation — one label per pass (~11 passes; each pass re-encodes the
          3000-char window, so expect roughly linear cost growth),
      (c) themed groups — e.g. semantic {person, org, address, DOB} split from
          numeric IDs {TFN, Medicare, phone, bank account, licence, passport},
      (d) current production config as reference.
      Hypotheses: isolation lifts recall on semantic classes (competition is what we
      pay descriptions to overcome) but hurts precision on confusable numeric IDs,
      where competition doubles as disambiguation — a lone "9-digit number" label
      will claim TFNs, ACNs and phone fragments alike. Numeric precision loss is
      partly tolerable since layer-1 checksum/regex recognizers dominate those
      classes and validation filters impostors. Expected sweet spot: a few themed
      groups, not full isolation. Sequencing: needs at least a provisional
      cross-pass overlap-resolution rule — best run together with (or right after)
      the overlaps-merging task above.

Evaluation (constraint: real documents are classified until stripped — cloud models can only
ever see synthetic/declassified data or aggregate metrics):
- [ ] **Tier 1 — synthetic corpus**: local generator with Faker + custom AU providers (TFN and
      Medicare with valid check digits, BSB/account, ABN/ACN, PayID), fake statement templates
      and transaction CSVs, degradation pipeline (DPI, skew, blur, JPEG artifacts) for OCR
      benchmarking. Ground truth known by construction → automatic precision/recall; the fast
      iteration loop, fully shareable. Sergey will supply a few unclassified-by-construction
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
- [ ] **Tier 2 — PII-transplanted real documents**: Sergey manually replaces real PII with fake
      in 4–6 real documents (one per major bank layout, one bad scan, one transactions CSV),
      keeping layout intact. Real layouts + known ground truth + declassified. One-time effort,
      reusable forever.
- [ ] **Tier 3 — metrics-only runs on the real corpus**: harness emits only aggregates (entity
      counts/type, confidence histograms, layer-disagreement rates, cross-OCR-engine
      disagreement). Local side-by-side review UI so manual acceptance checks are a quick
      click-through; only declassified findings are reported back.
- [ ] Scoring is recall-first and severity-weighted: acceptance = zero critical misses (TFN,
      account numbers, names) on the Tier 3 review set, not a single F1 number.

# TODO — PII stripping tool

All open Phase 1 tasks, with full working detail. The activity overview and evaluation-tier
plan are in [ROADMAP.md](ROADMAP.md); completed tasks and their engineering records are in
[DONE.md](DONE.md); design decisions (the *why*) in [ARCHITECTURE.md](ARCHITECTURE.md).

Grouped by theme. Suggested order on the image/PDF track (2026-07-14): PDF mode → demo on
the reference documents → pii_eval image tier → OCR bake-off.

## Next up — spaCy track (planned 2026-07-15)

Two tasks, in this order; they are independent — the review does not block the retirement.
Plan agreed with Sergei 2026-07-15; scope decisions recorded inline.

- [ ] Deep source review of spaCy — same drill as the gliner2-rs and presidio-image-redactor
      reviews (records in [DONE.md](DONE.md)): harvest knowledge/know-how/experience/
      things-to-avoid, not adopt. Target: the installed spaCy 3.8.13 (all `.pyx` sources ship
      in the wheel — review in place at site-packages, no clone) plus en_core_web_sm 3.8.0
      (its `config.cfg`/`meta.json` are reviewable too). Scope decided 2026-07-15 (~920
      source files make a full read infeasible): **focused core + architecture**.
      Core — the parts we depend on via Presidio, or are retiring:
      - tokenizer (`tokenizer.pyx`, `lang/en` punctuation/exceptions, char classes): the
        token boundaries Presidio's context enhancer lives on; explains recognizers.py's
        documented "a/c never survives tokenization into a context term" quirk; harvest
        rules affecting our label/context matching (infix `/`, hyphens, number+unit);
      - lemmatizer (`pipeline/lemmatizer.py` + `lang/en` rules/lookups): what
        `token.lemma_` actually is in en_core_web_sm — the input quality of Presidio's
        LemmaContextAwareEnhancer;
      - NER (`pipeline/ner.pyx`, `transition_parser.pyx`, `_parser_internals` BILUO
        transition system, `tok2vec.py` + MultiHashEmbed/Bloom embeddings in `ml/models`,
        and en_core_web_sm's config): explain from mechanism the failure modes we measured
        — cross-line glue spans, date-as-PERSON, total blindness to 'Wagga Wagga'/'Dubbo' —
        so the retirement rationale in ARCHITECTURE.md rests on mechanism, not just eval
        numbers;
      - pattern machinery (`matcher/matcher.pyx`, `phrasematcher.pyx`, entity/span/
        attribute rulers): token-level pattern DSL and trie-based phrase matching vs our
        char-level regexes; candidate harvest: a PhraseMatcher-style AU place-name
        gazetteer as a cheap LOCATION layer, token patterns robust to OCR whitespace;
      - span/overlap handling (`Doc.char_span` alignment modes, `util.filter_spans`,
        SpanGroup): their longest-first-greedy overlap resolution vs our recall-first
        union merge; char↔token alignment discipline vs our assembly-time interval
        recording in `ocr.py`.
      Architecture/engineering: the config/registry/factory system, Language pipeline
      composition and `pipe()` batching, model packaging (versioned pip package,
      compat ranges), serialization contracts (DocBin), Vocab/StringStore hash interning,
      the Doc memory model; testing/versioning practices worth stealing for a growing
      codebase. Method/deliverables as before: read in place, spacy.io/GitHub only to
      confirm findings; raw harvest appended to DONE.md (lettered findings), durable
      decisions distilled into ARCHITECTURE.md, actionable ideas become TODO items.
- [ ] Retire the last spaCy recognizer and remove the `--no-ner` regime. Ships the GLiNER2
      location label — experiment settled 2026-07-14 (11/11 vs 6/11 contextual towns, zero
      extra org over-strip, one fewer address leak; record in [DONE.md](DONE.md); this item
      subsumes the former "Ship the GLiNER2 location label" experiment item) — with the
      scope decision made by Sergei 2026-07-15: **drop the patterns-only regime entirely**.
      SpacyRecognizer disappears from the codebase; spaCy remains solely as Presidio's
      mandatory NLP engine (tokens/lemmas → context enhancer). Steps:
      - `gliner2_recognizer.py`: flip the `location` constructor default to True (flag kept
        for ablations); docstring — the location pass is now the production
        contextual-identifier net, not a stand-in for a surviving spaCy role;
      - `pipeline.py`: remove `use_ner`; always register Gliner2Recognizer (import stays
        deferred inside `__init__` — that is what lets tests shim `pii.gliner2_recognizer`
        in sys.modules); remove the SpacyRecognizer import and both regime branches;
        unconditional `remove_recognizer("SpacyRecognizer")` after
        `load_predefined_recognizers`; NLP_CONFIG untouched; update docstring + the
        registry-policy comment;
      - `cli.py`: drop `--no-ner` from strip and analyze; `pii_eval`: drop
        `use_ner`/`--no-ner` from score.py/`__main__.py`/README;
      - tests: move the `_NoopGliner2` stub from test_spacy_policy.py into conftest;
        `make_pipeline` grows a `stub_ner=True` default (constructed under the shim →
        fast, model-free, preserving today's fast-suite semantics; part of the cache key,
        not forwarded to PiiPipeline), `stub_ner=False` for model-marked tests; expose the
        shim as a fixture for CLI tests; replace test_spacy_policy.py with a slimmer
        registry-policy test file (SpacyRecognizer absent; Gliner2Recognizer present and
        supporting LOCATION; keep the model-marked Emily-Watson nuance test; add a
        model-marked "a teacher in Cairns" → LOCATION test); test_invalid.py's CLI test
        loses `--no-ner` and runs under the shim;
      - docs: ARCHITECTURE.md (spaCy table row → NLP-engine role only; drop the SPA node
        from the diagram; replace the "Two regimes" section — single pipeline now; rewrite
        the "spaCy-as-detector survives" bullet; supersede the two 2026-07-14 decision
        sections with a dated retirement decision carrying the eval numbers), pii/CLAUDE.md
        working agreement, README flags + timing note (keep the en_core_web_sm download —
        still required), move this item to DONE.md with the ship record, reword the layer-3
        bundled revisit (spaCy ablation → "consider dropping the GLiNER2 location pass once
        layer 3 owns contextual IDs").
      Verification: default `pytest` green and still model-free; `pytest -m "slow or
      model"`; full pii_eval generate+score on seeds 42 and 123 — expect the experiment-B
      numbers (11/11 towns, zero critical misses, org over-strips at baseline, no new
      address leaks); CLI smoke (no `--no-ner` anywhere, LOCATION placeholders appear for
      bare town names). Out of scope: the ORG-absorbs-contained-location merge rule (stays
      in the overlaps task below — the location pass reaches org-over-strip parity
      without it).

## Next up — image/PDF path

- [ ] PDFs — **treat as images**: render pages → OCR → redact pixels → reassemble PDF.
      Rationale: financial-sector PDFs often have junk/broken text layers, and rebuilding from
      pixels also eliminates the hidden-text-layer leak class entirely.
      *Decide later:* belt-and-braces variant that additionally scans any existing text layer
      to catch text the OCR misses (detection only — output still comes from pixels).
- [ ] Statement tables via the image path (the remaining half of the transaction-list task —
      CSV mode shipped 2026-07-12): tabular statements arrive as scans/PDF pages, not CSVs;
      verify the OCR path handles table layouts (row/column integrity, amounts kept intact)
      on the reference documents.
- [ ] Barcode masking: mailing barcodes on statements (Australia Post 4-state, and 1-D codes)
      encode the delivery address/customer ref — text-based detection can't see them, so
      detect and paint over barcode regions in the image pass (observed on several of the
      reference examples)
- [ ] OCR engine choice — *decide later:* Tesseract vs PaddleOCR vs Surya/docTR vs a local VLM
      (e.g. Qwen-VL class) doing OCR+PII detection in one pass. Start by benchmarking on real
      bank statements/scans. The engine seam is the parallel-lists word-box dict in
      `pii/ocr.py` (each backend is an adapter normalizing into it); the VLM is the exception
      that doesn't fit the OCR-then-analyze frame — see ARCHITECTURE.md.
- [ ] OCR preprocessing knobs: opt-in preprocessing chain for low-quality scans (bilateral
      filter / contrast stretch / adaptive threshold / rescale — see the harvested
      presidio-image-redactor chain in DONE.md). Preprocessed image feeds OCR only; painting
      stays on original pixels. Needs the eval degradation tier to measure.

## Detection pipeline

- [ ] **Layer-3 local-LLM audit pass** — "does this still contain anything identifying?"
      via llama-server; catches contextual identifiers NER can't see ("the borrower's wife,
      a dentist in Wagga Wagga"). Bundled revisits for when it lands:
      - promote PERSON_JOINT ("E & J Moore") and PERSON_REVERSED ("ROCHA RANDALL") into
        pii_eval `build.CRITICAL` (intermittent GLiNER2 misses, currently reported per-form
        without tripping the gate — see the invalid-identifiers record in DONE.md);
      - rerun the SpacyRecognizer ablation: once layer 3 owns contextual IDs, spaCy's
        LOCATION emissions can likely be dropped entirely (2026-07-14 record in DONE.md).
- [ ] Overlaps merging algorithm — define and document. Interesting areas: how the weights are
      combined (max, average, bayesian/aposteriori), what if winning classes of overlaps
      do not agree, should we merge at all in some cases. Adjacent-span coalescing for
      fragmented multi-part addresses belongs here too.
      Input (2026-07-14, image-demo wart 2): a strip-type span nested inside a
      kept-type span — GLiNER2 emits both ORGANIZATION 'WOOLWORTHS NEWTOWN' (kept) and
      ADDRESS 'NEWTOWN' (stripped), so the merchant name loses its suburb. Question:
      should a kept ORGANIZATION absorb contained ADDRESS fragments, or is that a leak
      vector (real addresses legitimately appear inside org-labeled spans)?
      Input (2026-07-14, invalid-identifiers work): invalid-class spans already rank below
      any valid type in `_merge_overlaps` (union extents, valid class wins the placeholder)
      — fold that rule into the general algorithm definition.
- [ ] Configurable strip-entity selection — let a run choose which data types to strip
      (e.g. names and addresses only). The pipeline already takes a `strip_entities` set
      internally; needs CLI exposure (`--entities` / named profiles) and documentation.
- [ ] Metadata scrubbing on all output formats

## Experiments — GLiNER2 tuning

- [ ] Per-class max_width for GLiNER2 (requested by Sergei 2026-07-14 —
      discomfort with the blanket default max_width=12). Only ADDRESS needs wide
      spans (tier-1: every other class ≤ 4 words), and w16 already showed wide-span
      FP creep, so enumerating 12-word candidate spans for *all* labels buys nothing
      for the narrow classes and may cost precision. Try per-pass widths — the
      recognizer already runs dedicated address passes, and max_width is an
      inference-time attribute that can be set before each pass (both copies:
      `model.max_width` and `model.span_rep.span_rep_layer.max_width`): address
      passes at 12, everything else back at the trained 8. Rerun tier-1 per-class
      P/R + latency. Natural companion to the labels-per-pass experiment below
      (same grid infrastructure); if per-pass mutation proves racy or awkward,
      evaluate two recognizer instances as the fallback.
- [ ] Labels-per-pass (schema partitioning). Label competition
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
- [ ] Policy for GLiNER2's numeric-ID *guesses* (2026-07-14, length-heuristic discussion).
      Diagnostic on the tier-1 corpus: nearly every short false positive is GLiNER2 labeling
      a numeric-ID type that layer-1 already owns with a checksum — `'42'` as AU_BANK_ACCOUNT,
      `'K3EN5L'` / `'TAS 2628'` as AU_TFN. A LOCATION-style char-length floor is the wrong
      instrument (TFN FPs are non-numeric junk; the real fix is format/digit-count) AND must
      NOT be applied to PERSON or ORGANIZATION — real short surnames (Wu, Ng) and bank
      acronyms (NAB, ANZ, BHP) live there, so a floor is a leak risk / pointless respectively
      (confirmed with Sergei). Cleaner single lever than N per-class floors: constrain
      GLiNER2's numeric-ID emissions — either drop those labels (layer-1 validates them) or
      route each guess through its layer-1 checksum recognizer before it may strip. The
      quick safe subset is DONE: `AU_BANK_ACCOUNT_MIN_DIGITS=5` floor on GLiNER2's account
      guesses (kills the `'42'` fragment, zero recall cost — spaced accounts survive because
      the model emits them as one span and the floor counts digits, not chars;
      tests/pii/test_gliner2_floors.py). The general per-class/validation policy for the
      other numeric IDs (TFN junk like `'K3EN5L'`, etc.) remains open. Edge to decide
      there: masked last-4 disclosures ("card ending 1234") fall under the digit floors by
      design — consistent with layer-1 (`\d{5,10}` never matched them), but the policy
      should take a deliberate stance on whether last-4 fragments are strip-worthy. Overlaps the
      invalid-identifiers and overlaps-merging work.
- [ ] Ablation: are the address workarounds still needed at max_width=12?
      Postponed (decision 2026-07-14) until the tier-1 corpus has more and more
      varied address examples — 12 ADDRESS spans from a handful of templates is
      too thin a basis for removing belt-and-braces protections. When picked up,
      fold it into the labels-per-pass experiment above (same mechanics: rerun
      the eval with the extra address passes disabled).
- [ ] LoRA adapter for Australian addresses on GLiNER2 — close the multi-part address
      fragmentation gap at the model level (GLiNER2 ships open training code and
      load_adapter(); pii_eval's generator can produce the training pairs). Revisit after
      the overlaps-merging task lands, which should already close most of the gap.
      *(2026-07-14: priority further reduced — the max_width=12 lift closed the
      one-line-address fragmentation on tier-1; LoRA now only matters if real-world
      wide spans score poorly, or for the '53 MILES SUBWAY'-style bare street-line
      recall misses.)*

## Evaluation

(The tier plan and constraints are described in [ROADMAP.md](ROADMAP.md); the completed
text tier's record is in [DONE.md](DONE.md).)

- [ ] **Tier 1 — image/degradation tier**: extend the synthetic generator with rendered
      documents and a degradation pipeline (DPI, skew, blur, JPEG artifacts) for OCR
      benchmarking; bbox-level ground truth. Match painted boxes with pixel tolerance from
      day one — exact-box assertions break across Tesseract versions (see the
      presidio-image-redactor review, DONE.md item (i)).
- [ ] **Tier 2 — PII-transplanted real documents**: Sergey manually replaces real PII with fake
      in 4–6 real documents (one per major bank layout, one bad scan, one transactions CSV),
      keeping layout intact. Real layouts + known ground truth + declassified. One-time effort,
      reusable forever.
- [ ] **Tier 3 — metrics-only runs on the real corpus**: harness emits only aggregates (entity
      counts/type, confidence histograms, layer-disagreement rates, cross-OCR-engine
      disagreement). Local side-by-side review UI so manual acceptance checks are a quick
      click-through; only declassified findings are reported back.

## Nice-to-have

- [ ] "Match original font" for painted placeholders (Sergei, 2026-07-14) —
      estimate font size/weight (and maybe family) from the covered words' boxes/pixels so
      placeholders blend into the document instead of the current fixed-Arial
      shrink-to-fit. Also worth considering: match fill to the local background around the
      box rather than the page-wide most-common border color.

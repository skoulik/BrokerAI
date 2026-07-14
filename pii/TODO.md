# TODO — PII stripping tool

All open Phase 1 tasks, with full working detail. The activity overview and evaluation-tier
plan are in [ROADMAP.md](ROADMAP.md); completed tasks and their engineering records are in
[DONE.md](DONE.md); design decisions (the *why*) in [ARCHITECTURE.md](ARCHITECTURE.md).

Grouped by theme. Suggested order on the image/PDF track (2026-07-14): PDF mode → demo on
the reference documents → pii_eval image tier → OCR bake-off.

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
- [ ] Ship the GLiNER2 location label (experiment DONE 2026-07-14, record in DONE.md;
      `Gliner2Recognizer(location=True)` exists, default-off). The head-to-head is settled —
      GLiNER2's location pass strictly dominates spaCy LOCATION on tier-1 (11/11 vs 6/11
      contextual-ID towns, zero extra org over-strip, one fewer address leak). Remaining
      work to flip defaults: turn the flag on in pii/pipeline.py, drop SpacyRecognizer's
      detector role (→ pure NLP-engine dependency), update test_spacy_policy.py + the
      ARCHITECTURE/CLAUDE.md decision notes, rerun the full pii_eval gate. Best landed
      together with the ORG-absorbs-contained-location merge rule (overlaps task above) so a
      merchant's suburb isn't split off a kept ORGANIZATION. Decision to flip is Sergei's.
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
      other numeric IDs (TFN junk like `'K3EN5L'`, etc.) remains open. Overlaps the
      invalid-identifiers and overlaps-merging work.
- [ ] Layer-1 gap: space-grouped bank accounts leak (found 2026-07-14). `a/c 1234 5678`
      (4+4) is detected by nobody — AuAccountNumberRecognizer's `\d{5,10}` needs a
      contiguous run so each 4-digit half falls short, no pattern spans the internal space,
      and GLiNER2's recall on the form is inconsistent (it catches `0007 3111 4` but missed
      `1234 5678`). `a/c 12345678` contiguous is fine. Add a labeled/space-tolerant account
      pattern (e.g. `a/c` or `account` followed by digit groups) guarded against eating
      transaction amounts and date ranges.
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

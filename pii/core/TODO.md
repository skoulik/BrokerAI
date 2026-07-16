# TODO — PII engine (core)

All open engine tasks, with full working detail. The activity overview and evaluation-tier
plan are in [ROADMAP.md](ROADMAP.md); completed tasks and their engineering records are in
[DONE.md](DONE.md); design decisions (the *why*) in [ARCHITECTURE.md](ARCHITECTURE.md).
Front-end tasks live with their component: [../cli/TODO.md](../cli/TODO.md),
[../gui/TODO.md](../gui/TODO.md).

Grouped by theme. Suggested order on the image/PDF track (2026-07-14, amended 2026-07-15):
PDF mode → demo on the reference documents → pii_eval image tier → Tesseract stack review
(docs + pytesseract source) → OCR bake-off.

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
- [ ] Tesseract docs/config review (time-boxed; prep for the preprocessing-knobs and
      bake-off tasks). Scope decision 2026-07-15: harvest operational knowledge from the
      official docs, NOT a source review — the ~150k-line C++ codebase is OCR internals we
      consume, not code we write, so the source-review drill that paid off on
      presidio-image-redactor/spaCy doesn't transfer. Targets: the ImproveQuality guide
      (Tesseract binarizes internally with Otsu — establishes which external preprocessing
      helps vs duplicates work, feeds the preprocessing-knobs task); PSM page-segmentation
      modes for statement layouts (sparse text vs uniform block vs single column — feeds
      the statement-tables task); OEM engine choice (LSTM vs legacy); semantics of the
      word-level `conf` values in the TSV output (needed before ever thresholding on
      them); useful config variables (`preserve_interword_spaces`, char whitelists,
      `--dpi`). Output: DONE.md record; distilled defaults into `pii/core/ocr.py` comments and
      ARCHITECTURE.md.
- [ ] pytesseract source review — companion to the docs review above, same
      harvest-not-adopt drill as the presidio-image-redactor and spaCy reviews. It fits
      the profile that made those pay: a single-file Python wrapper (pytesseract.py,
      521 lines at installed 0.3.13) that `pii/core/ocr.py` sits directly on. Targets: how
      `image_to_data` parses the TSV into the parallel-lists dict (type coercion of
      `conf` — int vs float across versions; field handling when recognized text
      contains tab/newline characters); the subprocess round-trip (PIL image → temp file
      — does DPI metadata survive, which formats are used); config-string
      quoting/escaping of user-supplied options; error and timeout paths. Output:
      DONE.md record; any warts become defensive handling at our seam in `pii/core/ocr.py`.
- [ ] OCR engine choice — *decide later:* Tesseract (current) vs the two candidate
      evaluations below vs a local VLM (e.g. Qwen-VL class) doing OCR+PII detection in one
      pass. Decide on benchmark numbers from real bank statements/scans (needs the image
      eval tier for ground truth). The engine seam is the parallel-lists word-box dict in
      `pii/core/ocr.py` (each backend is an adapter normalizing into it); the VLM is the
      exception that doesn't fit the OCR-then-analyze frame — see ARCHITECTURE.md.
- [ ] Evaluate PaddleOCR: write its `pii/core/ocr.py` adapter (detection polygons → axis-aligned
      word boxes) and benchmark against the Tesseract baseline on the same corpus —
      word-level accuracy on clean + degraded scans, table/statement layouts (row/column
      integrity), runtime, and install weight (pulls the paddlepaddle runtime).
- [ ] Evaluate Surya and docTR (same adapter shape, one bake-off pass): benchmark against
      the Tesseract baseline as above; both are GPU-first, which fits our setup. Check
      license fit before adopting (docTR is Apache-2.0; Surya's model weights carry a
      revenue-conditional commercial-use clause — verify current terms).
- [ ] OCR preprocessing knobs: opt-in preprocessing chain for low-quality scans (bilateral
      filter / contrast stretch / adaptive threshold / rescale — see the harvested
      presidio-image-redactor chain in DONE.md). Preprocessed image feeds OCR only; painting
      stays on original pixels. Needs the eval degradation tier to measure.

## Detection pipeline

- [ ] **Reversed-caps person-name residual** ('REID THOMAS' / 'BROOKS ETHAN') — what
      remains after the 2026-07-15 fixes (full history in DONE.md: JointNameRecognizer
      → interference diagnosis → per-cell NER windows + PERSON coalescing + name-forms
      statistics doc). Current numbers on the fixed-n name-forms corpus: PERSON_REVERSED
      **70/72 across seeds 42+123** (was 20–75% noise on n=5); PERSON_COMMA 32/32,
      PERSON_PARTICLE 20/20, PERSON_MULTIWORD 20/20. The two residual leaks are pure
      **label competition on isolated caps junk lines**: person-only pass finds both
      name words ('REID'@0.86 + 'THOMAS'@0.85), but in the production schema
      ORGANIZATION claims the line ('REID THOMAS RENT'@0.86 org) and person collapses
      to 0.06–0.31 — windowing cannot help. Candidates: (1) labels-per-pass isolation
      (the experiment below owns exactly this; person-only rescues both observed
      leaks), (2) the person-names database layer below (deterministic recall floor),
      (3) LoRA fine-tune on statement-style forms. The known-person permutation pass
      idea is retired as primary (the interference it targeted is fixed at the window
      level) but remains viable belt-and-braces. When the residual closes, promote
      PERSON_REVERSED into pii_eval `build.CRITICAL` (PERSON_JOINT was promoted with
      the joint-form fix).
- [ ] **Person-names database layer** (Sergei, 2026-07-15) — if reversed/varied-name
      recall stays unsatisfactory, integrate a names database as a deterministic
      recall floor: match known given names/surnames (e.g. the `names-dataset`
      package, US SSA + AU census name lists) as tokens and emit PERSON candidates
      for adjacent known-name pairs regardless of word order — 'REID THOMAS' hits
      (Thomas = known given name, Reid = known surname) with no NER involved. Design
      questions when picked up: score/context policy (confident vs context-promoted),
      precision on merchant lines (MCDONALDS, HARVEY NORMAN are surname-shaped —
      probably require a known *given* name in the pair, not just surnames), and the
      overlap policy vs keep-ORGANIZATION spans. Sibling of the AU place-name
      gazetteer task (same trie/set-matching machinery, same fuzzy-budget idea).
- [ ] **Layer-3 local-LLM audit pass** — *contingent, not committed (expectation set
      2026-07-15): the plan is to evaluate the tool end-to-end with layers 1+2 only, and
      build layer 3 only if those results prove unsatisfactory — see ROADMAP.md and
      ARCHITECTURE.md.* Design if built: "does this still contain anything identifying?"
      via llama-server; catches contextual identifiers NER can't see ("the borrower's wife,
      a dentist in Wagga Wagga"). Bundled revisit for when it lands: consider dropping the
      GLiNER2 location pass once layer 3 owns contextual IDs (the SpacyRecognizer it
      replaced was retired 2026-07-15 — records in DONE.md).
- [ ] Overlaps merging algorithm — define and document. Interesting areas: how the weights are
      combined (max, average, bayesian/aposteriori), what if winning classes of overlaps
      do not agree, should we merge at all in some cases. Adjacent-span coalescing for
      fragmented multi-part addresses belongs here too.
      Input (2026-07-14, image-demo wart 2): a strip-type span nested inside a
      kept-type span — GLiNER2 emits both ORGANIZATION 'WOOLWORTHS NEWTOWN' (kept) and
      ADDRESS 'NEWTOWN' (stripped), so the merchant name loses its suburb. Question:
      should a kept ORGANIZATION absorb contained ADDRESS fragments, or is that a leak
      vector (real addresses legitimately appear inside org-labeled spans)?
      *(2026-07-15: the tier-1 corpus now generates suburb-suffixed merchants as
      whole keep-ORGANIZATION spans, so this wart is measured on the over-strip
      axis — a fix here shows up as the ORGANIZATION over-stripped count dropping.)*
      Input (2026-07-14, invalid-identifiers work): invalid-class spans already rank below
      any valid type in `_merge_overlaps` (union extents, valid class wins the placeholder)
      — fold that rule into the general algorithm definition.
- [ ] Metadata scrubbing on all output formats *(eval note 2026-07-15: no pii_eval
      corpus format carries metadata — extend a tier alongside this task)*
- [ ] Slim the Presidio NLP engine: exclude `parser` and `ner` from the en_core_web_sm
      pipeline. Presidio loads the model with bare `spacy.load()` (spacy_nlp_engine.py, no
      component exclusions), so every analyzed text pays for the full 6-component pipeline;
      with SpacyRecognizer retired the spaCy NER output is read by nobody, and the parser
      only produces sentence bounds nothing consumes — lemmas need tagger+attribute_ruler
      only. Needs a small SpacyNlpEngine subclass or preloaded-nlp injection; first verify
      no recognizer/enhancer touches `nlp_artifacts.entities`/sents, then measure layer-1+2
      latency on the eval corpus. (spaCy source review finding (m), 2026-07-15 — record in
      [DONE.md](DONE.md).)
- [ ] AU place-name gazetteer as a cheap deterministic LOCATION layer (spaCy source review
      finding (j)): FlashText/PhraseMatcher-style trie — or plain set matching at our char
      level — over a public AU suburb/town list, case-insensitive, whitespace-normalized.
      Gives recall on bare town names independent of GLiNER2's location pass (and of the
      layer-3 audit when it lands); decide its overlap policy vs the location label when
      the overlaps-merging task above is done. Consider a fuzzy edit budget of
      `max(2, 0.3·len)` for OCR damage (review finding (i)).
      Also the recovery path for the `LOCATION_MIN_CHARS=4` trade-off: the floor on the
      GLiNER2 location pass knowingly sacrifices genuine 3-letter suburbs (Kew, Ayr) to
      kill the short-acronym FP class ('AU', 'NSW', 'NAB') — recorded 2026-07-14 in
      gliner2_recognizer.py/ARCHITECTURE.md. Gazetteer matches are exact lookups, so no
      length floor is needed and the 3-letter suburbs come back for free.
      *(2026-07-15: the tier-1 corpus now carries bare-town `LOCATION` and
      3-letter-suburb `LOCATION_SHORT` truth rows, so this task has a metric. First
      numbers, seed 42: both 100% — but the short suburbs are being rescued by the
      GLiNER2 ADDRESS pass on sentence context ("resided in Kew"), at barely-above-
      threshold scores (Kew scored 0.433 vs threshold 0.4), not by the floored
      location pass. Fragile; bare/contextless short suburbs are still the exposed
      case and the corpus doesn't generate those yet — add a no-context surface form
      when picking this up.)*

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
      suppresses sibling scores (documented in pii/core/gliner2_recognizer.py — the same
      span scores 1.0 alone vs 0.49 among siblings); addresses already get dedicated
      passes. New direct evidence (2026-07-15, reversed-caps diagnosis above): on CSV
      column blobs a person-only pass emits 'FULLER CHRISTOPHER'@0.80 where the
      production schema emits 0.33 — isolation rescues real misses, not just points.
      Question: does everything benefit from isolation? Grid to evaluate on
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
      tests/pii/core/test_gliner2_floors.py). The general per-class/validation policy for the
      other numeric IDs (TFN junk like `'K3EN5L'`, etc.) remains open. Edge to decide
      there: masked last-4 disclosures ("card ending 1234") fall under the digit floors by
      design — consistent with layer-1 (`\d{5,10}` never matched them), but the policy
      should take a deliberate stance on whether last-4 fragments are strip-worthy. Overlaps the
      invalid-identifiers and overlaps-merging work. *(Corpus note 2026-07-15: pii_eval
      generates no masked last-4 forms yet — add them, with a truth convention for
      whichever stance is chosen, when this policy lands.)*
- [ ] Ablation: are the address workarounds still needed at max_width=12?
      Postponed (decision 2026-07-14) until the tier-1 corpus has more and more
      varied address examples — 12 ADDRESS spans from a handful of templates is
      too thin a basis for removing belt-and-braces protections. *(2026-07-15:
      variety widened — PO Box postal lines, `ADDRESS_BARE` bare street lines in
      transaction descriptions; seed 42 now has 18 ADDRESS + 12 ADDRESS_BARE
      spans. Better, but still template-thin; judge again when picked up.)* When picked up,
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

- [ ] **Pseudonym-consistency scoring** (gap found 2026-07-15): the persona pool was
      built so the same people/accounts recur across a corpus, but the scorer creates a
      fresh `PseudonymMap` per document (`pii_eval/score.py`) and asserts nothing about
      placeholder identity — cross-document consistency is prepared for, never checked.
      Task: decide the intended semantics (one shared map per document *set* is the
      product story — pseudonyms consistent across a submission bundle), run the scorer
      with a shared map, and add an axis asserting same canonical value ⇒ same
      placeholder across documents (the truth manifest already carries the values).
- [ ] **Tier 1 — image/degradation tier**: iteration 1 SHIPPED 2026-07-16 (see DONE.md) —
      `pii_eval render` prints the text corpus to page images (Pillow, seeded font
      variety, monospace for fixed-column docs) and `score --modality image` scores the
      real image pipeline by re-OCR value survival with OCR-tolerant matching; paired
      text/image corpora share one truth.json, output at `pii_eval/corpora/image/s<seed>`.
      Remaining: degradation pipeline (DPI, skew, blur, JPEG artifacts) composing on the
      clean renders; realistic reportlab statement templates (mail barcodes) as a second
      layout source; a `partial` axis for the image scorer (token-level survival needs
      occurrence disambiguation — surname stems recur in kept business names, see the
      known-limitation note in pii_eval/README.md); bbox-level ground truth if
      box-placement assertions are ever needed —
      match painted boxes with pixel tolerance from day one, exact-box assertions break
      across Tesseract versions (see the presidio-image-redactor review, DONE.md item (i)).
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

# OCR bake-off round 2 — Surya 2 (line mode via llama.cpp) vs PP-OCRv6_medium

**Date:** 2026-07-17 · **Verdict: Surya 2 retired same-day** (Sergei's decision on these
findings; adapter preserved in git history at the commit introducing this report, then
removed). PaddleOCR v6_medium remains the engine. Round-1 report:
[2026-07-17-ocr-fidelity-tesseract-vs-paddleocr.md](2026-07-17-ocr-fidelity-tesseract-vs-paddleocr.md).

## Setup

- **surya-ocr 0.21.2** ("Surya 2", the post-0.20 architecture): 650M VLM (`surya-ocr-2`,
  Qwen-VL derived) served out-of-process — llama-server b9968 Vulkan, F16 GGUF (1.27 GB +
  205 MB mmproj, no quantization loss) — plus the in-process torch line-detection model.
  v1's native word/char boxes no longer exist; v1 (≤0.17.1) is unmaintained and pins
  transformers 4.x, so it was never a candidate (Sergei: target v2 only).
- **Adapter** (`pii/core/ocr_surya.py`, in git history): detection line polygons →
  whitespace-gap splitting of wide rows into cell-like segments → per-segment block-mode
  VLM OCR (their intended "merge with text-line detection" path) → HTML flatten →
  proportional line→word interpolation → `assemble`. Greedy decode (temperature 0).
- **Instrument:** `pii_eval score --modality image` leak gate, seed 42, paired
  text/image corpus, pillow 10.4 renders (paddle baseline re-taken under the same env:
  s42 + s123 both PASS).
- Hardware: RTX 2080 Ti (`--device Vulkan0`). The RX 9070 XT ran the same GGUF at 12
  tok/s vs the 2080 Ti's 260 (RDNA4 Vulkan immaturity) and was excluded.

## Headline numbers

| axis | paddle:v6_medium | Surya 2 line mode |
|---|---|---|
| s42 critical-leak gate | **PASS (0)** | **6 → 3 → 5 leaks across three runs** |
| s42 corpus wall time | ~2 min | ~7 min; **>10 min** with the vision-token floor |
| determinism | reproducible | **non-reproducible run to run** (temp 0) |
| per-line latency (warm, clean crop) | n/a (page-at-once) | ~0.5 s (no floor) |

## Failure classes found (each traced to raw model output, not adapter artifacts)

1. **Vision-token starvation → fabrication.** With llama-server defaults, a narrow
   account-number crop (`: 2880-95701`) came back as *"Lady Marian Perochenko (…) is a
   specialist in the sci…"* — pure invention; other segments looped ("ACTIVITY STMT STMT
   ACTIVITY…") or degenerated into token salad ("B6, B7, B8, …"). Cause: small line crops
   get too few image tokens; llama.cpp's own load warning says Qwen-VL wants ≥1024.
   `--image-min-tokens 1024` eliminated fabrication/loops entirely — and multiplied
   vision-prefill cost ~10×, taking the corpus run past 10 minutes. **Lesson (transfers
   to every future VLM pipeline): vision-token budget is a correctness knob, not a
   throughput knob; floor it and pay.**
2. **Table-izing of statement rows.** Full-width monospace row crops decode as tables:
   literal ` | ` separators between words, phantom `<th>` header cells, duplicated cells
   ("MOORE MOORE"). Broke NER continuity (PERSON leaks: the words are there, the
   pattern "ERIC | MOORE" is not a name) and corrupted interpolation geometry.
   Mitigated (not fully eliminated) by whitespace-gap segment splitting + pipe
   stripping; narrow segments mostly decode as clean `<p>`.
3. **Cross-script digit homoglyphs.** On a CLEAN 32 px render the model emitted U+06F5
   (Extended Arabic-Indic five, ۵) for an ASCII '5' — visually identical, breaks value
   matching and checksums by string identity while *looking* correct. A confusion class
   classic OCR engines cannot produce (their charsets are Latin). Adapter folds all
   Unicode Nd digits to ASCII; letter homoglyphs (Cyrillic А…) were the recorded watch
   item.
4. **Residual digit damage in dense rows** (with the vision floor): space-shattered
   amounts ("5, 7 06. 90" for 5,706.90), digit substitutions ("2880"→"2800",
   "730.20"→"73.0.20"), U+FFFD replacement chars, occasional whole-value omission
   (an account number absent from output entirely — the silent-omission class predicted
   in the review). These caused the remaining AU_TFN / AU_BANK_ACCOUNT / PERSON leaks.
5. **Non-reproducibility at temperature 0.** Three identical gate runs produced 6, 3,
   and 5 critical leaks with barely-overlapping leak sets (e.g. 'JEFFREY LAWRENCE'
   present in one run's OCR, absent in the next). llama.cpp parallel batching makes
   greedy decode non-deterministic on borderline tokens, and dense rows sit exactly on
   that borderline. **For a redaction gate this is disqualifying on its own: a gate you
   can pass by re-rolling is not a gate.** (Single-slot serving would likely restore
   determinism at a further large throughput cost — untried, see below.)

## Levers deliberately left untried (recorded for any future revisit)

CUDA llama.cpp build (Vulkan-on-NVIDIA ran ~30% GPU duty cycle, latency-bound);
`--image-max-tokens`; single-slot determinism; vllm under WSL2 (bf16 needs Ampere+ —
would need `VLLM_DTYPE=float16` on the 2080 Ti); full-page mode parsing the nested
`data-bbox` attributes the stock parser strips (finer-than-block geometry exists in raw
output); the formal `ocr-report` fidelity sweep (hours at observed speed — not worth it
once the gate variance was seen). A future Surya major release or a CUDA-grade serving
path could reopen the question; the adapter in git history restores in one revert.

## Operational knowledge worth keeping (transfers to the one-pass VLM TODO item)

- surya reads env at import time; caches re-pointed per repo convention
  (`HF_HUB_CACHE`→models/hf-cache, `MODEL_CACHE_DIR`→models/surya).
- Windows: force `SURYA_INFERENCE_BACKEND=llamacpp` (autodetect picks vllm on any
  NVIDIA box); auto-spawn works but surya's own cleanup dies with WinError 5 → orphaned
  server holding VRAM; kill via the PID in `~/.cache/datalab/surya/<backend>_server.json`.
- `SURYA_INFERENCE_URL` attaches to an external llama-server (the Mac/remote path);
  `SURYA_INFERENCE_KEEP_ALIVE=1` for back-to-back runs.
- Model license: weights are OpenRAIL-M-modified ($5M revenue/funding caps, non-compete,
  share-alike-on-output oddity); code Apache-2.0. Accepted for evaluation use.
- Env legacy of this round (kept deliberately): transformers 5.14.1 (GLiNER2 re-gated
  green), pillow 10.4 (corpora re-rendered, paddle baseline re-taken; the <11 pin died
  with surya, so a future pillow upgrade is unblocked but requires re-render+re-baseline).

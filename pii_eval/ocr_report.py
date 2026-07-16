"""OCR-fidelity sweep — glyph size × font face, the first factors of the
OCR degradation investigation (design agreed 2026-07-16; task record in
pii/core/TODO.md). Findings are Tesseract-scoped; the harness is
engine-neutral: pages render through pii_eval.render and OCR runs through
the pii.core.ocr seam, so every future bake-off backend reruns this sweep
unchanged.

This measures OCR fidelity directly, not PII leaks: the drawn text of
every page is known exactly, so OCR output is aligned against it and each
divergence is bucketed — substitution classes (digit/letter/case), word
merges and splits (space loss is the dominant leak driver per the s42
root-causing: shape breaks, not digit misreads), lost and spurious lines.
The analysis axis is the *measured x-height in pixels* per (font, size) —
tessdoc: <10 px poor, <8 px destroyed, ~30 px LSTM ceiling — because equal
em sizes land on very different x-heights across faces.

OCR words are re-bucketed into *visual* lines by box geometry before
alignment: Tesseract fragments wide-gutter tables into separate blocks,
reordering the assembled text (the stranded-label failure class), which
would read as mass line loss to an order-preserving aligner. Re-lining by
geometry keeps the recognition-fidelity measurement clean, while
`resegmented_lines` counts visual lines built from >1 Tesseract line —
the structural damage, reported separately.

Per-word confidences are recorded against word correctness (histograms
for correct vs erroneous words): the measured data that the "never
threshold on conf" ban (ARCHITECTURE.md Tesseract profile) waits on.

Cells append to a JSONL as they finish; a rerun skips completed cells, so
an interrupted sweep resumes instead of restarting.
"""

import json
import sys
import time
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path

from PIL import ImageFont

from pii.core.ocr import OcrResult, OcrWord, get_ocr
from pii_eval.render import (
    MONO_FONTS,
    PROPORTIONAL_FONTS,
    _is_fixed_column,
    format_csv_table,
    render_page,
)

ALL_FONTS = MONO_FONTS + PROPORTIONAL_FONTS
# Mostly below render.py's 20 px floor (which sits ON the documented
# x-height cliff), extended past 32 toward the realistic 300-dpi scan
# regime (10 pt @ 300 dpi ≈ 42 px em).
SIZES = [10, 12, 14, 16, 18, 20, 24, 28, 32, 40, 48]

_GAP = 0.45  # line-alignment gap cost: two gaps (0.9) beat pairing
# dissimilar lines (cost 1 − similarity), so junk lines don't pair up.


def measure_xheight(font_name: str, size: int) -> int:
    bbox = ImageFont.truetype(font_name, size).getbbox("x")
    return bbox[3] - bbox[1]


def _norm_lines(text: str) -> list[str]:
    """Whitespace-collapsed non-empty lines — gutter widths are layout,
    not glyphs, so runs of spaces must not count as errors; a *lost*
    boundary still registers (words differ)."""
    lines = (" ".join(line.split()) for line in text.splitlines())
    return [line for line in lines if line]


def _squash(s: str) -> str:
    return "".join(c for c in s.casefold() if c.isalnum())


def visual_lines(result: OcrResult) -> list[list[OcrWord]]:
    """Re-bucket OCR words into visual lines by box geometry.

    Words sorted by vertical center, grouped while the center stays
    within half a word-height of the group's running mean (page renders
    put ≥1.35 line-heights between baselines, so real lines are far
    apart), then each group sorted left-to-right.
    """
    words = sorted(result.words, key=lambda w: w.box.top + w.box.height / 2)
    lines: list[list[OcrWord]] = []
    centers: list[float] = []
    heights: list[float] = []
    for w in words:
        c = w.box.top + w.box.height / 2
        # Tolerance uses the taller of word and group height: punctuation
        # runs ("...") have tiny boxes and must still join their line.
        if lines and abs(c - centers[-1]) < 0.5 * max(
            w.box.height, heights[-1], 1
        ):
            group = lines[-1]
            group.append(w)
            centers[-1] += (c - centers[-1]) / len(group)
            heights[-1] = max(heights[-1], w.box.height)
        else:
            lines.append([w])
            centers.append(c)
            heights.append(float(w.box.height))
    for group in lines:
        group.sort(key=lambda w: w.box.left)
    return lines


def _line_sim(a: str, b: str) -> float:
    sm = SequenceMatcher(None, a, b, autojunk=False)
    if sm.quick_ratio() < 0.2:
        return 0.0
    return sm.ratio()


def align_lines(truth: list[str], ocr: list[str]):
    """Order-preserving line alignment.

    Returns (pairs, lost, spurious): index pairs of matched lines,
    truth-line indices with no OCR counterpart, OCR-line indices with no
    truth counterpart.
    """
    n, m = len(truth), len(ocr)
    sim = [[_line_sim(truth[i], ocr[j]) for j in range(m)] for i in range(n)]
    dist = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dist[i][0] = i * _GAP
    for j in range(1, m + 1):
        dist[0][j] = j * _GAP
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            dist[i][j] = min(
                dist[i - 1][j - 1] + 1 - sim[i - 1][j - 1],
                dist[i - 1][j] + _GAP,
                dist[i][j - 1] + _GAP,
            )
    pairs, lost, spurious = [], [], []
    i, j = n, m
    while i or j:
        if (
            i and j
            and abs(dist[i][j] - (dist[i - 1][j - 1] + 1 - sim[i - 1][j - 1]))
            < 1e-9
        ):
            pairs.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i and abs(dist[i][j] - (dist[i - 1][j] + _GAP)) < 1e-9:
            lost.append(i - 1)
            i -= 1
        else:
            spurious.append(j - 1)
            j -= 1
    pairs.reverse()
    lost.reverse()
    spurious.reverse()
    return pairs, lost, spurious


def _edit_ops(a, b) -> list[tuple]:
    """Minimal edit script between two sequences: [(kind, i, j)] with
    kind in sub/del/ins; equal positions are omitted. Backtrace prefers
    the diagonal, so 1:1 substitutions never decompose into del+ins."""
    n, m = len(a), len(b)
    dist = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dist[i][0] = i
    for j in range(1, m + 1):
        dist[0][j] = j
    for i in range(1, n + 1):
        ai = a[i - 1]
        row, prev = dist[i], dist[i - 1]
        for j in range(1, m + 1):
            row[j] = min(
                prev[j - 1] + (ai != b[j - 1]),
                prev[j] + 1,
                row[j - 1] + 1,
            )
    ops = []
    i, j = n, m
    while i or j:
        if i and j and dist[i][j] == dist[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            if a[i - 1] != b[j - 1]:
                ops.append(("sub", i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i and dist[i][j] == dist[i - 1][j] + 1:
            ops.append(("del", i - 1, j))
            i -= 1
        else:
            ops.append(("ins", i, j - 1))
            j -= 1
    ops.reverse()
    return ops


def _bucket(kind: str, ca: str, cb: str) -> str:
    """Error taxonomy. Space loss/gain is a word merge/split — the class
    that breaks pattern-recognizer shapes — whatever the edit kind."""
    if kind == "del":
        if ca == " ":
            return "merge"
        return ("del_digit" if ca.isdigit()
                else "del_alpha" if ca.isalpha() else "del_other")
    if kind == "ins":
        if cb == " ":
            return "split"
        return ("ins_digit" if cb.isdigit()
                else "ins_alpha" if cb.isalpha() else "ins_other")
    if ca == " ":
        return "merge"
    if cb == " ":
        return "split"
    if ca.isdigit() and cb.isdigit():
        return "sub_digit_digit"
    if ca.isdigit() or cb.isdigit():
        return "sub_digit_alpha" if (ca.isalpha() or cb.isalpha()) else "sub_other"
    if ca.isalpha() and cb.isalpha():
        return "sub_case" if ca.casefold() == cb.casefold() else "sub_alpha"
    return "sub_other"


def _conf_bin(conf: float) -> int:
    return min(max(int(conf) // 10, 0), 9)


def score_page(truth_text: str, ocr: OcrResult) -> dict:
    """Align OCR output against the exact drawn text; return the cell's
    fidelity stats. CER can exceed 1.0 at garbage sizes (spurious output
    counts as insertions against the truth-char denominator)."""
    tlines = _norm_lines(truth_text)
    vlines = visual_lines(ocr)
    olines = [" ".join(w.text for w in line) for line in vlines]
    pairs, lost, spurious = align_lines(tlines, olines)

    buckets: Counter = Counter()
    confusion: Counter = Counter()
    conf_hist = {"correct": [0] * 10, "error": [0] * 10}
    conf_sums = {"correct": [0.0, 0], "error": [0.0, 0]}
    char_errors = 0
    word_errors = 0

    def _tally(word: OcrWord, ok: bool) -> None:
        key = "correct" if ok else "error"
        conf_hist[key][_conf_bin(word.conf)] += 1
        conf_sums[key][0] += word.conf
        conf_sums[key][1] += 1

    for i, j in pairs:
        ops = [
            (kind, tlines[i][ti] if kind != "ins" else "",
             olines[j][oj] if kind != "del" else "")
            for kind, ti, oj in _edit_ops(tlines[i], olines[j])
        ]
        char_errors += len(ops)
        for kind, ca, cb in ops:
            buckets[_bucket(kind, ca, cb)] += 1
            if kind == "sub" and ca != " " and cb != " ":
                confusion[f"{ca}->{cb}"] += 1
        twords = tlines[i].split()
        owords = [w.text for w in vlines[j]]
        flags = [True] * len(owords)
        for kind, _, oj in _edit_ops(twords, owords):
            word_errors += 1
            if kind != "del":
                flags[oj] = False
        for word, ok in zip(vlines[j], flags):
            _tally(word, ok)

    squashed_all = _squash("\n".join(olines))
    lines_merged = 0
    for i in lost:
        char_errors += len(tlines[i])
        word_errors += len(tlines[i].split())
        buckets["lost_chars"] += len(tlines[i])
        sq = _squash(tlines[i])
        if len(sq) >= 4 and sq in squashed_all:
            lines_merged += 1
    for j in spurious:
        char_errors += len(olines[j])
        word_errors += len(vlines[j])
        buckets["spurious_chars"] += len(olines[j])
        for word in vlines[j]:
            _tally(word, False)

    truth_chars = sum(len(line) for line in tlines)
    truth_words = sum(len(line.split()) for line in tlines)
    return {
        "truth_chars": truth_chars,
        "truth_words": truth_words,
        "truth_lines": len(tlines),
        "visual_lines": len(vlines),
        "tsv_lines": len({w.line for w in ocr.words}),
        "resegmented_lines": sum(
            1 for line in vlines if len({w.line for w in line}) > 1
        ),
        "lines_lost": len(lost),
        "lines_merged_elsewhere": lines_merged,
        "lines_spurious": len(spurious),
        "char_errors": char_errors,
        "word_errors": word_errors,
        "cer": char_errors / truth_chars if truth_chars else 0.0,
        "wer": word_errors / truth_words if truth_words else 0.0,
        "buckets": dict(buckets),
        "confusion": dict(confusion),
        "conf_correct": conf_hist["correct"],
        "conf_error": conf_hist["error"],
        "conf_correct_sum": conf_sums["correct"][0],
        "conf_correct_n": conf_sums["correct"][1],
        "conf_error_sum": conf_sums["error"][0],
        "conf_error_n": conf_sums["error"][1],
    }


def default_out(backend: str) -> str:
    """One report file per backend; the Tesseract name predates the
    backend seam and stays unsuffixed. ':' (paddle tier separator) is
    not filename-safe on Windows."""
    suffix = "" if backend == "tesseract" else f"_{backend}".replace(":", "-")
    return f"pii_eval/reports/ocr_fidelity{suffix}.jsonl"


def run(
    seeds=None,
    out: str | None = None,
    fonts=None,
    sizes=None,
    keep_images: bool = False,
    ocr_backend: str = "tesseract",
) -> Path:
    seeds = seeds or [42, 7, 123]
    fonts = fonts or ALL_FONTS
    sizes = sizes or SIZES
    ocr = get_ocr(ocr_backend)
    out_path = Path(out or default_out(ocr_backend))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    done = set()
    if out_path.exists():
        for line in out_path.read_text("utf-8").splitlines():
            r = json.loads(line)
            done.add((r.get("backend", "tesseract"), r["seed"], r["doc"],
                      r["font"], r["size"]))
        if done:
            print(f"resuming: {len(done)} cells already in {out_path}",
                  file=sys.stderr)

    with out_path.open("a", encoding="utf-8") as fh:
        for seed in seeds:
            corpus = Path(f"pii_eval/corpora/text/s{seed}")
            truth = json.loads((corpus / "truth.json").read_text("utf-8"))
            for doc in truth["docs"]:
                text = (corpus / doc["file"]).read_text("utf-8")
                fixed = _is_fixed_column(doc["file"])
                if doc["file"].endswith(".csv"):
                    text = format_csv_table(text)
                # Fixed-column docs stay monospace (their layout IS the
                # whitespace; a proportional face would conflate layout
                # destruction with glyph fidelity) — font comparisons are
                # valid within a doc class only.
                doc_fonts = [
                    f for f in fonts if not fixed or f in MONO_FONTS
                ]
                for font_name in doc_fonts:
                    for size in sizes:
                        key = (ocr_backend, seed, doc["file"], font_name,
                               size)
                        if key in done:
                            continue
                        t0 = time.time()
                        page = render_page(text, font_name, size)
                        stats = score_page(text, ocr(page))
                        row = {
                            "backend": ocr_backend,
                            "seed": seed,
                            "doc": doc["file"],
                            "doc_class": "fixed" if fixed else "prose",
                            "font": font_name,
                            "size": size,
                            "xheight": measure_xheight(font_name, size),
                            **stats,
                            "secs": round(time.time() - t0, 2),
                        }
                        if keep_images:
                            pages_dir = out_path.parent / "pages"
                            pages_dir.mkdir(exist_ok=True)
                            page.save(
                                pages_dir / f"s{seed}-{Path(doc['file']).stem}"
                                            f"-{Path(font_name).stem}-{size}.png"
                            )
                        fh.write(json.dumps(row) + "\n")
                        fh.flush()
                        print(
                            f"  s{seed} {doc['file']} {font_name}@{size} "
                            f"xh={row['xheight']} cer={row['cer']:.3f} "
                            f"({row['secs']}s)",
                            file=sys.stderr,
                        )
    summarize(out_path)
    return out_path


def summarize(path) -> None:
    """Compact matrices from the JSONL; deeper cuts are ad-hoc analysis.
    A report file normally holds one backend (default_out); if several
    are mixed via -o, each gets its own section."""
    all_rows = [
        json.loads(line)
        for line in Path(path).read_text("utf-8").splitlines()
    ]
    if not all_rows:
        print("no cells in report")
        return
    for backend in sorted({r.get("backend", "tesseract") for r in all_rows}):
        rows = [r for r in all_rows
                if r.get("backend", "tesseract") == backend]
        print(f"\n=== backend: {backend} ({len(rows)} cells) ===")
        _summarize_backend(rows)


def _summarize_backend(rows: list[dict]) -> None:
    sizes = sorted({r["size"] for r in rows})

    for doc_class in ("prose", "fixed"):
        cells = [r for r in rows if r["doc_class"] == doc_class]
        if not cells:
            continue
        fonts = sorted({r["font"] for r in cells})
        # ASCII-only output: the Windows console codepage (cp1251 here)
        # cannot encode arrows/dashes and print() would crash.
        print(f"\nCER% by font x em size - {doc_class} docs "
              f"(x-height px in parens)")
        print(f"{'font':<14}" + "".join(f"{s:>12}" for s in sizes))
        for font in fonts:
            out = [f"{font:<14}"]
            for size in sizes:
                sub = [r for r in cells
                       if r["font"] == font and r["size"] == size]
                if not sub:
                    out.append(f"{'-':>12}")
                    continue
                err = sum(r["char_errors"] for r in sub)
                chars = sum(r["truth_chars"] for r in sub)
                cer = 100 * err / chars if chars else 0.0
                out.append(f"{cer:>7.1f}({sub[0]['xheight']:>2})")
            print("".join(out))

    buckets: Counter = Counter()
    confusion: Counter = Counter()
    for r in rows:
        buckets.update(r["buckets"])
        confusion.update(r["confusion"])
    print("\nerror buckets (all cells):")
    for name, n in buckets.most_common():
        print(f"  {name:<18}{n:>8}")
    print("\ntop confusion pairs:")
    for pair, n in confusion.most_common(20):
        print(f"  {pair!r:<12}{n:>6}")

    ok_sum = sum(r["conf_correct_sum"] for r in rows)
    ok_n = sum(r["conf_correct_n"] for r in rows)
    bad_sum = sum(r["conf_error_sum"] for r in rows)
    bad_n = sum(r["conf_error_n"] for r in rows)
    if ok_n and bad_n:
        print(f"\nmean word conf: correct {ok_sum / ok_n:.1f} (n={ok_n}), "
              f"erroneous {bad_sum / bad_n:.1f} (n={bad_n})")
        hi_bad = sum(sum(r["conf_error"][8:]) for r in rows)
        print(f"erroneous words with conf >= 80: {hi_bad} "
              f"({100 * hi_bad / bad_n:.0f}% of errors)")

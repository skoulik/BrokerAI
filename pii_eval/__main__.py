import argparse
import sys

from pii.core.ocr import OCR_BACKENDS

# Canonical home of every generated corpus (gitignored): one folder per
# modality (text/, image/), one subfolder per seed.
CORPUS_ROOT = "pii_eval/corpora"


def _default_corpus(seed: int, modality: str = "text") -> str:
    return f"{CORPUS_ROOT}/{modality}/s{seed}"


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pii_eval",
        description="Synthetic PII evaluation corpus: generate and score.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="build a corpus with ground truth")
    gen.add_argument("-o", "--out", default=None,
                     help=f"output folder (default: {CORPUS_ROOT}/text/s<seed>)")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--docs", type=int, default=9)
    gen.add_argument(
        "--invalid", action=argparse.BooleanOptionalAction, default=True,
        help="append checksum-invalid injection docs (typo'd/malformed "
             "identifiers with ground truth)",
    )

    ren = sub.add_parser(
        "render", help="render a text corpus to images (paired image corpus)"
    )
    ren.add_argument("-c", "--corpus", default=None,
                     help="text corpus to render "
                          f"(default: {CORPUS_ROOT}/text/s<seed>)")
    ren.add_argument("-o", "--out", default=None,
                     help=f"output folder (default: {CORPUS_ROOT}/image/s<seed>)")
    ren.add_argument("--seed", type=int, default=42,
                     help="which seed's corpus to render when -c is not given")

    rep = sub.add_parser(
        "ocr-report",
        help="OCR-fidelity sweep: font × glyph size vs rendered truth "
             "(resumable)",
    )
    rep.add_argument("--seed", type=int, action="append", dest="seeds",
                     help="corpus seed; repeatable (default: 42 7 123)")
    rep.add_argument("-o", "--out", default=None,
                     help="report JSONL (appended; completed cells skipped; "
                          "default: pii_eval/reports/ocr_fidelity[_<backend>]"
                          ".jsonl)")
    rep.add_argument("--fonts", default=None,
                     help="comma-separated font files (default: all 9)")
    rep.add_argument("--sizes", default=None,
                     help="comma-separated em sizes (default: full grid)")
    rep.add_argument("--keep-images", action="store_true",
                     help="save rendered sweep pages next to the report")
    rep.add_argument("--summary-only", action="store_true",
                     help="re-print the summary of an existing report")
    rep.add_argument("--ocr-backend", choices=list(OCR_BACKENDS),
                     default="paddle",
                     help="OCR engine to sweep (default: paddle); the "
                          "default report file is suffixed per backend")

    imp = sub.add_parser(
        "import-real",
        help="render a folder of real PDFs into an image corpus "
             "(hand-authored truth.json)",
    )
    imp.add_argument("-s", "--src", required=True,
                     help="folder of source PDFs (e.g. sensitive/statements/1)")
    imp.add_argument("-o", "--out", default=None,
                     help=f"output folder (default: {CORPUS_ROOT}/real/"
                          "<src folder name>)")
    imp.add_argument("--dpi", type=int, default=None,
                     help="render resolution (default: 300)")

    gtr = sub.add_parser(
        "gt-render",
        help="paint a real corpus' ground-truth markup through the "
             "production painting seam (the perfect-pipeline reference)",
    )
    gtr.add_argument("-c", "--corpus", required=True,
                     help=f"real corpus folder (e.g. {CORPUS_ROOT}/real/1)")

    sc = sub.add_parser("score", help="run the pii pipeline and score it")
    sc.add_argument("-c", "--corpus", default=None,
                    help=f"corpus folder (default: {CORPUS_ROOT}/<modality>/s<seed>)")
    sc.add_argument("--seed", type=int, default=42,
                    help="which seed's corpus to score when -c is not given")
    sc.add_argument("--modality", choices=["text", "image"], default="text",
                    help="text: span/cell scoring; image: render pipeline + "
                         "re-OCR value survival")
    sc.add_argument("--threshold", type=float, default=0.4)
    sc.add_argument("--invalid-identifiers",
                    choices=["ignore", "all", "likely", "context"],
                    default="likely",
                    help="collection tier for checksum-invalid candidates")
    sc.add_argument("--ocr-backend", choices=list(OCR_BACKENDS),
                    default="paddle",
                    help="OCR engine for --modality image "
                         "(default: paddle)")

    args = parser.parse_args()
    if args.command == "generate":
        from pii_eval.generate import generate

        generate(args.out or _default_corpus(args.seed),
                 seed=args.seed, docs=args.docs,
                 invalid=args.invalid)
        return 0
    if args.command == "render":
        from pii_eval.render import render

        render(args.corpus or _default_corpus(args.seed),
               args.out or _default_corpus(args.seed, "image"))
        return 0
    if args.command == "import-real":
        from pathlib import Path

        from pii.core.pdf_mode import DEFAULT_DPI
        from pii_eval.realdocs import import_pdfs

        import_pdfs(args.src,
                    args.out or f"{CORPUS_ROOT}/real/{Path(args.src).name}",
                    dpi=args.dpi or DEFAULT_DPI)
        return 0
    if args.command == "gt-render":
        from pii_eval.realdocs import render_gt

        render_gt(args.corpus)
        return 0
    if args.command == "ocr-report":
        from pii_eval.ocr_report import default_out, run, summarize

        if args.summary_only:
            summarize(args.out or default_out(args.ocr_backend))
            return 0
        run(seeds=args.seeds,
            out=args.out,
            fonts=args.fonts.split(",") if args.fonts else None,
            sizes=[int(s) for s in args.sizes.split(",")]
            if args.sizes else None,
            keep_images=args.keep_images,
            ocr_backend=args.ocr_backend)
        return 0
    if args.modality == "image":
        from pii_eval.score_image import score_image

        return score_image(args.corpus or _default_corpus(args.seed, "image"),
                           threshold=args.threshold,
                           invalid_identifiers=args.invalid_identifiers,
                           ocr_backend=args.ocr_backend)
    from pii_eval.score import score

    return score(args.corpus or _default_corpus(args.seed),
                 threshold=args.threshold,
                 invalid_identifiers=args.invalid_identifiers)


if __name__ == "__main__":
    sys.exit(main())

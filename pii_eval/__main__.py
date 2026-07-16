import argparse
import sys

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
    if args.modality == "image":
        from pii_eval.score_image import score_image

        return score_image(args.corpus or _default_corpus(args.seed, "image"),
                           threshold=args.threshold,
                           invalid_identifiers=args.invalid_identifiers)
    from pii_eval.score import score

    return score(args.corpus or _default_corpus(args.seed),
                 threshold=args.threshold,
                 invalid_identifiers=args.invalid_identifiers)


if __name__ == "__main__":
    sys.exit(main())

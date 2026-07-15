import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="pii_eval",
        description="Synthetic PII evaluation corpus: generate and score.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="build a corpus with ground truth")
    gen.add_argument("-o", "--out", default="pii_eval/corpus")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--docs", type=int, default=9)
    gen.add_argument(
        "--invalid", action=argparse.BooleanOptionalAction, default=True,
        help="append checksum-invalid injection docs (typo'd/malformed "
             "identifiers with ground truth)",
    )

    sc = sub.add_parser("score", help="run the pii pipeline and score it")
    sc.add_argument("-c", "--corpus", default="pii_eval/corpus")
    sc.add_argument("--threshold", type=float, default=0.4)
    sc.add_argument("--invalid-identifiers",
                    choices=["ignore", "all", "likely", "context"],
                    default="likely",
                    help="collection tier for checksum-invalid candidates")

    args = parser.parse_args()
    if args.command == "generate":
        from pii_eval.generate import generate

        generate(args.out, seed=args.seed, docs=args.docs,
                 invalid=args.invalid)
        return 0
    from pii_eval.score import score

    return score(args.corpus,
                 threshold=args.threshold,
                 invalid_identifiers=args.invalid_identifiers)


if __name__ == "__main__":
    sys.exit(main())

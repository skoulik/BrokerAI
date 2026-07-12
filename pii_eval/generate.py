"""Corpus generation: N annotated documents + truth.json manifest."""

import json
from pathlib import Path

from pii_eval.personas import make_pool
from pii_eval.templates_csv import transactions_csv
from pii_eval.templates_text import legacy_statement, loan_application


def generate(outdir: str, seed: int = 42, docs: int = 9) -> Path:
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    pool = make_pool(seed)

    manifest = {"seed": seed, "docs": []}
    makers = [
        ("legacy", "txt", legacy_statement),
        ("loan", "txt", loan_application),
        ("tx", "csv", transactions_csv),
    ]
    for i in range(docs):
        stem, ext, make = makers[i % len(makers)]
        name = f"{stem}_{i:02d}.{ext}"
        if ext == "csv":
            text, anns = make(pool)
        else:
            doc = make(pool)
            text, anns = doc.text, doc.anns
        (out / name).write_text(text, encoding="utf-8")
        manifest["docs"].append(
            {
                "file": name,
                "kind": "csv" if ext == "csv" else "text",
                "entities": [a.to_json() for a in anns],
            }
        )

    (out / "truth.json").write_text(
        json.dumps(manifest, indent=1), encoding="utf-8"
    )
    n_ents = sum(len(d["entities"]) for d in manifest["docs"])
    print(f"{docs} docs, {n_ents} annotated entities -> {out}")
    return out

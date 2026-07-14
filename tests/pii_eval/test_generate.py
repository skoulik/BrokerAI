"""Corpus generator invariants: determinism and ground-truth alignment."""

import csv
import io
import json

from pii_eval.generate import generate


def _load(outdir):
    manifest = json.loads((outdir / "truth.json").read_text("utf-8"))
    return manifest


def test_same_seed_same_corpus(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    generate(str(a), seed=42, docs=6)
    generate(str(b), seed=42, docs=6)
    files = sorted(p.name for p in a.iterdir())
    assert files == sorted(p.name for p in b.iterdir())
    for name in files:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_truth_spans_align_with_documents(tmp_path):
    generate(str(tmp_path), seed=42, docs=9)
    manifest = _load(tmp_path)
    assert len(manifest["docs"]) == 11  # 9 base + 2 invalid-injection docs
    for doc in manifest["docs"]:
        text = (tmp_path / doc["file"]).read_text("utf-8")
        assert doc["entities"], doc["file"]
        if doc["kind"] == "csv":
            rows = list(csv.reader(io.StringIO(text)))
            for e in doc["entities"]:
                assert e["value"] in rows[e["row"]][e["col"]], (doc["file"], e)
        else:
            for e in doc["entities"]:
                assert text[e["start"] : e["end"]] == e["value"], (doc["file"], e)


def test_critical_flag_present(tmp_path):
    generate(str(tmp_path), seed=1, docs=3, invalid=False)
    manifest = _load(tmp_path)
    assert len(manifest["docs"]) == 3
    ents = [e for d in manifest["docs"] for e in d["entities"]]
    assert any(e["critical"] for e in ents)
    assert all("strip_expected" in e for e in ents)


def test_invalid_docs_appended_without_disturbing_base(tmp_path):
    plain, full = tmp_path / "plain", tmp_path / "full"
    generate(str(plain), seed=42, docs=3, invalid=False)
    generate(str(full), seed=42, docs=3)
    # base docs byte-identical with or without the injection docs
    for p in plain.iterdir():
        if p.name != "truth.json":
            assert p.read_bytes() == (full / p.name).read_bytes()
    names = {p.name for p in full.iterdir()} - {p.name for p in plain.iterdir()}
    assert names == {"loan_inv_03.txt", "tx_inv_04.csv"}


def test_invalid_annotations_cover_types_and_evidence_tiers(tmp_path):
    generate(str(tmp_path), seed=42, docs=3)
    manifest = _load(tmp_path)
    inv = [
        e
        for d in manifest["docs"]
        for e in d["entities"]
        if e["type"].endswith(("_INVALID", "_MALFORMED"))
    ]
    assert {e["type"] for e in inv} == {
        "AU_TFN_INVALID",
        "AU_MEDICARE_MALFORMED",
        "AU_ABN_INVALID",
        "CREDIT_CARD_INVALID",
    }
    assert {e["evidence"] for e in inv} == {"in-span", "context", "none"}
    # never expected strips, never critical-gate members
    assert all(not e["strip_expected"] and not e["critical"] for e in inv)

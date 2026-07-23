"""Corpus generator invariants: determinism and ground-truth alignment."""

import csv
import io
import json

from pii_eval.generate import generate
from pii_eval.personas import TOWNS


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
    # 9 base + name-forms statistics doc + 2 invalid-injection docs
    assert len(manifest["docs"]) == 12
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
    assert len(manifest["docs"]) == 4  # 3 base + name-forms doc
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
    assert names == {"loan_inv_04.txt", "tx_inv_05.csv"}


def test_known_hard_forms_present_and_not_gated(tmp_path):
    """The per-form probe types (corpus additions 2026-07-15) must keep
    appearing: bare street lines, suburb-suffixed merchant keep-orgs and
    account-holder private entities (ORGANIZATION_PRIVATE strip, 2026-07-21).
    The unfixed ones may not enter the critical gate; PERSON_JOINT is gated
    since the layer-1 joint-name recognizer took ownership (2026-07-15).
    Bare-town LOCATION became a KEEP probe when standalone location detection
    was retired (2026-07-23) — asserted in the keep-probe block below."""
    generate(str(tmp_path), seed=42, docs=9)
    ents = [e for d in _load(tmp_path)["docs"] for e in d["entities"]]
    by_type = {}
    for e in ents:
        by_type.setdefault(e["type"], []).append(e)

    for t in ("ADDRESS_BARE",
              "PERSON_JOINT", "PERSON_REVERSED", "CONTEXTUAL_ID",
              "PERSON_COMMA", "PERSON_PARTICLE", "PERSON_MULTIWORD",
              "ORGANIZATION_PRIVATE", "PERSON_COLLIDING",
              "ORGANIZATION_ATF"):
        assert by_type.get(t), f"probe type {t} missing from corpus"
        assert all(e["strip_expected"] for e in by_type[t]), t
        gated = t == "PERSON_JOINT"
        assert all(e["critical"] == gated for e in by_type[t]), t

    # The name-forms doc fixes per-form n by construction — real
    # statistics, not the pool templates' handful of random draws.
    assert len(by_type["PERSON_REVERSED"]) >= 32, "reversed sample too small"
    assert len(by_type["PERSON_COMMA"]) >= 16, "comma sample too small"

    # Joint-name recognizer trade-off keep-probes (2026-07-15): 'AND'-orgs
    # with a corporate marker (must keep) and without one (the documented
    # sacrifice). Per-form keep rows, never gate members. The issue-#10
    # trio (2026-07-22): letter+10-digit receipt refs, >16-digit runs and
    # masked last-4 card disclosures must survive identifier
    # post-validation unstripped.
    for t in ("LOCATION", "ORGANIZATION_AND", "ORGANIZATION_AND_BARE",
              "PROSE_AND", "AMOUNT_COLUMN", "REFERENCE_NUMBER",
              "DIGITS_OVERLONG", "CARD_LAST4", "TRAILING_AMOUNT", "AU_AFSL",
              "AU_CREDIT_LICENCE"):
        assert by_type.get(t), f"probe type {t} missing from corpus"
        assert all(not e["strip_expected"] and not e["critical"]
                   for e in by_type[t]), t

    # Colliding-surname joint draws are the non-gated PERSON_COLLIDING probe.
    assert any(
        e["value"].split()[-1].upper() in ("FEE", "CARD")
        for e in by_type["PERSON_COLLIDING"]
    ), "no colliding-surname draw in corpus"

    # Account-holder private entities: a trust and a PTY LTD name must appear
    # as strip-expected ORGANIZATION_PRIVATE (org_policy, 2026-07-21) — the
    # reverse of the old keep-org stance.
    private = [e["value"] for e in by_type["ORGANIZATION_PRIVATE"]]
    assert any("TRUST" in v for v in private), "no trust name as private-org"
    assert any("PTY LTD" in v for v in private), "no PTY LTD name as private-org"

    # Merchants/institutions remain keep-orgs, including suburb-suffixed forms.
    orgs = [e["value"] for e in by_type["ORGANIZATION"]]
    towns = {t.upper() for t in TOWNS}
    assert any(v.split()[-1] in towns for v in orgs), \
        "no suburb-suffixed merchant keep-org"


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

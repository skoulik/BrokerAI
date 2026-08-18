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
    A probe enters the critical gate only once its form is reliably covered:
    PERSON_JOINT since the layer-1 joint-name recognizer took ownership
    (2026-07-15), PERSON_REVERSED since layer 0 closed its residual at 100%
    on seeds 42/123/7 (2026-08-09). Bare-town LOCATION became a KEEP probe
    when standalone location detection was retired (2026-07-23) — asserted in
    the keep-probe block below."""
    generate(str(tmp_path), seed=42, docs=9)
    ents = [e for d in _load(tmp_path)["docs"] for e in d["entities"]]
    by_type = {}
    for e in ents:
        by_type.setdefault(e["type"], []).append(e)

    for t in ("ADDRESS_BARE",
              "PERSON_JOINT", "PERSON_REVERSED", "CONTEXTUAL_ID",
              "PERSON_COMMA", "PERSON_PARTICLE", "PERSON_MULTIWORD",
              "ORGANIZATION_PRIVATE", "PERSON_COLLIDING",
              "ORGANIZATION_ATF",
              # Corporate licences moved keep -> strip 2026-08-14 (Sergei,
              # "for now"). Not gated: the decision is provisional, so it
              # must not become a release blocker.
              "AU_AFSL", "AU_CREDIT_LICENCE",
              # The joint name with no constituents anywhere in the document
              # (2026-08-14). SHOULD strip and cannot: joint names are derived
              # from people already detected, and nothing names these two. It
              # stays in the corpus, strip-expected and non-gated, so the loss
              # is scored on every run instead of being deleted from it.
              "PERSON_JOINT_NO_EVIDENCE",
              # A label sitting directly ABOVE its value (2026-08-14). Strips
              # on the image tier, where a page has columns; a known MISS on
              # the text tier, which is left-only by decision. Non-gated for
              # that reason, and kept so the difference is scored rather than
              # remembered.
              "ACCOUNT_LABELLED_ABOVE"):
        assert by_type.get(t), f"probe type {t} missing from corpus"
        assert all(e["strip_expected"] for e in by_type[t]), t
        gated = t in ("PERSON_JOINT", "PERSON_REVERSED")
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
    # REFERENCE_ACROSS_COLUMN (2026-08-14): an unrelated reference in the
    # right column of a line whose LEFT column happens to carry an account
    # label. The retired 60-character lookback stripped it; visual attachment
    # must not.
    for t in ("LOCATION", "ORGANIZATION_AND", "ORGANIZATION_AND_BARE",
              "PROSE_AND", "AMOUNT_COLUMN", "REFERENCE_NUMBER",
              "DIGITS_OVERLONG", "CARD_LAST4", "TRAILING_AMOUNT",
              "REFERENCE_ACROSS_COLUMN", "YEAR_ACROSS_COLUMN"):
        assert by_type.get(t), f"probe type {t} missing from corpus"
        assert all(not e["strip_expected"] and not e["critical"]
                   for e in by_type[t]), t

    # Colliding-surname joint draws are the non-gated PERSON_COLLIDING probe.
    assert any(
        e["value"].split()[-1].upper() in ("FEE", "CARD")
        for e in by_type["PERSON_COLLIDING"]
    ), "no colliding-surname draw in corpus"

    # Account-holder private entities: a trust and a PTY LTD name must appear
    # as strip-expected ORGANIZATION_PRIVATE — no keep list names them
    # (pii.core.entity_keep), the reverse of the old keep-org stance.
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


def test_documents_span_pages_and_repeat_their_holder(tmp_path):
    """A one-page corpus cannot see the failure the two-sweep pipeline exists
    for. Statements must therefore span pages AND reprint the holder and the
    account number on each — a document that merely grows longer would test
    pagination, not cross-page detection."""
    corpus = generate(str(tmp_path / "c"), seed=42, docs=3)
    truth = json.loads((corpus / "truth.json").read_text("utf-8"))
    by_file = {d["file"]: d for d in truth["docs"]}

    statement = next(f for f in by_file if f.startswith("legacy"))
    pages = (corpus / statement).read_text("utf-8").split("\f")
    assert len(pages) > 1, "the statement must span pages"

    # Page boundaries in offset space, so an annotation can be placed.
    bounds, at = [], 0
    for page in pages:
        bounds.append((at, at + len(page)))
        at += len(page) + 1

    def page_of(ann):
        return next(i for i, (a, b) in enumerate(bounds, 1)
                    if a <= ann["start"] < b)

    entities = by_file[statement]["entities"]
    accounts = [e for e in entities if e["type"] == "AU_BANK_ACCOUNT"]
    assert {page_of(e) for e in accounts} == set(range(1, len(pages) + 1)), \
        "the account number must be reprinted on every page"

    # The holder in caps on page 1 and title case on the continuation pages:
    # the same entity under two surface forms, which is what the document-wide
    # grouping has to recognize.
    people = [e for e in entities if e["type"] == "PERSON"]
    caps = {e["value"] for e in people if page_of(e) == 1 and e["value"].isupper()}
    later = {e["value"] for e in people if page_of(e) > 1}
    assert caps, "no caps-form holder on page 1"
    assert any(v.upper() in caps for v in later), \
        "no title-case reprint of a page-1 name on a later page"


def test_the_name_forms_doc_stays_one_page(tmp_path):
    # Every row is a different person by construction, so it has nothing to
    # carry across a break — pagination would only spend model time.
    corpus = generate(str(tmp_path / "c"), seed=42, docs=3)
    truth = json.loads((corpus / "truth.json").read_text("utf-8"))
    names = next(d["file"] for d in truth["docs"]
                 if d["file"].startswith("names"))
    assert "\f" not in (corpus / names).read_text("utf-8")


def test_the_account_name_is_reprinted_truncated(tmp_path):
    """The specimen that motivated fuzzy borrowed matching (2026-08-11): a
    value printed in full on one page and TRUNCATED to a fixed-width field on
    another. Both certain matching tiers miss it — the known value is a strict
    superstring of what the page prints — so the probe isolates the fuzzy
    borrowed tier. (Until 2026-08-11 the truncation also removed the
    legal-form marker the org policy keyed on, which made it a leak outright.)
    """
    corpus = generate(str(tmp_path / "c"), seed=42, docs=3)
    truth = json.loads((corpus / "truth.json").read_text("utf-8"))
    statement = next(d for d in truth["docs"] if d["file"].startswith("legacy"))
    entities = statement["entities"]

    truncated = [e for e in entities if e["type"] == "ORGANIZATION_TRUNCATED"]
    assert truncated, "no truncated account-name probe"
    full = {e["value"] for e in entities if e["type"] == "ORGANIZATION_PRIVATE"}
    for probe in truncated:
        assert probe["strip_expected"] is True
        # A genuine truncation of a full form printed elsewhere in the doc,
        # not a different name that merely looks similar.
        assert any(name.startswith(probe["value"]) for name in full), probe
        assert probe["value"] not in full


def test_the_statement_wraps_its_address_inside_a_two_column_block(tmp_path):
    """The 2026-08-13 fail mode: a value that wraps inside ONE column of a
    two-column block. `ocr_page._rows` bands such a page visually, so the
    other column's field lands between the address's halves and no contiguous
    search reaches across it. The probe needs both halves to be a truth entity
    of their own type, and the right column's field to sit between them on the
    line — otherwise the corpus renders an ordinary one-column address and
    measures nothing.
    """
    corpus = generate(str(tmp_path / "c"), seed=42, docs=3)
    truth = json.loads((corpus / "truth.json").read_text("utf-8"))
    statement = next(d for d in truth["docs"] if d["file"].startswith("legacy"))
    text = (corpus / statement["file"]).read_text("utf-8")

    wrapped = [e for e in statement["entities"] if e["type"] == "ADDRESS_WRAPPED"]
    # Page 1 prints the pair, and every continuation page reprints it — the
    # borrowed half of the fix is only measurable on the second printing.
    assert len(wrapped) >= 4 and len(wrapped) % 2 == 0
    for probe in wrapped:
        assert probe["strip_expected"] is True

    street, suburb = wrapped[0], wrapped[1]
    between = text[street["end"] : suburb["start"]]
    assert "\n" in between, "the address does not wrap"
    assert between.strip(), "no second column between the halves"
    # Both halves start in the same column, which is what the borrowed tier's
    # x-overlap guard keys on once this is rendered.
    assert text.rfind("\n", 0, street["start"]) - street["start"] == (
        text.rfind("\n", 0, suburb["start"]) - suburb["start"]
    )

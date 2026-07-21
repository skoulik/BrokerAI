"""GLiNER2 window isolation at RECORD_SEPARATOR + same-type coalescing.

The 2026-07-15 reversed-name diagnosis (records in pii/DONE.md): same-person
mentions in different word orders interfere inside one attention window, so
csv_mode's cell sentinel is now a hard window boundary; and isolated lines
emit reversed names as adjacent fragments whose union misses only the
joining space, so PERSON spans coalesce like ADDRESS spans always did.
Model-free via the fake-model pattern (see test_gliner2_floors)."""

from pii.core import RECORD_SEPARATOR
from pii.core.gliner2_recognizer import Gliner2Recognizer


class _RecordingFakeModel:
    """Returns preset entities per label; records the window texts each
    batch call received."""

    max_width = 8

    class span_rep:  # noqa: N801 - mimics the real attribute path
        class span_rep_layer:  # noqa: N801
            max_width = 8

    def __init__(self, per_label):
        self.per_label = per_label
        self.calls: list[list[str]] = []

    def batch_extract_entities(self, texts, schema, batch_size, threshold,
                               include_confidence, include_spans):
        self.calls.append(list(texts))
        return [
            {
                "entities": {
                    label: [{"text": tx, "confidence": 0.95}
                            for tx in self.per_label[label]]
                    for label in schema if label in self.per_label
                }
            }
            for _ in texts
        ]


def test_record_separator_splits_windows():
    rec = Gliner2Recognizer()
    model = _RecordingFakeModel({"person": ["SCHAEFER"]})
    rec._model = model
    text = (
        f"OSKO P12 SCHAEFER JOSEPH RENT\n{RECORD_SEPARATOR}\n"
        f"PAYID PAYMENT FROM JOSEPH SCHAEFER"
    )
    results = rec.analyze(text, entities=None)
    # every pass saw two windows — one per cell, none containing the other
    assert model.calls and all(len(c) == 2 for c in model.calls)
    assert all(RECORD_SEPARATOR not in w for c in model.calls for w in c)
    # occurrence spans map back to global offsets in both segments
    persons = [r for r in results if r.entity_type == "PERSON"]
    found = sorted(text[r.start:r.end] for r in persons)
    assert found and all(s.upper() == "SCHAEFER" for s in found)
    assert any(r.start > text.index(RECORD_SEPARATOR) for r in persons)
    assert any(r.start < text.index(RECORD_SEPARATOR) for r in persons)


def test_plain_text_is_single_window():
    rec = Gliner2Recognizer()
    model = _RecordingFakeModel({"person": []})
    rec._model = model
    rec.analyze("Transfer to Joseph Schaefer, ref 123", entities=None)
    assert all(len(c) == 1 for c in model.calls)


def test_adjacent_person_fragments_coalesce():
    # The reversed-name fragment shape: 'SCHAEFER' + 'JOSEPH RENT' with a
    # one-space gap must become one PERSON span (the space must not leak).
    rec = Gliner2Recognizer()
    rec._model = _RecordingFakeModel({"person": ["SCHAEFER", "JOSEPH RENT"]})
    text = "OSKO P12345678 SCHAEFER JOSEPH RENT"
    persons = [
        r for r in rec.analyze(text, entities=None)
        if r.entity_type == "PERSON"
    ]
    assert len(persons) == 1
    assert text[persons[0].start:persons[0].end] == "SCHAEFER JOSEPH RENT"


def test_distant_person_spans_stay_separate():
    rec = Gliner2Recognizer()
    rec._model = _RecordingFakeModel({"person": ["Julie Summers", "Brian Reid"]})
    text = "From Julie Summers to Brian Reid"
    persons = [
        r for r in rec.analyze(text, entities=None)
        if r.entity_type == "PERSON"
    ]
    assert len(persons) == 2  # ' to ' is not a comma/whitespace gap


def test_reversed_two_token_person_refound_from_canonical():
    # The 1.pdf 'KULIK OLGA' leak: under same-window interference the model
    # returns only the canonical 'OLGA KULIK' (the reversed mention collapses
    # to a sub-threshold surname fragment), so the given name leaks. The
    # reversed order must be re-found from the confident canonical detection.
    rec = Gliner2Recognizer()
    rec._model = _RecordingFakeModel({"person": ["OLGA KULIK"]})
    text = "OLGA KULIK\nAccount name(s) KULIK OLGA"
    found = sorted(
        text[r.start:r.end]
        for r in rec.analyze(text, entities=None)
        if r.entity_type == "PERSON"
    )
    assert "OLGA KULIK" in found
    assert "KULIK OLGA" in found  # reversed order recovered — no OLGA leak


def test_joint_connector_merges_couple_fragments():
    # Full-name joint form (issue #4): GLiNER2 emits a fragment on each side;
    # they merge across the ' and ' / ' & ' connector into the couple's single
    # span. Name-signal-gated by construction (both sides are real detections).
    rec = Gliner2Recognizer()
    rec._model = _RecordingFakeModel({"person": ["JULIE", "BRIAN SUMMERS"]})
    text = "OSKO P12345678 JULIE AND BRIAN SUMMERS RENT"
    persons = [
        r for r in rec.analyze(text, entities=None) if r.entity_type == "PERSON"
    ]
    assert len(persons) == 1
    assert text[persons[0].start:persons[0].end] == "JULIE AND BRIAN SUMMERS"


def test_joint_connector_merges_title_case():
    rec = Gliner2Recognizer()
    rec._model = _RecordingFakeModel({"person": ["Jeffrey", "Randall Lawrence"]})
    text = "To Jeffrey and Randall Lawrence today"
    (person,) = [
        r for r in rec.analyze(text, entities=None) if r.entity_type == "PERSON"
    ]
    assert text[person.start:person.end] == "Jeffrey and Randall Lawrence"


def test_joint_connector_keeps_distinct_people_separate():
    # Two full names either side of ' and ' are DISTINCT people, not a couple.
    # The single-token guard on the left fragment keeps them separate so they
    # get distinct placeholders.
    rec = Gliner2Recognizer()
    rec._model = _RecordingFakeModel({"person": ["Julie Summers", "Brian Reid"]})
    text = "Pay Julie Summers and Brian Reid now"
    persons = [
        r for r in rec.analyze(text, entities=None) if r.entity_type == "PERSON"
    ]
    assert len(persons) == 2
    assert sorted(text[p.start:p.end] for p in persons) == [
        "Brian Reid", "Julie Summers",
    ]


def test_three_token_person_not_reversed():
    # FP guard: reversal is restricted to two-token names. A 3-token name is
    # not reversed (particle/middle-name order is ambiguous).
    rec = Gliner2Recognizer()
    rec._model = _RecordingFakeModel({"person": ["ANNA MARIE SMITH"]})
    text = "ANNA MARIE SMITH paid rent to SMITH MARIE ANNA today"
    persons = [
        text[r.start:r.end]
        for r in rec.analyze(text, entities=None)
        if r.entity_type == "PERSON"
    ]
    assert "ANNA MARIE SMITH" in persons
    assert "SMITH MARIE ANNA" not in persons

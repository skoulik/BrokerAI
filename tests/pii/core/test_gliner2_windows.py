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

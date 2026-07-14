"""Min-length floors on GLiNER2 span emissions (2026-07-14).

Two guards live in Gliner2Recognizer.analyze, tested here without loading the
model by feeding controlled predictions through a fake:

- AU_BANK_ACCOUNT digit floor (always on): a fragment like '42' is dropped,
  but a real account written with spaces ('0007 3111 4') is emitted by the
  model as ONE span and survives — the count is on digits, so the internal
  spaces don't push it under the floor. This is the reassembly case.
- LOCATION char floor (location=True only): short codes/acronyms ('NAB')
  are dropped, real place names ('Wagga Wagga') kept.
"""

from pii.gliner2_recognizer import Gliner2Recognizer


class _FakeModel:
    """Returns preset entities for whichever labels a pass asks about."""

    max_width = 8

    class span_rep:  # noqa: N801 - mimics the real attribute path
        class span_rep_layer:  # noqa: N801
            max_width = 8

    def __init__(self, per_label):
        self.per_label = per_label  # label -> list of surface texts

    def batch_extract_entities(self, texts, schema, batch_size, threshold,
                               include_confidence, include_spans):
        out = []
        for _t in texts:
            ents = {
                label: [{"text": tx, "confidence": 0.95}
                        for tx in self.per_label[label]]
                for label in schema if label in self.per_label
            }
            out.append({"entities": ents})
        return out


def _spans(rec, text, entity_type):
    return sorted(
        text[r.start:r.end]
        for r in rec.analyze(text, entities=None)
        if r.entity_type == entity_type
    )


def test_bank_account_digit_floor_keeps_spaced_account():
    rec = Gliner2Recognizer()
    rec._model = _FakeModel({"bank account number": ["42", "0007 3111 4"]})
    text = "ref 42 and account 0007 3111 4 here"
    got = _spans(rec, text, "AU_BANK_ACCOUNT")
    assert "0007 3111 4" in got   # 9 digits across spaces -> reassembled, kept
    assert "42" not in got        # 2-digit fragment -> dropped


def test_location_char_floor_drops_short_tokens():
    rec = Gliner2Recognizer(location=True)
    rec._model = _FakeModel({"location": ["NAB", "Wagga Wagga"]})
    text = "paid NAB while visiting Wagga Wagga today"
    got = _spans(rec, text, "LOCATION")
    assert "Wagga Wagga" in got
    assert "NAB" not in got

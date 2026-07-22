"""Post-emission guards on GLiNER2 spans, tested without loading the model
by feeding controlled predictions through a fake.

- LOCATION char floor (2026-07-14, always on): short codes/acronyms ('NAB')
  are dropped, real place names ('Wagga Wagga') kept — and genuine
  3-letter suburbs ('Kew') knowingly fall with the acronyms (the
  LOCATION_SHORT corpus probe; the gazetteer task is the recovery path).
- Identifier post-validation (issue #10, 2026-07-22): every numeric-ID
  guess passes its class arithmetic/format before it may strip
  (IDENTIFIER_VALIDATORS). Shape-correct checksum failures demote to the
  *_INVALID shadow classes (or drop when demote_invalid=False, the
  'ignore' tier); structurally impossible values — the letter+10-digit
  bank receipt references the model labeled TFN/licence/passport on real
  statements — are plain-dropped. The AU_BANK_ACCOUNT digit floor
  (2026-07-14; '42' dropped, spaced '0007 3111 4' reassembled and kept)
  is folded into the same table, now with a 16-digit cap on top.
"""

from pii.core.gliner2_recognizer import Gliner2Recognizer


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
    rec = Gliner2Recognizer()
    rec._model = _FakeModel({"location": ["NAB", "Kew", "Wagga Wagga"]})
    text = "paid NAB near Kew while visiting Wagga Wagga today"
    got = _spans(rec, text, "LOCATION")
    assert "Wagga Wagga" in got
    assert "NAB" not in got
    # the documented sacrifice: a real 3-letter suburb falls with the
    # acronyms — the location pass itself cannot protect it
    assert "Kew" not in got


# Checksum-verified literals (same convention as test_invalid.py):
VALID_TFN = "291 417 774"        # passes TFN mod-11
INVALID_TFN = "291 417 775"      # single-digit typo, fails mod-11
LEGACY_TFN = "12345678"          # 8 digits: structural pass, no checksum
RECEIPT_REF = "W1045366576"      # letter + 10 digits: bank receipt junk
VALID_MEDICARE = "2123 45670 1"    # weighted sum 170 -> check digit 0
INVALID_MEDICARE = "2123 45675 1"  # check digit 5 fails
MALFORMED_MEDICARE = "9123 45670 1"  # first digit outside 2-6


def test_bank_account_digit_cap_drops_junk_run():
    # 22 digits can never be an AU account (10 digits + 6-digit BSB = 16
    # max) — the issue-#10-sibling run that over-extended a card via
    # _merge_overlaps.
    junk = "4564942700010443013795"
    rec = Gliner2Recognizer()
    rec._model = _FakeModel({"bank account number": [junk]})
    assert _spans(rec, f"ref {junk} end", "AU_BANK_ACCOUNT") == []


def test_tfn_receipt_reference_dropped_entirely():
    # Structurally impossible (10 digits): dropped, NOT demoted — junk is
    # not a mangled TFN.
    rec = Gliner2Recognizer()
    rec._model = _FakeModel({"tax file number": [RECEIPT_REF]})
    text = f"MISCELLANEOUS CREDIT {RECEIPT_REF}"
    assert _spans(rec, text, "AU_TFN") == []
    assert _spans(rec, text, "AU_TFN_INVALID") == []


def test_tfn_valid_and_legacy_kept_invalid_demoted():
    rec = Gliner2Recognizer()
    rec._model = _FakeModel(
        {"tax file number": [VALID_TFN, LEGACY_TFN, INVALID_TFN]}
    )
    text = f"TFN {VALID_TFN} or {LEGACY_TFN} but {INVALID_TFN}"
    assert _spans(rec, text, "AU_TFN") == sorted([VALID_TFN, LEGACY_TFN])
    assert _spans(rec, text, "AU_TFN_INVALID") == [INVALID_TFN]


def test_demote_invalid_off_drops_checksum_failures():
    # The 'ignore' tier wiring: shape-correct failures vanish silently.
    rec = Gliner2Recognizer(demote_invalid=False)
    rec._model = _FakeModel({"tax file number": [INVALID_TFN]})
    text = f"TFN {INVALID_TFN}"
    assert _spans(rec, text, "AU_TFN") == []
    assert _spans(rec, text, "AU_TFN_INVALID") == []


def test_medicare_checksum_and_structure():
    rec = Gliner2Recognizer()
    rec._model = _FakeModel(
        {"medicare number": [VALID_MEDICARE, INVALID_MEDICARE,
                             MALFORMED_MEDICARE]}
    )
    text = (f"cards {VALID_MEDICARE} and {INVALID_MEDICARE} "
            f"and {MALFORMED_MEDICARE}")
    assert _spans(rec, text, "AU_MEDICARE") == [VALID_MEDICARE]
    assert _spans(rec, text, "AU_MEDICARE_INVALID") == [INVALID_MEDICARE]
    # first digit outside 2-6 is structurally impossible -> plain drop


def test_passport_digit_cap():
    rec = Gliner2Recognizer()
    # 'PA1234567' is the real AU shape (7 digits); the receipt ref carries
    # 10 digits and no passport format reaches that.
    rec._model = _FakeModel({"passport number": ["PA1234567", "F7624452812"]})
    text = "passport PA1234567 vs ref F7624452812"
    assert _spans(rec, text, "PASSPORT") == ["PA1234567"]


def test_licence_alnum_cap():
    rec = Gliner2Recognizer()
    rec._model = _FakeModel(
        {"driver licence number": [
            "123456789",                        # 9 alnum: plausible licence
            "L2724656893",                      # 11 alnum: receipt junk
            "Australian credit licence 230686",  # AFSL-style phrase
        ]}
    )
    text = ("DL 123456789 then L2724656893 then "
            "Australian credit licence 230686")
    assert _spans(rec, text, "AU_DRIVERS_LICENCE") == ["123456789"]

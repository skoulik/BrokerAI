"""PseudonymMap: stable placeholders, rehydration, persistence."""

from pii.mapping import PseudonymMap


def test_placeholder_stable_and_case_insensitive():
    m = PseudonymMap()
    first = m.placeholder_for("PERSON", "John Smith")
    assert first == "PERSON_1"
    # case-insensitive, whitespace-collapsed matching
    assert m.placeholder_for("PERSON", "JOHN  SMITH") == first
    assert m.placeholder_for("PERSON", "Jane Doe") == "PERSON_2"


def test_placeholder_prefix_mapping():
    m = PseudonymMap()
    assert m.placeholder_for("AU_TFN", "291 417 774") == "TFN_1"
    assert m.placeholder_for("AU_BANK_ACCOUNT", "7412154728") == "ACCOUNT_1"


def test_rehydrate_restores_first_seen_form():
    m = PseudonymMap()
    m.placeholder_for("PERSON", "John Smith")
    m.placeholder_for("PERSON", "JOHN SMITH")  # same person, later form
    assert m.rehydrate("Dear PERSON_1,") == "Dear John Smith,"


def test_rehydrate_leaves_unknown_placeholders():
    m = PseudonymMap()
    assert m.rehydrate("PERSON_7 stays") == "PERSON_7 stays"


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "map.json"
    m = PseudonymMap(path)
    m.placeholder_for("EMAIL_ADDRESS", "olga@example.com")
    m.save()
    m2 = PseudonymMap(path)
    # continues numbering and rehydrates from the stored state
    assert m2.placeholder_for("EMAIL_ADDRESS", "olga@example.com") == "EMAIL_1"
    assert m2.placeholder_for("EMAIL_ADDRESS", "new@example.com") == "EMAIL_2"
    assert m2.rehydrate("EMAIL_1") == "olga@example.com"

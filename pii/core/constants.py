"""Engine-wide constants with no imports, so any core module can depend on
them without risking an import cycle."""

# Hard NER window boundary (U+241E SYMBOL FOR RECORD SEPARATOR). csv_mode joins
# independent cells with it; the GLiNER2 recognizer never lets a prediction
# window span across it (2026-07-15: same-person mentions in different word
# orders interfere inside one attention window — see
# pii/core/gliner2_recognizer.py).
RECORD_SEPARATOR = "␞"

"""Local PII stripping tool (BrokerAI Phase 1).

Standalone from the RAG app. Detects PII with layered analyzers
(Presidio patterns incl. Australian entities, GLiNER2 NER) and replaces it
with consistent pseudonyms (John Smith -> PERSON_1) recorded in a local
mapping store, so cloud responses can be rehydrated locally.
"""

# Hard NER window boundary (U+241E SYMBOL FOR RECORD SEPARATOR). csv_mode
# joins independent cells with it; the GLiNER2 recognizer never lets a
# prediction window span across it (2026-07-15: same-person mentions in
# different word orders interfere inside one attention window — see
# pii/gliner2_recognizer.py). Defined here, above the imports, so both
# modules share one definition without a cycle.
RECORD_SEPARATOR = "␞"

from pii.mapping import PseudonymMap
from pii.pipeline import PiiPipeline

__all__ = ["PiiPipeline", "PseudonymMap", "RECORD_SEPARATOR"]

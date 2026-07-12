"""Local PII stripping tool (BrokerAI Phase 1).

Standalone from the RAG app. Detects PII with layered analyzers
(Presidio patterns incl. Australian entities, GLiNER NER) and replaces it
with consistent pseudonyms (John Smith -> PERSON_1) recorded in a local
mapping store, so cloud responses can be rehydrated locally.
"""

from pii.mapping import PseudonymMap
from pii.pipeline import PiiPipeline

__all__ = ["PiiPipeline", "PseudonymMap"]

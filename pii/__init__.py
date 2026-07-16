"""Local PII stripping tool (BrokerAI Phase 1).

Umbrella package. The engine lives in ``pii.core``; the command-line front-end
in ``pii.cli``; a GUI front-end (``pii.gui``) is planned. ``cli`` and ``gui``
both build on ``pii.core`` and must not import each other. See
pii/ARCHITECTURE.md for the component map and pii/core/ARCHITECTURE.md for the
engine design.

The names below are re-exported from ``pii.core`` as a convenience for existing
callers; ``pii.core`` is the canonical import path.
"""

from pii.core import PiiPipeline, PseudonymMap, RECORD_SEPARATOR

__all__ = ["PiiPipeline", "PseudonymMap", "RECORD_SEPARATOR"]

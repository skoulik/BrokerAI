"""Local PII stripping tool (BrokerAI Phase 1).

Umbrella package. The engine lives in ``pii.core``; the command-line front-end
in ``pii.cli``; a GUI front-end (``pii.gui``) is planned. ``cli`` and ``gui``
both build on ``pii.core`` and must not import each other. See
pii/ARCHITECTURE.md for the component map and pii/core/ARCHITECTURE.md for the
engine design.

The names below are re-exported from ``pii.core`` as a convenience for existing
callers; ``pii.core`` is the canonical import path. Resolution is lazy for the
same load-bearing reason as in ``pii.core.__init__`` (torch-free OCR-only
processes; see that docstring).
"""

_REEXPORT = ("PiiPipeline", "PseudonymMap", "RECORD_SEPARATOR")

__all__ = list(_REEXPORT)


def __getattr__(name: str):
    if name in _REEXPORT:
        import pii.core

        return getattr(pii.core, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(__all__)

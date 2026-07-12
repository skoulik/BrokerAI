"""Tier 1 synthetic evaluation corpus for the pii stripping tool.

Generates Australian bank statements (plain text + CSV, later PDF/image)
populated with checksum-valid fake PII, with ground truth known by
construction, and scores the pii pipeline against it (recall-first).

Everything here is synthetic — the corpus is shareable by design. Layouts
and description phrasing are modeled on the reference documents in
sensitive/statements/ but contain nothing from them.
"""

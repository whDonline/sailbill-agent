"""Multi-format ingestion.

``load_tree`` is the entry point: it turns one input file into a *flat* list of
:class:`NormalizedDocument`, recursively expanding EML attachments.  Flattening
(rather than nesting) keeps the pipeline simple, while ``meta['parent_id']`` and
``meta['root_id']`` preserve the fact that "this DN came attached to that email"
so a bill is still assembled from the whole message rather than from a stray
attachment.
"""

from __future__ import annotations

from .router import load_document, load_tree, sniff_kind

__all__ = ["load_document", "load_tree", "sniff_kind"]

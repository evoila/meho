# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Public API for the doc-collections package (G4.6 T1 collections-as-data).

Re-exports the read schemas, the single ORM→wire projection, the
resolver surface, and (T6 #1555) the readiness/lifecycle surface so
downstream tasks can import from ``meho_backplane.docs_collections``
without knowing the internal module split. The backend-agnostic search
router (T2 #1551), collection-scoped search (T3 #1552), and the catalogue
tool / CLI (T4 #1553) all build on this surface; the probe / enable /
disable routes and the search-time readiness guard build on the
``lifecycle`` + ``service`` modules (T6 #1555).
"""

from meho_backplane.docs_collections.lifecycle import (
    DocCollectionDisabledError,
    DocCollectionNotReadyError,
    DocCollectionStateError,
    ensure_collection_searchable,
)
from meho_backplane.docs_collections.resolver import (
    DocCollectionNotFoundError,
    resolve_doc_collection,
)
from meho_backplane.docs_collections.schemas import (
    DocCollection,
    DocCollectionSummary,
    project_doc_collection_to_summary,
)
from meho_backplane.docs_collections.service import (
    probe_collection,
    set_collection_enabled,
)

__all__ = [
    "DocCollection",
    "DocCollectionDisabledError",
    "DocCollectionNotFoundError",
    "DocCollectionNotReadyError",
    "DocCollectionStateError",
    "DocCollectionSummary",
    "ensure_collection_searchable",
    "probe_collection",
    "project_doc_collection_to_summary",
    "resolve_doc_collection",
    "set_collection_enabled",
]

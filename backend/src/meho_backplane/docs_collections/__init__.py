# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Public API for the doc-collections package (G4.6 T1 collections-as-data).

Re-exports the read schemas, the single ORM→wire projection, and the
resolver surface so downstream tasks can import from
``meho_backplane.docs_collections`` without knowing the internal module
split. The backend-agnostic search router (T2 #1551), collection-scoped
search (T3 #1552), and the catalogue tool / CLI (T4 #1553) all build on
this surface.
"""

from meho_backplane.docs_collections.resolver import (
    DocCollectionNotFoundError,
    resolve_doc_collection,
)
from meho_backplane.docs_collections.schemas import (
    DocCollection,
    DocCollectionSummary,
    project_doc_collection_to_summary,
)

__all__ = [
    "DocCollection",
    "DocCollectionNotFoundError",
    "DocCollectionSummary",
    "project_doc_collection_to_summary",
    "resolve_doc_collection",
]

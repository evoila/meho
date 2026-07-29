# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Public API for the doc-collections package (G4.6 T1 collections-as-data).

Re-exports the read schemas, the single ORM→wire projection, the
resolver surface, and (T6 #1555) the readiness/lifecycle surface so
downstream tasks can import from ``meho_backplane.docs_collections``
without knowing the internal module split. The backend-agnostic search
router (T2 #1551), collection-scoped search (T3 #1552), and the catalogue
tool / CLI (T4 #1553) all build on this surface; the probe / enable /
disable routes build on the ``lifecycle`` + ``service`` modules (T6
#1555). The search-time readiness rejection is the access gate's
(:func:`~meho_backplane.docs_search.resolve_entitled_ready_collection`),
not this package's — ``lifecycle`` owns only the write-side status machine.
"""

from meho_backplane.docs_collections.lifecycle import (
    DocCollectionStateError,
)
from meho_backplane.docs_collections.resolver import (
    DocCollectionNotFoundError,
    resolve_doc_collection,
)
from meho_backplane.docs_collections.schemas import (
    DocCollection,
    DocCollectionCreate,
    DocCollectionCreateResponse,
    DocCollectionSummary,
    project_doc_collection,
    project_doc_collection_create_response,
    project_doc_collection_to_summary,
)
from meho_backplane.docs_collections.service import (
    DocCollectionBackendTypeError,
    DocCollectionConflictError,
    DocCollectionGlobalError,
    DocCollectionNotDisabledError,
    create_doc_collection,
    delete_doc_collection,
    probe_collection,
    set_collection_enabled,
)

__all__ = [
    "DocCollection",
    "DocCollectionBackendTypeError",
    "DocCollectionConflictError",
    "DocCollectionCreate",
    "DocCollectionCreateResponse",
    "DocCollectionGlobalError",
    "DocCollectionNotDisabledError",
    "DocCollectionNotFoundError",
    "DocCollectionStateError",
    "DocCollectionSummary",
    "create_doc_collection",
    "delete_doc_collection",
    "probe_collection",
    "project_doc_collection",
    "project_doc_collection_create_response",
    "project_doc_collection_to_summary",
    "resolve_doc_collection",
    "set_collection_enabled",
]

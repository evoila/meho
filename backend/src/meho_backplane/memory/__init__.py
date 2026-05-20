# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Server-side memory layer — 5-scope shape over the G0.4 documents table.

G5.1-T1 (#421) of Initiative #332. The memory module exposes a
tenant-scoped :class:`~meho_backplane.memory.service.MemoryService`
that wraps :func:`~meho_backplane.retrieval.indexer.index_document`
and :func:`~meho_backplane.retrieval.retriever.retrieve` for the five
memory scopes consumer-needs.md §G5 defines:

* ``user`` — private to one operator across every tenant they belong to.
* ``user-tenant`` — private to one operator within one tenant.
* ``user-target`` — private to one operator scoped to one target.
* ``tenant`` — shared across every operator in the tenant.
* ``target`` — shared across every operator with access to one target.

RBAC per scope is owned by
:class:`~meho_backplane.memory.rbac.MemoryRbacResolver`. The HTTP
route (T2 #422), MCP meta-tools (T3 #423), and CLI verbs (T4 #424)
build on top of :class:`MemoryService`; the auto-expiry executor
(G5.2 #374) is a separate scope and reuses the ``expires_at``
metadata field this module honours on read.
"""

from meho_backplane.memory.audit import write_internal_audit_row
from meho_backplane.memory.expiry import (
    start_memory_expiry_sweeper,
    stop_memory_expiry_sweeper,
)
from meho_backplane.memory.rbac import MemoryRbacResolver, PermissionDeniedError
from meho_backplane.memory.schemas import (
    MemoryEntry,
    MemoryEntryCreate,
    MemoryEntrySearchHit,
    MemoryScope,
)
from meho_backplane.memory.service import MemoryService

__all__ = [
    "MemoryEntry",
    "MemoryEntryCreate",
    "MemoryEntrySearchHit",
    "MemoryRbacResolver",
    "MemoryScope",
    "MemoryService",
    "PermissionDeniedError",
    "start_memory_expiry_sweeper",
    "stop_memory_expiry_sweeper",
    "write_internal_audit_row",
]

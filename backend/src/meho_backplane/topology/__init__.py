# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Topology graph data layer (Initiative #363, G9.1).

This package owns the per-tenant topology graph. The schema and ORM
models live in :mod:`meho_backplane.db.models` (``GraphNode`` /
``GraphEdge``, migration ``0007``, Task #448).

**Write half — Task #450 (T3):** taking a connector's
:class:`~meho_backplane.connectors.schemas.TopologyHints` snapshot and
reconciling it against the existing ``graph_node`` + ``graph_edge`` rows
for one ``(tenant_id, target_id)`` scope.

* :mod:`meho_backplane.topology.refresh` — :func:`refresh_target_topology`
  resolves the target's connector, calls ``discover_topology``, diffs
  the result against the DB, and applies inserts / updates / soft-deletes
  in one transaction. Emits one synchronous audit row + one fail-open
  broadcast event per refresh.
* :mod:`meho_backplane.topology.scheduler` —
  :func:`start_topology_refresh_scheduler` registers an
  ``asyncio.create_task`` loop in the FastAPI lifespan that walks every
  tenant's targets on a cadence, advisory-locked per ``(tenant, target)``
  so two replicas never stampede the same target.

**Read half — Task #451 (T4):** the three recursive-CTE query verbs
every blast-radius check and topology question goes through.

* :func:`meho_backplane.topology.query.find_dependents`
* :func:`meho_backplane.topology.query.find_dependencies`
* :func:`meho_backplane.topology.query.find_path`
* :class:`meho_backplane.topology.schemas.TopologyNode`
* :class:`meho_backplane.topology.schemas.TopologyPath`

The API (T5), CLI (T6), and MCP (T7) fronts consume :mod:`query` as a
thin shell and never re-derive the traversal or the tenant boundary.

**Resolver — Task #594 (G9.2-T2):** the public name → :class:`GraphNode`
resolver the annotation flow (G9.2 T3 / T4) calls before writing or
reading an edge endpoint. Works for non-target nodes (``target_id IS
NULL``) as well as registered targets.

* :func:`meho_backplane.topology.resolvers.resolve_node`
* :class:`meho_backplane.topology.resolvers.AmbiguousNodeError` —
  re-exported by :mod:`query` for back-compat with pre-G9.2 importers.
* :class:`meho_backplane.topology.resolvers.NodeNotFoundError`

**Annotate / unannotate — Task #595 (G9.2-T3):** the curated-edge
write service. Resolves both endpoints, validates ``kind`` against
:class:`~meho_backplane.db.models.GraphEdgeKind`, runs §6 conflict
detection (sticky ``superseded_by`` for same-kind/different-endpoint
auto edges; bidirectional ``conflicts_with`` for incompatible kinds
over the same endpoint pair), and writes one audit row + one
broadcast event per operation. The REST routes (T5), CLI verbs (T6),
and MCP tools (T7) all funnel through these two primitives.

* :func:`meho_backplane.topology.annotate.annotate_edge`
* :func:`meho_backplane.topology.annotate.unannotate_edge`
* :class:`meho_backplane.topology.annotate.NodeRef`
* :class:`meho_backplane.topology.annotate.InvalidEdgeKindError`
* :class:`meho_backplane.topology.annotate.AutoEdgeDeletionError`
* :class:`meho_backplane.topology.annotate.UnannotateSelectorError`

**Edge listing — Task #596 (G9.2-T4):** the flat tenant-scoped
filter-composable read helper for ``graph_edge`` rows. The T5 REST
route ``GET /api/v1/topology/edges``, the T6 CLI
``meho topology list-edges``, and the T7 MCP
``query_topology(kind='edges')`` facet all dispatch through it rather
than re-deriving the tenant boundary or the filter composition.

* :func:`meho_backplane.topology.query.list_edges`
* :class:`meho_backplane.topology.schemas.TopologyEdge`
* :class:`meho_backplane.topology.schemas.TopologyEdgeEndpoint`

**Bulk import — Task #600 (G9.2-T8, stretch):** the batch curated-
edge writer. The CLI ``meho topology bulk-import <file>`` and the
REST route ``POST /api/v1/topology/edges/bulk`` both call
:func:`bulk_import_edges`, which runs an all-or-nothing transaction
over a list of :class:`BulkImportRow` (per-row idempotent annotate
calls in one transaction) plus a ``--dry-run`` plan-preview path.

* :func:`meho_backplane.topology.bulk_import.bulk_import_edges`
* :class:`meho_backplane.topology.bulk_import.BulkImportRow`
* :class:`meho_backplane.topology.bulk_import.BulkImportResult`
* :class:`meho_backplane.topology.bulk_import.BulkImportValidationError`
"""

from meho_backplane.topology.annotate import (
    AnnotateConflictError,
    AutoEdgeDeletionError,
    InvalidEdgeKindError,
    NodeRef,
    UnannotateSelectorError,
    annotate_edge,
    annotate_edge_in_txn,
    unannotate_edge,
)
from meho_backplane.topology.bulk_import import (
    BulkEdgeAction,
    BulkEdgeResult,
    BulkImportResult,
    BulkImportRow,
    BulkImportRowError,
    BulkImportValidationError,
    bulk_import_edges,
)
from meho_backplane.topology.history_retention import (
    start_topology_history_retention_sweeper,
    stop_topology_history_retention_sweeper,
)
from meho_backplane.topology.nodes import (
    CreateNodeResult,
    InvalidNodeKindError,
    create_or_get_node,
)
from meho_backplane.topology.query import list_edges
from meho_backplane.topology.refresh import RefreshResult, refresh_target_topology
from meho_backplane.topology.resolvers import (
    AmbiguousNodeError,
    NodeNotFoundError,
    resolve_node,
)
from meho_backplane.topology.scheduler import (
    start_topology_refresh_scheduler,
    stop_topology_refresh_scheduler,
)
from meho_backplane.topology.schemas import TopologyEdge, TopologyEdgeEndpoint

__all__ = [
    "AmbiguousNodeError",
    "AnnotateConflictError",
    "AutoEdgeDeletionError",
    "BulkEdgeAction",
    "BulkEdgeResult",
    "BulkImportResult",
    "BulkImportRow",
    "BulkImportRowError",
    "BulkImportValidationError",
    "CreateNodeResult",
    "InvalidEdgeKindError",
    "InvalidNodeKindError",
    "NodeNotFoundError",
    "NodeRef",
    "RefreshResult",
    "TopologyEdge",
    "TopologyEdgeEndpoint",
    "UnannotateSelectorError",
    "annotate_edge",
    "annotate_edge_in_txn",
    "bulk_import_edges",
    "create_or_get_node",
    "list_edges",
    "refresh_target_topology",
    "resolve_node",
    "start_topology_history_retention_sweeper",
    "start_topology_refresh_scheduler",
    "stop_topology_history_retention_sweeper",
    "stop_topology_refresh_scheduler",
    "unannotate_edge",
]

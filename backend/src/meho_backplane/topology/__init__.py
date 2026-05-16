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
"""

from meho_backplane.topology.refresh import RefreshResult, refresh_target_topology
from meho_backplane.topology.scheduler import (
    start_topology_refresh_scheduler,
    stop_topology_refresh_scheduler,
)

__all__ = [
    "RefreshResult",
    "refresh_target_topology",
    "start_topology_refresh_scheduler",
    "stop_topology_refresh_scheduler",
]

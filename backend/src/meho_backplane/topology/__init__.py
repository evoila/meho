# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Topology graph data layer — refresh service + scheduled background task.

Initiative #363 (G9.1), Task #450 (T3). This package owns the **write**
half of the topology graph: taking a connector's
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

Traversal reads (``dependents`` / ``dependencies`` / ``path``) are T4's
``graph.py`` territory and are deliberately absent here.
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

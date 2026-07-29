# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""meho_backplane.connectors.topology — targetless topology write ops (#2537).

The second **synthetic** connector subpackage (the ``secret`` broker was
the first): no vendor connector backs it. Importing the package (the
lifespan's
:func:`~meho_backplane.connectors.registry._eager_import_connectors` pass
walks ``connectors/<product>/`` and imports each subpackage) queues
:func:`~meho_backplane.connectors.topology.ops.register_topology_graph_operations`
onto the lifespan-driven registrar list via
:func:`~meho_backplane.operations.typed_register.register_typed_op_registrar`,
so the three ``topology.*`` ``endpoint_descriptor`` rows land before the
first dispatch.

Like the secret broker, this package calls neither ``register_connector``
nor ``register_connector_v2``: the synthetic ``topology-graph-1.x``
identity has no connector class. The handlers are module-level functions
the dispatcher routes to with ``connector_instance=None`` /
``target=None`` — the graph is tenant-scoped state, not a probed target.

Why these ops exist as dispatchable descriptors at all: routing the MCP
write fronts (``meho.topology.annotate`` / ``.create_node`` /
``.unannotate``) through :func:`~meho_backplane.operations.dispatch`
puts them behind :func:`~meho_backplane.operations._validate.policy_gate`
— the single seam where an AGENT principal's ``caution``-level write
parks as a durable :class:`~meho_backplane.db.models.ApprovalRequest`
(propose-then-approve) while a human ``tenant_admin`` keeps the
default-allow immediate path. The REST and UI fronts are human-only
surfaces and keep calling the service primitives directly.
"""

from meho_backplane.connectors.topology.ops import register_topology_graph_operations
from meho_backplane.operations.typed_register import register_typed_op_registrar

# Queue the topology.* typed-op upserts onto the lifespan-driven
# registrar list (run after the connector eager-import pass).
register_typed_op_registrar(register_topology_graph_operations)

__all__ = [
    "register_topology_graph_operations",
]

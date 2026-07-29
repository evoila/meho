# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho.topology.delete_node`` — guarded hard-delete of a manual seed.

Task #2485 (Initiative #2494, G0.32). The delete counterpart to
:mod:`meho_backplane.mcp.tools.topology_create_node`: seeding a
``graph_node`` had no undo on any surface, so a mis-seeded or
probe-residue manual node persisted indefinitely (refresh reconciliation
only touches nodes adopted onto the refreshed target, and soft-deleted
nodes stay reachable in traversals). This tool removes one manually-
seeded node by id, writing a ``removed`` history tombstone.

The tool is a **thin admin meta-tool** in the ``meho.*`` namespace
(``tenant_admin`` only, ``op_class='write'``). It routes through
:func:`~meho_backplane.operations.dispatch` (op_id ``topology.delete_node``
on the synthetic ``topology-graph-1.x`` connector registered by
:mod:`meho_backplane.connectors.topology.ops`) so the policy gate runs
per call: an AGENT principal's delete parks as a durable approval request
while a human ``tenant_admin`` executes immediately — the exact posture
:mod:`.topology_create_node` uses. The typed-op handler forwards to
:func:`~meho_backplane.topology.node_delete.delete_node`; the service
primitive owns the guards / tombstone / audit / broadcast — this front
performs no DB work of its own.

Separate module for the same reason as ``.create_node``: the
``meho_backplane.mcp.tools.topology`` module is already large, and the
registry auto-discovers every module under ``meho_backplane.mcp.tools``,
so a separate file registers identically without any wiring changes.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.topology.ops import TOPOLOGY_DELETE_NODE_OP_ID
from meho_backplane.connectors.topology.schemas import (
    DELETE_NODE_PARAMETER_SCHEMA,
    DELETE_NODE_RESPONSE_SCHEMA,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.tools.topology import dispatch_topology_write, with_parked_shape

__all__: list[str] = []


_DELETE_NODE_TOOL_NAME: Final[str] = "meho.topology.delete_node"


_DELETE_NODE_DESCRIPTION: Final[str] = (
    "Hard-delete a manually-seeded `graph_node` row by `node_id` "
    "(tenant_admin only), writing a `removed` history tombstone so the "
    "delete stays visible in `query_topology {kind: timeline}`. "
    "Tenant-scoped automatically — no `tenant_id` argument (cross-tenant "
    "deletion is structurally impossible).\n\n"
    "WHEN TO CALL: an operator seeded a node by mistake, or a probe left "
    "residue you seeded by hand that no longer belongs (soft-deleted "
    "nodes stay reachable in traversals, so a manual delete is the only "
    "way to remove one). Get the `node_id` from `query_topology` or from "
    "the `meho.topology.create_node` response.\n\n"
    "REFUSES (returns -32602):\n"
    "  - `probe_owned_node` — the node is probe-derived "
    "(`source='auto'`) or bound to a registered target: refresh "
    "reconciliation owns it and it resurrects on the next probe. Only "
    "manually-seeded nodes (`source='curated'`, target-unbound) are "
    "deletable.\n"
    "  - `node_has_edges` — the node still has live edges. Remove them "
    "first with `meho.topology.unannotate` (the error lists the blocking "
    "edge ids); a bare delete would drop curated edges without their "
    "history tombstones.\n\n"
    "Returns `{node_id, kind, name}` (the pre-delete identity). A second "
    "delete of the same id returns -32602 (node not found).\n\n"
    "AGENT PRINCIPALS: the delete does not execute immediately — it "
    "parks as a durable approval request and the tool returns "
    "`{status: awaiting_approval, approval_request_id, ...}`; a human "
    "operator approves it from the approvals surfaces. Human "
    "tenant_admin calls execute immediately."
)


async def _delete_node_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Route a ``meho.topology.delete_node`` call through the dispatcher (#2485).

    :func:`~meho_backplane.mcp.tools.topology.dispatch_topology_write`
    owns the policy gate (agents park, humans execute), the typed-op
    handler's call into
    :func:`~meho_backplane.topology.node_delete.delete_node`, and the
    result / error mapping back to the MCP wire contract. Tenant scope
    comes from *operator* inside the service — never from *arguments*
    (``additionalProperties: false`` already rejects a smuggled
    ``tenant_id`` at the schema layer).
    """
    return await dispatch_topology_write(operator, TOPOLOGY_DELETE_NODE_OP_ID, arguments)


register_mcp_tool(
    definition=ToolDefinition(
        name=_DELETE_NODE_TOOL_NAME,
        description=_DELETE_NODE_DESCRIPTION,
        inputSchema=DELETE_NODE_PARAMETER_SCHEMA,
        outputSchema=with_parked_shape(DELETE_NODE_RESPONSE_SCHEMA),
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_delete_node_handler,
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho.topology.create_node`` — manual seed for the topology graph.

Initiative #772 (G0.9.1), Task #778 (T6, Signal #14). Closes the
empty-tenant bootstrap gap: a fresh tenant has zero ``graph_node`` rows
(no probe has run yet) and the rest of the topology MCP surface
(``meho.topology.annotate``) requires both endpoints to already exist.
Before this tool, the only way to create a node was the CLI verb
``meho topology refresh <target>``; that is not reachable from an MCP
session, so an agent driving the bootstrap path hit
``-32602 no graph_node matched ...`` with no in-tool remediation.

The tool is a **thin admin meta-tool** in the ``meho.*`` namespace
(``tenant_admin`` only, ``op_class='write'``). Since #2537 it routes
through :func:`~meho_backplane.operations.dispatch` (op_id
``topology.create_node`` on the synthetic ``topology-graph-1.x``
connector registered by :mod:`meho_backplane.connectors.topology.ops`)
so the policy gate runs per call: an AGENT principal's seed parks as a
durable approval request while a human ``tenant_admin`` executes
immediately. The typed-op handler forwards to
:func:`~meho_backplane.topology.nodes.create_or_get_node`; the service
primitive owns validation / upsert / audit / broadcast — this front
performs no DB work of its own. The shape mirrors the
``meho.topology.annotate`` admin tool in
:mod:`meho_backplane.mcp.tools.topology` so an operator carries one
mental model across the topology write surface.

Why a separate module
=====================

``meho_backplane.mcp.tools.topology`` is already four tools and >1100
lines; adding a fifth would push the module further past the
codebase's 600-line per-file guidance. The MCP tool registry
auto-discovers every module under ``meho_backplane.mcp.tools`` (see
``mcp.registry.eager_import_mcp_modules``), so a separate file
registers identically without any wiring changes.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.topology.ops import TOPOLOGY_CREATE_NODE_OP_ID
from meho_backplane.connectors.topology.schemas import (
    CREATE_NODE_PARAMETER_SCHEMA,
    CREATE_NODE_RESPONSE_SCHEMA,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.tools.topology import dispatch_topology_write, with_parked_shape

__all__: list[str] = []


_CREATE_NODE_TOOL_NAME: Final[str] = "meho.topology.create_node"


_CREATE_NODE_DESCRIPTION: Final[str] = (
    "Manually seed a `graph_node` row in the operator's tenant "
    "(tenant_admin only). Idempotent on the `(tenant, kind, name)` "
    "triple: a repeat call refreshes `last_seen` and merges the "
    "manual-seed properties rather than erroring. Tenant-scoped "
    "automatically — no `tenant_id` argument (cross-tenant creation is "
    "structurally impossible).\n\n"
    "WHEN TO CALL: a fresh tenant has zero nodes (no probe has run "
    "yet) and you need to assert a curated edge — `meho.topology."
    "annotate` cannot resolve endpoints that do not yet exist as "
    "`graph_node` rows. Seed both endpoints first, then annotate. "
    "Example bootstrap flow:\n"
    "  1. `meho.topology.create_node {kind: principal, name: "
    "k8s-sa-prod}`\n"
    "  2. `meho.topology.create_node {kind: vault-role, name: "
    "rdc-vault}`\n"
    "  3. `meho.topology.annotate {from_name: k8s-sa-prod, kind: "
    "authenticates-via, to_name: rdc-vault}`\n\n"
    "Also useful for curated inner-graph nodes the probes cannot "
    "infer (Vault roles, Keycloak realms, externally-managed "
    "principals) so a subsequent annotation can reference them.\n\n"
    "DO NOT use to mirror nodes the refresh service already writes — "
    "running `meho topology refresh <target>` is the canonical path "
    "for probe-derivable nodes (`vm`, `pod`, `service`, ...). Manual "
    "seeds for those kinds work (idempotent on the unique index) but "
    "duplicate the refresh service's job; if you find yourself doing "
    "it routinely, run a refresh instead.\n\n"
    "Returns `{node_id, kind, name, was_created}`. `was_created=true` "
    "means a fresh row was inserted; `false` means an existing row "
    "was refreshed (the call is still a success — the bootstrap "
    "precondition is satisfied either way).\n\n"
    "AGENT PRINCIPALS: the seed does not execute immediately — it "
    "parks as a durable approval request and the tool returns "
    "`{status: awaiting_approval, approval_request_id, ...}`; a human "
    "operator approves it from the approvals surfaces. Human "
    "tenant_admin calls execute immediately as before."
)


async def _create_node_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Route a ``meho.topology.create_node`` call through the dispatcher (#2537).

    :func:`~meho_backplane.mcp.tools.topology.dispatch_topology_write`
    owns the policy gate (agents park, humans execute), the typed-op
    handler's call into
    :func:`~meho_backplane.topology.nodes.create_or_get_node`, and the
    result / error mapping back to the MCP wire contract. Tenant scope
    comes from *operator* inside the service — never from *arguments*
    (``additionalProperties: false`` already rejects a smuggled
    ``tenant_id`` at the schema layer).
    """
    return await dispatch_topology_write(operator, TOPOLOGY_CREATE_NODE_OP_ID, arguments)


register_mcp_tool(
    definition=ToolDefinition(
        name=_CREATE_NODE_TOOL_NAME,
        description=_CREATE_NODE_DESCRIPTION,
        inputSchema=CREATE_NODE_PARAMETER_SCHEMA,
        outputSchema=with_parked_shape(CREATE_NODE_RESPONSE_SCHEMA),
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_create_node_handler,
)

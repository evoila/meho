# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_targets_update`` — operator-plane MCP tool to change one target.

The MCP front of the ``targets.update`` operation on the synthetic
``targets-registry-1.x`` connector
(:mod:`meho_backplane.connectors.targets.update`). Like
``meho_targets_register`` it needs ``tenant_admin`` and routes through
:func:`~meho_backplane.mcp.tools.targets_register.dispatch_targets_write`,
so the policy gate, the role floor, audit and broadcast run on every call.
Every call waits until a person with ``tenant_admin`` approves it. The
operation runs the
service of REST ``PATCH /api/v1/targets/{name}``.

``call_operation`` reaches the same operation with the same checks; this
tool only gives it a named entry with its own ``inputSchema``.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.targets.update import (
    TARGETS_UPDATE_OP_ID,
    TARGETS_UPDATE_PARAMETER_SCHEMA,
    TARGETS_UPDATE_RESPONSE_SCHEMA,
)
from meho_backplane.mcp.registry import ToolDefinition, ToolSurface, register_mcp_tool
from meho_backplane.mcp.tools.targets_register import dispatch_targets_write
from meho_backplane.mcp.tools.topology import with_parked_shape

__all__: list[str] = []


_TOOL_NAME: Final[str] = "meho_targets_update"


_DESCRIPTION: Final[str] = (
    "Change fields of one target in the operator's tenant (tenant_admin "
    "only). Runs the same code as REST `PATCH /api/v1/targets/{name}`, so "
    "the product check, the `secret_ref` tenant check, the host check and "
    "the TLS checks all apply. Tenant-scoped automatically.\n\n"
    "WHEN TO CALL: a registered target has a wrong or old field, for "
    "example a wrong `secret_ref`, a new host or a new port.\n\n"
    "Name the target by `target_id`, the UUID `list_targets` returns in "
    "`id`, never by name. Send only the fields to change; `null` clears a "
    "field that can be empty. `name` cannot change (remove and register "
    "again), and `fingerprint` is set by `targets.probe` only.\n\n"
    "Returns `{target_id, name, product, version, host, updated_fields}`. "
    "When you changed the host, port, product or version, run "
    "`targets.probe` next.\n\n"
    "APPROVAL: the change does not run at once. It waits for approval and "
    "the tool returns `{status: awaiting_approval, approval_request_id, "
    "...}`; a person with `tenant_admin` approves it from the approvals "
    "surfaces (`/ui/approvals`, `meho approvals list`), and MEHO then runs "
    "the change with the exact original params."
)


async def _update_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Route a ``meho_targets_update`` call through the dispatcher."""
    return await dispatch_targets_write(operator, TARGETS_UPDATE_OP_ID, arguments)


register_mcp_tool(
    definition=ToolDefinition(
        feature="targets",
        name=_TOOL_NAME,
        surface=ToolSurface.OPERATOR,
        description=_DESCRIPTION,
        inputSchema=TARGETS_UPDATE_PARAMETER_SCHEMA,
        outputSchema=with_parked_shape(TARGETS_UPDATE_RESPONSE_SCHEMA),
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_update_handler,
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho_targets_register`` — admin MCP tool to register a target (#2861).

Closes the agent-surface gap Goal #221 surfaced: targets was the only
CRUD-shaped registry in MEHO with no ``meho_*`` admin write tool
(connectors, sensors, scheduler, agent principals, topology nodes all
have one). An agent onboarding a new system had no in-tool path and fell
back to a raw ``POST /api/v1/targets`` with a hand-minted JWT.

The tool is a **thin admin meta-tool** in the ``meho_*`` namespace
(``tenant_admin`` only, ``op_class='write'``) — explicitly outside
CLAUDE.md's ~17-tool daily agent surface, which is exactly what the
admin namespace holds. It routes through
:func:`~meho_backplane.operations.dispatcher.dispatch` (op_id
``targets.register`` on the synthetic ``targets-registry-1.x`` connector
registered by :mod:`meho_backplane.connectors.targets.ops`) so the policy
gate runs per call: an AGENT principal's registration parks as a durable
approval request while a human ``tenant_admin`` executes immediately. The
typed-op handler forwards to
:func:`~meho_backplane.api.v1.targets.create_target` — the same service
REST calls — so validation is reused, not re-implemented.

The shape mirrors :mod:`meho_backplane.mcp.tools.topology_create_node`:
a separate module (the registry auto-discovers every module under
``meho_backplane.mcp.tools``), a shared parameter/response schema with
the typed op, and the same parked-shape ``outputSchema`` union.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.targets.ops import (
    TARGETS_REGISTER_OP_ID,
    TARGETS_REGISTRY_CONNECTOR_ID,
)
from meho_backplane.connectors.targets.schemas import (
    TARGETS_REGISTER_PARAMETER_SCHEMA,
    TARGETS_REGISTER_RESPONSE_SCHEMA,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInternalError, McpInvalidParamsError
from meho_backplane.mcp.tools.topology import with_parked_shape
from meho_backplane.operations.dispatcher import dispatch

__all__: list[str] = []


_TOOL_NAME: Final[str] = "meho_targets_register"


_DESCRIPTION: Final[str] = (
    "Register a new target in the operator's tenant (tenant_admin only). "
    "Reuses the same service path as REST `POST /api/v1/targets`, so the "
    "product-token check, the `secret_ref` tenant-scope guard, the SSRF "
    "guard on `host`/`fqdn`, and the JWT-derived `tenant_id` all apply "
    "unchanged. Tenant-scoped automatically — no `tenant_id` argument "
    "(cross-tenant registration is structurally impossible).\n\n"
    "WHEN TO CALL: an onboarding flow reaches 'register this target' — an "
    "rke2 cluster, a vCenter, a DNS server — and you need it in the "
    "registry before dispatching against it. Supply `name` + `product` + "
    "`host` (plus any optional connection detail); `product` must be a "
    "registered connector product token (an unknown one is rejected with "
    "the valid set named). This is the in-tool alternative to a raw REST "
    "POST.\n\n"
    "DO NOT use to auto-register a candidate returned by a `discover` "
    "probe — that path is deliberately operator-reviewed (Initiative "
    "#363). This tool is for explicit, named registrations.\n\n"
    "`fingerprint` is server-managed (probe-written) and rejected. "
    "Returns `{target_id, name, product, host, tenant_id}`. After it, "
    "`list_targets` shows the new row in your tenant.\n\n"
    "AGENT PRINCIPALS: the registration does not execute immediately — it "
    "parks as a durable approval request and the tool returns "
    "`{status: awaiting_approval, approval_request_id, ...}`; a human "
    "operator approves it from the approvals surfaces (`/ui/approvals`, "
    "`meho approvals list`, `meho_approvals_*`), which re-executes the "
    "registration with the exact original params. Human tenant_admin "
    "calls execute immediately."
)


#: Handler exceptions the dispatcher wraps as ``connector_error`` that
#: are operator-actionable input problems on this write. The typed-op
#: handler flattens every service/model validation failure
#: (``HTTPException`` / ``ValidationError``) to ``ValueError`` at its
#: boundary, so that is the one class to map back to ``-32602``.
_TARGETS_WRITE_INVALID_PARAM_EXCEPTIONS: Final[frozenset[str]] = frozenset({"ValueError"})


async def dispatch_targets_write(
    operator: Operator,
    op_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Dispatch one gated targets write and translate the result for MCP.

    Routes through :func:`~meho_backplane.operations.dispatcher.dispatch`
    with ``target=None`` (the registry is tenant-scoped state, not a
    probed target) so the policy gate, audit, and broadcast run on the
    same path every other governed op uses. Mirrors
    :func:`~meho_backplane.mcp.tools.topology.dispatch_topology_write`.

    Result translation:

    * ``ok`` — return the typed-op handler's domain payload verbatim.
    * ``awaiting_approval`` — the AGENT-principal park, returned as a
      structured envelope (not an error): the proposal succeeded and the
      approval id is what the agent references next.
    * ``denied`` — policy refusal → ``-32602``.
    * ``error`` with the flattened ``ValueError`` (or a schema
      ``invalid_params``) → ``-32602`` with the service's message.
    * anything else — ``-32603`` (misconfiguration, e.g. the registrar
      never ran).
    """
    result = await dispatch(
        operator=operator,
        connector_id=TARGETS_REGISTRY_CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params=dict(arguments),
    )
    if result.status == "ok":
        if not isinstance(result.result, dict):
            raise McpInternalError(f"{op_id}: unexpected non-object result payload")
        return result.result
    if result.status == "awaiting_approval":
        approval_request_id = str(result.extras.get("approval_request_id"))
        return {
            "status": "awaiting_approval",
            "approval_request_id": approval_request_id,
            "op_id": op_id,
            "message": (
                f"{op_id} parked for approval (request "
                f"{approval_request_id}): the registration executes with "
                "these exact params once a tenant operator approves it via "
                "/ui/approvals, `meho approvals list`, or the "
                "meho_approvals_* MCP tools."
            ),
        }
    if result.status == "denied":
        raise McpInvalidParamsError(f"denied: {result.error or 'policy denied'}")

    exception_class = result.extras.get("exception_class")
    message = str(result.extras.get("exception_message") or result.error or f"{op_id} failed")
    if (
        exception_class in _TARGETS_WRITE_INVALID_PARAM_EXCEPTIONS
        or result.extras.get("error_code") == "invalid_params"
    ):
        raise McpInvalidParamsError(message)
    raise McpInternalError(
        message,
        data={"error_code": result.extras.get("error_code"), "op_id": op_id},
    )


async def _register_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Route a ``meho_targets_register`` call through the dispatcher.

    :func:`dispatch_targets_write` owns the whole translation: policy gate
    (agents park, humans execute), the typed-op handler's call into
    :func:`~meho_backplane.api.v1.targets.create_target`, and the result /
    error mapping back to the MCP wire contract. Tenant scope comes from
    *operator* inside the service — never from *arguments*
    (``additionalProperties: false`` already rejects a smuggled
    ``tenant_id`` at the schema layer).
    """
    return await dispatch_targets_write(operator, TARGETS_REGISTER_OP_ID, arguments)


register_mcp_tool(
    definition=ToolDefinition(
        feature="targets",
        name=_TOOL_NAME,
        description=_DESCRIPTION,
        inputSchema=TARGETS_REGISTER_PARAMETER_SCHEMA,
        outputSchema=with_parked_shape(TARGETS_REGISTER_RESPONSE_SCHEMA),
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_register_handler,
)

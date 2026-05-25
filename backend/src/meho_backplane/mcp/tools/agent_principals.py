# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MCP tools for the agent-principal lifecycle surface.

G11.2-T1 (#815) under Initiative #803. Three ``meho.agent_principals.*``
tools that mirror the REST surface (``/api/v1/agent-principals``):

* ``meho.agent_principals.list`` — list active agent principals for
  the operator's tenant. Role: ``operator``.
* ``meho.agent_principals.register`` — register a new agent principal
  (create Keycloak client + DB row). Role: ``tenant_admin``.
* ``meho.agent_principals.revoke`` — revoke an agent principal (kill
  switch). Role: ``tenant_admin``.

RBAC is enforced at two layers: the registry filter hides write tools
from non-admins in ``tools/list``, and the dispatcher re-checks at
call time.

Error mapping
-------------

* :class:`~meho_backplane.auth.agent_principals.AgentPrincipalExistsError`
  / :class:`~meho_backplane.auth.agent_principals.AgentPrincipalNotFoundError`
  → :class:`~meho_backplane.mcp.server.McpInvalidParamsError` (JSON-RPC
  ``-32602``).
* :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminNotConfiguredError`
  → :class:`~meho_backplane.mcp.server.McpInvalidParamsError` with a
  descriptive message so operators know to configure the admin URL.
* :class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminError`
  → :class:`~meho_backplane.mcp.server.McpInvalidParamsError` with the
  error class name for operator diagnostics.
"""

from __future__ import annotations

from typing import Any, Final

import structlog

from meho_backplane.auth.agent_principals import (
    AgentPrincipalCreate,
    AgentPrincipalExistsError,
    AgentPrincipalNotFoundError,
    AgentPrincipalRead,
    AgentPrincipalService,
)
from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

_log = structlog.get_logger(__name__)

_OP_IDS: Final[dict[str, str]] = {
    "list": "agent_principal.list",
    "register": "agent_principal.register",
    "revoke": "agent_principal.revoke",
}


def _row_to_dict(entry: AgentPrincipalRead) -> dict[str, Any]:
    return entry.model_dump(mode="json")


def _require_name(arguments: dict[str, Any]) -> str:
    name = arguments.get("name")
    if not isinstance(name, str) or not name:
        raise McpInvalidParamsError("name is required and must be a non-empty string")
    return name


# ---------------------------------------------------------------------------
# meho.agent_principals.list
# ---------------------------------------------------------------------------


async def _list_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["list"],
        audit_op_class="read",
    )
    include_revoked: bool = bool(arguments.get("include_revoked", False))
    service = AgentPrincipalService()
    principals = await service.list_(operator.tenant_id, include_revoked=include_revoked)
    return {"principals": [_row_to_dict(p) for p in principals]}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agent_principals.list",
        description=(
            "List agent principals registered for the operator's tenant "
            "(G11.2-T1 #815). Returns {principals: [...]} sorted by name. "
            "Revoked principals are excluded by default; pass "
            "include_revoked=true to include them."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "include_revoked": {
                    "type": "boolean",
                    "description": "Include revoked principals (default false).",
                },
            },
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class="read",
    ),
    handler=_list_handler,
)


# ---------------------------------------------------------------------------
# meho.agent_principals.register
# ---------------------------------------------------------------------------


async def _register_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    name = _require_name(arguments)
    owner_sub: str | None = arguments.get("owner_sub") or None
    if owner_sub is not None and not isinstance(owner_sub, str):
        raise McpInvalidParamsError("owner_sub must be a string when provided")
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["register"],
        audit_op_class="write",
        audit_agent_principal_name=name,
    )
    service = AgentPrincipalService()
    try:
        entry = await service.register(
            tenant_id=operator.tenant_id,
            created_by_sub=operator.sub,
            payload=AgentPrincipalCreate(name=name, owner_sub=owner_sub),
        )
    except AgentPrincipalExistsError as exc:
        raise McpInvalidParamsError(
            f"agent principal {name!r} already exists for this tenant"
        ) from exc
    except KeycloakAdminNotConfiguredError as exc:
        raise McpInvalidParamsError(
            "Keycloak admin is not configured; "
            "set KEYCLOAK_ADMIN_URL, KEYCLOAK_ADMIN_CLIENT_ID, and "
            "KEYCLOAK_ADMIN_CLIENT_SECRET"
        ) from exc
    except KeycloakAdminError as exc:
        raise McpInvalidParamsError(f"Keycloak admin error: {type(exc).__name__}") from exc
    except ValueError as exc:
        raise McpInvalidParamsError(str(exc)) from exc
    return _row_to_dict(entry)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agent_principals.register",
        description=(
            "Register a new agent principal for the operator's tenant "
            "(G11.2-T1 #815). Creates a Keycloak client tagged kind=agent "
            "and inserts a DB row. "
            "Returns the created principal record. "
            "409 when a principal with the same name already exists. "
            "Requires tenant_admin."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Agent identity name (letters, digits, hyphen, "
                        "underscore, dot). The Keycloak clientId will be "
                        "'agent:<name>'."
                    ),
                },
                "owner_sub": {
                    "type": "string",
                    "description": (
                        "OIDC sub of the principal who owns this agent "
                        "(kill-switch owner). Defaults to the caller's sub."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_register_handler,
)


# ---------------------------------------------------------------------------
# meho.agent_principals.revoke
# ---------------------------------------------------------------------------


async def _revoke_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    name = _require_name(arguments)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["revoke"],
        audit_op_class="write",
        audit_agent_principal_name=name,
    )
    service = AgentPrincipalService()
    try:
        entry = await service.revoke(operator.tenant_id, name)
    except AgentPrincipalNotFoundError as exc:
        raise McpInvalidParamsError(
            f"agent principal {name!r} not found or already revoked"
        ) from exc
    except KeycloakAdminNotConfiguredError as exc:
        raise McpInvalidParamsError(
            "Keycloak admin is not configured; "
            "set KEYCLOAK_ADMIN_URL, KEYCLOAK_ADMIN_CLIENT_ID, and "
            "KEYCLOAK_ADMIN_CLIENT_SECRET"
        ) from exc
    except KeycloakAdminError as exc:
        raise McpInvalidParamsError(f"Keycloak admin error: {type(exc).__name__}") from exc
    return _row_to_dict(entry)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.agent_principals.revoke",
        description=(
            "Revoke an agent principal — kill switch (G11.2-T1 #815). "
            "Disables the Keycloak client immediately (no new token grants) "
            "and marks the DB row revoked. "
            "In-flight tokens remain valid until their exp. "
            "Returns the updated principal record. "
            "Requires tenant_admin."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the agent principal to revoke.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_revoke_handler,
)

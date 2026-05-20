# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin MCP tools for the broadcast-override CRUD surface.

G6.3-T5 (#382) — three ``meho.broadcast.overrides.*`` tools that
mirror the T4 (#381) REST surface (``/api/v1/broadcast/overrides``)
onto the MCP transport:

* ``meho.broadcast.overrides.list`` — list the operator's tenant's
  rules. Optional ``op_id_pattern`` arg (exact match).
* ``meho.broadcast.overrides.set`` — create a rule. Args mirror
  T4's :class:`BroadcastOverrideCreate` Pydantic model.
* ``meho.broadcast.overrides.remove`` — delete a rule by id.

All three are ``tenant_admin``-required. RBAC enforcement happens at
two layers: the registry filter (``required_role=TENANT_ADMIN``
hides the tool from ``tools/list`` for non-admins) and the
dispatcher's call-time re-check (``handle_tools_call`` raises
``McpInvalidParamsError`` with ``"forbidden"`` if a non-admin
somehow knows the name). The tools cite Initiative #376 so an
agent calling them can read the broader contract.

In-process call into T4
=======================

Each tool's handler opens a transient :class:`AsyncSession` via
:func:`get_sessionmaker`, calls the matching ``*_impl`` function
T4 exposes
(:func:`~meho_backplane.api.v1.broadcast_overrides.list_overrides_impl`,
:func:`~meho_backplane.api.v1.broadcast_overrides.create_override_impl`,
:func:`~meho_backplane.api.v1.broadcast_overrides.delete_override_impl`),
and translates the result into the MCP wire shape:

* List → ``{"overrides": [<row dict>, ...]}``.
* Set → ``{"override": <row dict>}``.
* Remove → ``{"removed": true}`` (204-equivalent).

:class:`HTTPException` raised by the impl (409 on duplicate, 404 on
delete-not-found) is caught and re-raised as
:class:`McpInvalidParamsError` so the MCP dispatcher emits a
``-32602`` JSON-RPC error with the FastAPI detail string. This is
the conservative mapping — the MCP spec doesn't have a clean
analogue for "conflict" or "not found", and INVALID_PARAMS
("the call's params can't be satisfied") is the closest
spec-blessed shape for both.

Audit + broadcast inheritance
=============================

The ``*_impl`` functions bind the same audit-side contextvars
T4's REST handlers do (``audit_op_id``,
``audit_op_class="write"``, override-diff fragment). The chassis
MCP audit middleware reads these on the response side, so a
mutation MCP call produces an audit row + broadcast event identical
in shape to the REST call's row (modulo ``method="MCP"`` vs
``"POST"``/``"DELETE"`` and the synthetic
``/mcp/tools/call/meho.broadcast.overrides.set`` path).
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from fastapi import HTTPException
from pydantic import ValidationError

from meho_backplane.api.v1.broadcast_overrides import (
    BroadcastOverrideCreate,
    BroadcastOverrideRead,
    create_override_impl,
    delete_override_impl,
    list_overrides_impl,
)
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

_log = structlog.get_logger(__name__)


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Serialise a :class:`BroadcastOverride` ORM row through Pydantic.

    The MCP wire shape is a JSON dict. The Pydantic
    :class:`BroadcastOverrideRead` model is the single source of
    truth for which columns are exposed; using it here keeps the
    MCP surface and the REST surface in lock-step (any future
    response-model change in T4 propagates here automatically).
    """
    return BroadcastOverrideRead.model_validate(row).model_dump(mode="json")


def _http_to_mcp(exc: HTTPException) -> McpInvalidParamsError:
    """Translate a route-side ``HTTPException`` to the MCP wire-error.

    The MCP dispatcher maps :class:`McpInvalidParamsError` to
    JSON-RPC ``-32602`` Invalid Params. 409 / 404 from the impl
    functions both surface here: there is no MCP analogue for
    "conflict" or "not found", but "the call's params can't be
    satisfied" is the closest spec-blessed shape.

    The raw FastAPI ``detail`` token is preserved verbatim --
    REST and MCP callers see the same identifier
    (``broadcast_override_already_exists`` /
    ``broadcast_override_not_found``). The HTTP status code is
    intentionally NOT embedded in the message; the JSON-RPC
    envelope already carries ``-32602`` and the audit row's
    ``status_code`` column records the upstream HTTP code for
    forensics. Pattern matches :mod:`~meho_backplane.mcp.tools.audit`'s
    ``McpInvalidParamsError(str(exc))`` shape.
    """
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return McpInvalidParamsError(detail)


# ---------------------------------------------------------------------------
# meho.broadcast.overrides.list
# ---------------------------------------------------------------------------


async def _list_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    op_id_pattern = arguments.get("op_id_pattern")
    if op_id_pattern is not None and not isinstance(op_id_pattern, str):
        raise McpInvalidParamsError("op_id_pattern must be a string when provided")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        rows = await list_overrides_impl(
            operator=operator,
            session=session,
            op_id_pattern=op_id_pattern,
        )
        return {"overrides": [_row_to_dict(r) for r in rows]}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.broadcast.overrides.list",
        description=(
            "List broadcast-detail override rules for the operator's "
            "tenant (Initiative #376). Tenant-admin only. Optional "
            "op_id_pattern argument filters by exact-match pattern "
            "(not a glob match against an op_id). Returns "
            "{overrides: [row, ...]}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "op_id_pattern": {
                    "type": "string",
                    "maxLength": 128,
                    "description": (
                        "Exact-match filter on the rule's stored "
                        "op_id_pattern. Omit to list every rule."
                    ),
                },
            },
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="read",
    ),
    handler=_list_handler,
)


# ---------------------------------------------------------------------------
# meho.broadcast.overrides.set
# ---------------------------------------------------------------------------


async def _set_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    # Re-validate through Pydantic so the same scope-pair invariant
    # and glob-not-regex blacklist run for MCP as for REST. The
    # inputSchema does first-pass shape checks; Pydantic runs the
    # cross-field validators.
    try:
        payload = BroadcastOverrideCreate.model_validate(arguments)
    except ValidationError as exc:
        raise McpInvalidParamsError(f"invalid arguments: {exc}") from exc
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        try:
            row = await create_override_impl(
                payload=payload,
                operator=operator,
                session=session,
            )
        except HTTPException as exc:
            raise _http_to_mcp(exc) from exc
        return {"override": _row_to_dict(row)}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.broadcast.overrides.set",
        description=(
            "Create a broadcast-detail override rule for the operator's "
            "tenant (Initiative #376). Tenant-admin only. op_id_pattern "
            "accepts globs (* + literals; regex chars are rejected). "
            "scope_field/scope_value must both be set (scoped rule) or "
            "both omitted (op-wide rule). detail is one of "
            "full|aggregate. A duplicate (same pattern + scope triple) "
            "returns an error with detail "
            "'broadcast_override_already_exists'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "op_id_pattern": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "description": (
                        "op_id glob (e.g. 'vault.kv.*' or "
                        "'k8s.configmap.info'). Regex chars rejected."
                    ),
                },
                "scope_field": {
                    "type": ["string", "null"],
                    "enum": ["namespace", "target_name", None],
                    "description": (
                        "Scope key. Null for an op-wide rule; non-null requires scope_value."
                    ),
                },
                "scope_value": {
                    "type": ["string", "null"],
                    "maxLength": 128,
                    "description": (
                        "Scope value (e.g. 'kube-system'). Required when scope_field is non-null."
                    ),
                },
                "detail": {
                    "type": "string",
                    "enum": ["full", "aggregate"],
                    "description": "Override detail.",
                },
            },
            "required": ["op_id_pattern", "detail"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_set_handler,
)


# ---------------------------------------------------------------------------
# meho.broadcast.overrides.remove
# ---------------------------------------------------------------------------


async def _remove_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    raw_id = arguments.get("override_id")
    if not isinstance(raw_id, str):
        raise McpInvalidParamsError("override_id must be a string UUID")
    try:
        override_id = uuid.UUID(raw_id)
    except (TypeError, ValueError) as exc:
        raise McpInvalidParamsError(f"override_id is not a valid UUID: {raw_id!r}") from exc
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session, session.begin():
        try:
            await delete_override_impl(
                override_id=override_id,
                operator=operator,
                session=session,
            )
        except HTTPException as exc:
            raise _http_to_mcp(exc) from exc
        return {"removed": True}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.broadcast.overrides.remove",
        description=(
            "Delete a broadcast-detail override rule by id for the "
            "operator's tenant (Initiative #376). Tenant-admin only. "
            "Returns {removed: true} on success. A 404-equivalent "
            "error (detail 'broadcast_override_not_found') surfaces "
            "both 'id doesn't exist' and 'id belongs to another "
            "tenant' -- existence is not leaked across tenant "
            "boundaries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "override_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "Override row UUID.",
                },
            },
            "required": ["override_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_remove_handler,
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin MCP tools for the Sensor CRUD surface (#2503).

Task #2503 under Initiative #2416 (parent goal #221) -- three
``meho.sensor.*`` tools that mirror the REST surface (``/api/v1/sensors``)
onto the MCP transport:

* ``meho.sensor.list`` -- list the operator's tenant's sensors (with
  optional status / cadence filters). Role: ``operator``.
* ``meho.sensor.create`` -- create a sensor. Role: ``tenant_admin``.
* ``meho.sensor.delete`` -- hard-delete a sensor by id. Role:
  ``tenant_admin``.

Three verbs (rather than one parametric tool) so an MCP client's
``tools/list`` surfaces the available actions discoverably -- the same
rationale :mod:`meho_backplane.mcp.tools.scheduler` documents.

Each handler instantiates the stateless
:class:`~meho_backplane.checks.service.SensorAdminService` and translates
the result into the MCP wire shape. Service-level errors map to
:class:`~meho_backplane.mcp.server.McpInvalidParamsError`: the safe-only
guard surfaces ``sensor_requires_safe_operation``, an unknown op
``sensor_operation_not_found``, a duplicate name ``sensor_name_conflict``,
a cross-tenant create by a non-platform-admin
``cross_tenant_requires_platform_admin`` (the shared
:func:`~meho_backplane.auth.rbac.authorize_tenant_scope` seam the REST
route uses, the #1638 IDOR primitive), and a not-found / cross-tenant
delete target ``sensor_not_found`` -- the same codes the REST route
surfaces. Each handler binds the same audit-side
contextvars the REST handlers do so an MCP call produces an audit row
identical in shape (modulo the ``method="MCP"`` distinction).
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog
from fastapi import HTTPException
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import authorize_tenant_scope
from meho_backplane.checks.schemas import SensorCreate, SensorRead
from meho_backplane.checks.service import (
    SensorAdminService,
    SensorNameConflictError,
    SensorOperationNotFoundError,
    SensorRequiresSafeOperationError,
)
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError

#: Canonical operation identifiers bound into ``audit_op_id`` per tool.
#: The same identifiers the REST routes use so a row's op_id is
#: transport-independent.
_SENSOR_OP_IDS: Final[dict[str, str]] = {
    "list": "sensor.list",
    "create": "sensor.create",
    "delete": "sensor.delete",
}


def _row_to_dict(entry: SensorRead) -> dict[str, Any]:
    """Serialise a :class:`SensorRead` to the MCP wire dict."""
    return entry.model_dump(mode="json")


def _require_sensor_id(arguments: dict[str, Any]) -> uuid.UUID:
    """Extract a required ``sensor_id`` UUID or raise invalid-params."""
    raw = arguments.get("sensor_id")
    if not isinstance(raw, str) or not raw:
        raise McpInvalidParamsError("sensor_id is required and must be a non-empty string")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise McpInvalidParamsError(f"sensor_id is not a valid UUID: {raw!r}") from exc


# ---------------------------------------------------------------------------
# meho.sensor.list
# ---------------------------------------------------------------------------


async def _list_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SENSOR_OP_IDS["list"],
        audit_op_class="read",
    )
    status = arguments.get("status")
    cadence_kind = arguments.get("cadence_kind")
    limit_raw = arguments.get("limit", 100)
    offset_raw = arguments.get("offset", 0)
    # The inputSchema does first-pass shape + bounds; the int() cast is a
    # defensive narrow for the static type-checker.
    limit = int(limit_raw)
    offset = int(offset_raw)
    service = SensorAdminService()
    sensors = await service.list_(
        operator.tenant_id,
        status=status if isinstance(status, str) else None,
        cadence_kind=cadence_kind if isinstance(cadence_kind, str) else None,
        limit=limit,
        offset=offset,
    )
    structlog.contextvars.bind_contextvars(audit_row_count=len(sensors))
    return {"sensors": [_row_to_dict(s) for s in sensors]}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.sensor.list",
        description=(
            "List sensors for the operator's tenant (Initiative #2416). "
            "Operator-level read. Returns {sensors: [sensor, ...]} sorted "
            "newest-first; each sensor carries the latest-result projection "
            "(last_state, last_value, last_evidence, last_evaluated_at, "
            "state_since) so the list is also the status view. Optional "
            "filters: status ('active'|'paused'), cadence_kind "
            "('interval'|'cron'). Tenant-scoped via the JWT; cross-tenant "
            "listing is not exposed on the MCP transport."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "paused"],
                    "description": "Optional status filter.",
                },
                "cadence_kind": {
                    "type": "string",
                    "enum": ["interval", "cron"],
                    "description": "Optional cadence-kind filter.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                    "description": "Max sensors per page. Default 100; max 500.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Rows to skip before the first returned sensor. Default 0.",
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
# meho.sensor.create
# ---------------------------------------------------------------------------


async def _create_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    # Re-validate through Pydantic so the cadence-union check, the cron
    # syntax check, the assertion spec parse, and the size cap all run for
    # MCP as for REST. The inputSchema does first-pass shape checks.
    try:
        payload = SensorCreate.model_validate(arguments)
    except ValidationError as exc:
        raise McpInvalidParamsError(f"invalid arguments: {exc}") from exc
    # Cross-tenant admin: a payload.tenant_id naming a *different* tenant
    # crosses the tenant boundary, which is a platform-level capability --
    # not something tenant-admin *rank* confers. Reuse the REST route's
    # shared authz seam (authorize_tenant_scope, the #1638 cross-tenant
    # IDOR primitive) rather than gating on rank: the tool already requires
    # tenant_admin, so a rank check would be dead code and let any
    # tenant-admin write a sensor into any tenant. authorize_tenant_scope
    # returns the caller's own tenant for the same-tenant / null case and
    # only allows a different tenant for operator.platform_admin.
    try:
        target_tenant = authorize_tenant_scope(operator, payload.tenant_id)
    except HTTPException as exc:
        # 403 cross_tenant_requires_platform_admin -> the MCP wire-error;
        # the same detail token the REST caller sees (there is no MCP
        # analogue for 403, so invalid-params is the closest shape). Mirrors
        # meho_backplane.mcp.tools.broadcast_overrides._http_to_mcp.
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        raise McpInvalidParamsError(detail) from exc
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SENSOR_OP_IDS["create"],
        audit_op_class="write",
        audit_sensor_cadence_kind=payload.cadence_kind.value,
        audit_tenant_scope=("other" if target_tenant != operator.tenant_id else "self"),
    )
    service = SensorAdminService()
    try:
        entry = await service.create(
            tenant_id=target_tenant,
            created_by_sub=operator.sub,
            payload=payload,
        )
    except SensorOperationNotFoundError as exc:
        raise McpInvalidParamsError(exc.error_code) from exc
    except SensorRequiresSafeOperationError as exc:
        raise McpInvalidParamsError(exc.error_code) from exc
    except SensorNameConflictError as exc:
        raise McpInvalidParamsError(exc.error_code) from exc
    structlog.contextvars.bind_contextvars(audit_sensor_id=str(entry.id))
    return {"sensor_id": str(entry.id), "sensor": _row_to_dict(entry)}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.sensor.create",
        description=(
            "Create one sensor under the operator's tenant (Initiative "
            "#2416). Tenant_admin only. A sensor pins an (op + args + "
            "assertion + cadence + severity) tuple the check runner "
            "evaluates on a schedule. Args: name (unique per tenant), "
            "connector_id + op_id (the operation; MUST resolve to a "
            "safety_level='safe' descriptor -- a non-safe or unknown op is "
            "refused with 'sensor_requires_safe_operation' / "
            "'sensor_operation_not_found'), assertion (a bounded "
            "select->compare spec; a bad path or comparator is rejected), "
            "cadence_kind ('interval'|'cron') plus exactly one of "
            "interval_seconds (5..86400) or cron_expr (+ optional timezone). "
            "Optional: target (dispatch target object), params (op params "
            "object), severity ('degraded'|'critical', default 'critical'), "
            "for_seconds (hold-time hysteresis, default 0), identity_sub "
            "(default '__sensor__'), tenant_id (platform-admin-only "
            "cross-tenant target; a non-platform tenant-admin naming "
            "another tenant is refused with "
            "'cross_tenant_requires_platform_admin'). A duplicate name -> "
            "'sensor_name_conflict'. "
            "There is no update/pause path -- status is server-initialized "
            "to 'active' at create (clients cannot supply it) and "
            "runner-parked to 'paused' by #2505. "
            "Response: {sensor_id, sensor: {...}}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1, "maxLength": 128},
                "connector_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "op_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "target": {
                    "type": ["object", "null"],
                    "description": "Optional dispatch target the op is scoped to.",
                },
                "params": {
                    "type": "object",
                    "description": "Op params (default {}).",
                },
                "assertion": {
                    "type": "object",
                    "description": (
                        "Bounded select->compare assertion spec: "
                        "{select: {path, aggregate?}, compare: {type, ...}}."
                    ),
                },
                "cadence_kind": {
                    "type": "string",
                    "enum": ["interval", "cron"],
                },
                "interval_seconds": {
                    "type": ["integer", "null"],
                    "minimum": 5,
                    "maximum": 86400,
                    "description": "Interval in seconds (required when cadence_kind=interval).",
                },
                "cron_expr": {
                    "type": ["string", "null"],
                    "maxLength": 128,
                    "description": "5-field cron expression (required when cadence_kind=cron).",
                },
                "timezone": {
                    "type": "string",
                    "maxLength": 64,
                    "description": "IANA timezone for cron evaluation (default 'UTC').",
                },
                "severity": {
                    "type": "string",
                    "enum": ["degraded", "critical"],
                    "description": "Worst state a failing assertion drives (default critical).",
                },
                "for_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Hold-time hysteresis seconds (default 0).",
                },
                "identity_sub": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "Identity sub the runner dispatches under (default __sensor__).",
                },
                "tenant_id": {
                    "type": ["string", "null"],
                    "format": "uuid",
                    "description": (
                        "Target tenant UUID for cross-tenant admin create "
                        "(platform-admin only, the #1638 cross-tenant "
                        "capability -- a non-platform tenant-admin naming a "
                        "different tenant is refused with "
                        "'cross_tenant_requires_platform_admin'). When omitted "
                        "or null, the sensor is created under the caller's tenant."
                    ),
                },
            },
            "required": ["name", "connector_id", "op_id", "assertion", "cadence_kind"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_create_handler,
)


# ---------------------------------------------------------------------------
# meho.sensor.delete
# ---------------------------------------------------------------------------


async def _delete_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    sensor_id = _require_sensor_id(arguments)
    structlog.contextvars.bind_contextvars(
        audit_op_id=_SENSOR_OP_IDS["delete"],
        audit_op_class="write",
        audit_sensor_id=str(sensor_id),
    )
    service = SensorAdminService()
    deleted = await service.delete(operator.tenant_id, sensor_id)
    if not deleted:
        raise McpInvalidParamsError("sensor_not_found")
    return {"sensor_id": str(sensor_id), "deleted": True}


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.sensor.delete",
        description=(
            "Hard-delete one sensor by id (Initiative #2416). Tenant_admin "
            "only. Removes the row (no tombstone). Cross-tenant / absent id "
            "-> 'sensor_not_found' (existence not leaked across tenants). "
            "Response: {sensor_id, deleted: true}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "sensor_id": {
                    "type": "string",
                    "format": "uuid",
                    "description": "UUID of the sensor to delete.",
                },
            },
            "required": ["sensor_id"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class="write",
    ),
    handler=_delete_handler,
)

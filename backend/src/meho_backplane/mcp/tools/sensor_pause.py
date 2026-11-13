# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Admin MCP tools to pause and resume a Sensor.

* ``meho_sensor_pause`` -- stop the runner's checks of one sensor. Role:
  ``tenant_admin``.
* ``meho_sensor_resume`` -- run it again. Role: ``tenant_admin``.

Both mirror ``PATCH /api/v1/sensors/{id}`` with ``status``. A paused sensor
keeps its id, result history and dashboards. Both are safe to call twice.
An absent or cross-tenant id gives ``sensor_not_found``. As on the REST
route, the audit row carries ``before`` and ``after`` with the status.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

import structlog

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.checks.sensor_pause import set_sensor_status
from meho_backplane.db.models import SensorStatus
from meho_backplane.mcp.registry import (
    ToolDefinition,
    ToolHandler,
    ToolSurface,
    register_mcp_tool,
)
from meho_backplane.mcp.server import McpInvalidParamsError

_OP_IDS: Final[dict[str, str]] = {"pause": "sensor.pause", "resume": "sensor.resume"}


def _require_sensor_id(arguments: dict[str, Any]) -> uuid.UUID:
    raw = arguments.get("sensor_id")
    if not isinstance(raw, str) or not raw:
        raise McpInvalidParamsError("sensor_id is required and must be a non-empty string")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise McpInvalidParamsError(f"sensor_id is not a valid UUID: {raw!r}") from exc


def _status_handler(verb: str, status: SensorStatus) -> ToolHandler:
    async def _handler(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        sensor_id = _require_sensor_id(arguments)
        structlog.contextvars.bind_contextvars(
            audit_op_id=_OP_IDS[verb],
            audit_op_class="write",
            audit_sensor_id=str(sensor_id),
        )
        result = await set_sensor_status(operator.tenant_id, sensor_id, status)
        if result is None:
            raise McpInvalidParamsError("sensor_not_found")
        structlog.contextvars.bind_contextvars(audit_before=result.before, audit_after=result.after)
        return result.sensor.model_dump(mode="json")

    return _handler


def _register_status_tool(verb: str, status: SensorStatus, description: str) -> None:
    register_mcp_tool(
        definition=ToolDefinition(
            feature="sensors",
            name=f"meho_sensor_{verb}",
            surface=ToolSurface.OPERATOR,
            description=description,
            inputSchema={
                "type": "object",
                "properties": {
                    "sensor_id": {
                        "type": "string",
                        "format": "uuid",
                        "description": f"UUID of the sensor to {verb}.",
                    },
                },
                "required": ["sensor_id"],
                "additionalProperties": False,
            },
            required_role=TenantRole.TENANT_ADMIN,
            op_class="write",
        ),
        handler=_status_handler(verb, status),
    )


_register_status_tool(
    "pause",
    SensorStatus.PAUSED,
    (
        "Pause one sensor. Tenant_admin only. A paused sensor is not run, so it "
        "does not call its target. Its id, result history and dashboards stay. "
        "Safe to call twice. Cross-tenant / absent id -> 'sensor_not_found'. "
        "Response: the sensor."
    ),
)
_register_status_tool(
    "resume",
    SensorStatus.ACTIVE,
    (
        "Resume a paused sensor. Tenant_admin only. The sensor runs again at "
        "once. If its target credential is still wrong, it is paused again "
        "after a few refused logins. Safe to call twice. Cross-tenant / absent "
        "id -> 'sensor_not_found'. Response: the sensor."
    ),
)

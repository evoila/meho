# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""winsrv service ops — reads (``list`` / ``get`` safe) + lifecycle writes.

``start`` / ``stop`` / ``restart`` are ``caution`` (recoverable service-state
changes — satellite-executable only through the Stage-3 composed write gate,
per the Initiative #3259 satellite table). Windows services are driven by the
``Microsoft.PowerShell.Management`` cmdlets (``Get-Service`` /
``Start-Service`` / ``Stop-Service`` / ``Restart-Service``) over the shared
PowerShell-over-SSH transport.

PowerShell injection safety
---------------------------

The operator-supplied service ``name`` is interpolated only inside a
single-quoted PowerShell literal via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote` (embedded
``'`` doubled — complete escaping inside a single-quoted PowerShell string).
The transport is ``powershell -EncodedCommand`` (base64 UTF-16LE), so the
shell never parses the script — only the PowerShell parser does.

References
----------

* ``Get-Service`` / ``Start-Service`` / ``Stop-Service`` / ``Restart-Service``:
  https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.management/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.winsrv.ops import SSH_TRANSPORT_NOTE, WinsrvOp, normalise_json_rows

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.winsrv.connector import WinsrvConnector

__all__ = [
    "SERVICE_OPS",
    "winsrv_service_get",
    "winsrv_service_list",
    "winsrv_service_restart",
    "winsrv_service_start",
    "winsrv_service_stop",
]

# The Get-Service projection every read returns — the operator-relevant
# subset. Selected explicitly so the JSON shape stays stable and bounded.
_SERVICE_SELECT: str = "Name, DisplayName, Status, StartType"


async def winsrv_service_list(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.service.list`` — every service (list-shaped).

    Runs ``Get-Service`` and returns ``{rows, total}`` (each row carries
    ``Name`` / ``DisplayName`` / ``Status`` / ``StartType``). Read-only.
    The ``@{ rows; total }`` envelope keeps stdout JSON-shaped even on a
    host with no services (never an empty-stdout ``PwshRunError``).
    """
    del params  # declared empty in schema; intentionally ignored
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$svc = @(Get-Service | Select-Object {_SERVICE_SELECT}); "
        "ConvertTo-Json -Depth 3 -InputObject @{ rows = $svc; total = $svc.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


async def winsrv_service_get(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.service.get`` — one service by name.

    Runs ``Get-Service -Name <name>`` and returns ``{rows, total}`` — a
    missing service is a terminating error under ``'Stop'`` (a real
    ``PwshRunError``), not a false-empty read. ``rows`` normally has one
    element.
    """
    name: str = params["name"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$svc = @(Get-Service -Name {ps_single_quote(name)} "
        f"| Select-Object {_SERVICE_SELECT}); "
        "ConvertTo-Json -Depth 3 -InputObject @{ rows = $svc; total = $svc.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"name": name, "rows": rows, "total": len(rows)}


async def _service_action(
    connector: WinsrvConnector,
    target: Any,
    name: str,
    cmdlet: str,
    action: str,
    operator: Operator | None,
) -> dict[str, Any]:
    """Run a service lifecycle cmdlet and return the post-action status.

    ``cmdlet`` is ``Start-Service`` / ``Stop-Service`` / ``Restart-Service``.
    Under ``$ErrorActionPreference = 'Stop'`` a cmdlet failure (missing
    service, insufficient rights) terminates the pwsh process non-zero →
    :class:`~meho_backplane.connectors._shared.pwsh.PwshRunError`. On success
    the trailing ``Get-Service`` reads back the resulting ``Status``.
    """
    quoted = ps_single_quote(name)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{cmdlet} -Name {quoted}; "
        f"$svc = Get-Service -Name {quoted}; "
        "ConvertTo-Json -Compress -InputObject @{ "
        "ok = $true; name = $svc.Name; status = $svc.Status.ToString() }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    status = payload.get("status") if isinstance(payload, dict) else None
    return {"name": name, "action": action, "status": status, "op_class": "write"}


async def winsrv_service_start(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.service.start`` (caution) — ``Start-Service``."""
    return await _service_action(
        connector, target, params["name"], "Start-Service", "start", operator
    )


async def winsrv_service_stop(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.service.stop`` (caution) — ``Stop-Service``."""
    return await _service_action(
        connector, target, params["name"], "Stop-Service", "stop", operator
    )


async def winsrv_service_restart(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.service.restart`` (caution) — ``Restart-Service``."""
    return await _service_action(
        connector, target, params["name"], "Restart-Service", "restart", operator
    )


_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The Windows service short name (the ``Name`` column, e.g. ``MSSQLSERVER``).",
}

_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": True,
}

_ACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "action": {"type": "string"},
        "status": {"type": ["string", "null"]},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["name", "action", "op_class"],
    "additionalProperties": True,
}


def _action_op(op_id: str, handler_attr: str, verb: str, cmdlet: str) -> WinsrvOp:
    """Build one caution-tier service lifecycle op (start / stop / restart)."""
    return WinsrvOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=f"{verb.capitalize()} a Windows service via ``{cmdlet}`` (caution).",
        description=(
            f"Runs ``{cmdlet} -Name <name>`` on the Windows Server host under "
            "``$ErrorActionPreference = 'Stop'`` and reads back the resulting "
            f"service status. safety_level=caution (a recoverable service-state "
            "change; the production-path gate is policy territory keyed on this "
            "value)."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"name": _NAME_PROP},
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_ACTION_RESPONSE_SCHEMA,
        group_key="services",
        tags=("write", "service", verb),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                f"{verb.capitalize()} a named Windows service (e.g. restart "
                "``MSSQLSERVER`` after a config change). Recoverable; "
                "safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"name": "Required. The service short name."},
            "output_shape": "{'name', 'action', 'status', 'op_class': 'write'}.",
        },
    )


SERVICE_OPS: tuple[WinsrvOp, ...] = (
    WinsrvOp(
        op_id="winsrv.service.list",
        handler_attr="winsrv_service_list",
        summary="List every Windows service via ``Get-Service`` (name/status/start-type).",
        description=(
            "Runs ``Get-Service`` and returns one row per service (Name, "
            "DisplayName, Status, StartType). Read-only; the right op when "
            "the agent needs the service inventory or doesn't yet know a "
            "service's exact short name."
        ),
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="services",
        tags=("read-only", "service", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate Windows services or find a service's short "
                "name before a start/stop/restart. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": ("{'rows': [{Name, DisplayName, Status, StartType}], 'total': <int>}."),
        },
    ),
    WinsrvOp(
        op_id="winsrv.service.get",
        handler_attr="winsrv_service_get",
        summary="Read one Windows service by name via ``Get-Service -Name``.",
        description=(
            "Runs ``Get-Service -Name <name>`` and returns the single "
            "matching service (Name, DisplayName, Status, StartType) as a "
            "``{rows, total}`` envelope. A missing service is a terminating "
            "error, not a false-empty read. Read-only."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"name": _NAME_PROP},
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["name", "rows", "total"],
            "additionalProperties": True,
        },
        group_key="services",
        tags=("read-only", "service", "lookup"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read one named Windows service's current state "
                "(e.g. is ``MSSQLSERVER`` running?). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"name": "Required. The service short name."},
            "output_shape": "{'name', 'rows': [<service object>], 'total': <int>}.",
        },
    ),
    _action_op("winsrv.service.start", "winsrv_service_start", "start", "Start-Service"),
    _action_op("winsrv.service.stop", "winsrv_service_stop", "stop", "Stop-Service"),
    _action_op("winsrv.service.restart", "winsrv_service_restart", "restart", "Restart-Service"),
)

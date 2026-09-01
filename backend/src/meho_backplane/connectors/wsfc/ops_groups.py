# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""wsfc group (clustered-role) ops — list/state (safe) + move/offline/online writes.

A cluster *group* is a clustered role (a SQL Server FCI instance is one group).
Driven by ``Get-ClusterGroup`` / ``Move-ClusterGroup`` / ``Stop-ClusterGroup`` /
``Start-ClusterGroup`` over the shared PowerShell-over-SSH transport.

* ``move`` (``Move-ClusterGroup``) is ``caution`` — a recoverable planned
  failover of a role to another node (the governed failover the acceptance
  criteria want proven on the lab cluster).
* ``offline`` (``Stop-ClusterGroup``) and ``online`` (``Start-ClusterGroup``)
  are ``dangerous`` + ``requires_approval`` (the rke2 mold). Per the #3259
  initiative table, a production-role state change is an outage / data-risk
  event: taking a SQL FCI role offline is a service outage, and forcing one
  online (e.g. to the wrong node, or while it should stay down) can cause
  split-brain / data issues. Both park for a human decision; both are
  satellite-EXCLUDED by the tier ladder.

PowerShell injection safety
---------------------------

Operator-supplied strings (group name / target node) are interpolated only
inside single-quoted PowerShell literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`.

References
----------

* ``Get-ClusterGroup`` / ``Move-ClusterGroup`` / ``Stop-ClusterGroup`` /
  ``Start-ClusterGroup`` (FailoverClusters):
  https://learn.microsoft.com/en-us/powershell/module/failoverclusters/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.wsfc.ops import SSH_TRANSPORT_NOTE, WsfcOp, normalise_json_rows

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.wsfc.connector import WsfcConnector

__all__ = [
    "GROUP_OPS",
    "wsfc_group_list",
    "wsfc_group_move",
    "wsfc_group_offline",
    "wsfc_group_online",
    "wsfc_group_state",
]

_GROUP_SELECT: str = "Name, State, OwnerNode"


async def wsfc_group_list(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.groups.list`` — every clustered role (list-shaped)."""
    del params
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$g = @(Get-ClusterGroup | Select-Object {_GROUP_SELECT}); "
        "ConvertTo-Json -Depth 3 -InputObject @{ rows = $g; total = $g.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


async def wsfc_group_state(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.groups.state`` — one role's state + owner by name.

    Returns a flat dict with the stringified ``state`` (``Online`` / ``Offline``
    / ``Failed`` / ``PartialOnline`` / ``Pending``) and ``owner_node`` — the op
    a Sensor pins to watch a SQL FCI role's ``Online`` state (``$.state``).
    """
    name: str = params["name"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$g = Get-ClusterGroup -Name {ps_single_quote(name)}; "
        'ConvertTo-Json -Compress -InputObject @{ name = "$($g.Name)"; '
        'state = "$($g.State)"; owner_node = "$($g.OwnerNode)" }'
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def wsfc_group_move(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.groups.move`` (caution) — ``Move-ClusterGroup``.

    Fails a role over to another node. Optional ``node`` names the destination
    (omit to let the cluster pick). safety_level=caution — a recoverable
    planned failover.
    """
    name: str = params["name"]
    quoted = ps_single_quote(name)
    clauses = [f"Move-ClusterGroup -Name {quoted}"]
    if params.get("node"):
        clauses.append(f"-Node {ps_single_quote(params['node'])}")
    move_expr = " ".join(clauses)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{move_expr} | Out-Null; "
        f"$g = Get-ClusterGroup -Name {quoted}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true; "
        'name = "$($g.Name)"; state = "$($g.State)"; owner_node = "$($g.OwnerNode)" }'
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {
        "name": name,
        "action": "move",
        "state": data.get("state"),
        "owner_node": data.get("owner_node"),
        "op_class": "write",
    }


async def _group_power(
    connector: WsfcConnector,
    target: Any,
    name: str,
    cmdlet: str,
    action: str,
    operator: Operator | None,
) -> dict[str, Any]:
    """Run ``Stop-ClusterGroup`` / ``Start-ClusterGroup`` and read back the state."""
    quoted = ps_single_quote(name)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{cmdlet} -Name {quoted} | Out-Null; "
        f"$g = Get-ClusterGroup -Name {quoted}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true; "
        'name = "$($g.Name)"; state = "$($g.State)" }'
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {"name": name, "action": action, "state": data.get("state"), "op_class": "write"}


async def wsfc_group_offline(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.groups.offline`` (dangerous, resume path only) — ``Stop-ClusterGroup``."""
    return await _group_power(
        connector, target, params["name"], "Stop-ClusterGroup", "offline", operator
    )


async def wsfc_group_online(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.groups.online`` (dangerous, resume path only) — ``Start-ClusterGroup``."""
    return await _group_power(
        connector, target, params["name"], "Start-ClusterGroup", "online", operator
    )


_GROUP_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The clustered role / group name (e.g. ``SQL Server (MSSQLSERVER)``).",
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

_GROUP_ACTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "action": {"type": "string"},
        "state": {"type": ["string", "null"]},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["name", "action", "op_class"],
    "additionalProperties": True,
}


def _power_op(op_id: str, handler_attr: str, verb: str, cmdlet: str) -> WsfcOp:
    """Build one dangerous+approval group power op (offline / online)."""
    return WsfcOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=f"Take a clustered role {verb} via ``{cmdlet}`` (dangerous, approval-gated).",
        description=(
            f"Runs ``{cmdlet} -Name <name>`` and reads back the role's state. "
            "safety_level=dangerous, requires_approval=True — a production-role "
            "state change is an outage / data-risk event (offline is a service "
            "outage; forcing online can cause split-brain), so a dispatch parks "
            "for a human decision. For a planned node-to-node failover use the "
            "``caution`` ``wsfc.groups.move`` instead."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"name": _GROUP_NAME_PROP},
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_GROUP_ACTION_RESPONSE_SCHEMA,
        group_key="groups",
        tags=("write", "group", verb),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                f"Take a clustered role {verb}. Destructive to the running "
                "service; approval-gated — parks for a human decision first. "
                "For a planned failover use ``wsfc.groups.move``. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"name": "Required. The clustered role / group name."},
            "output_shape": f"{{'name', 'action': '{verb}', 'state', 'op_class': 'write'}}.",
        },
    )


GROUP_OPS: tuple[WsfcOp, ...] = (
    WsfcOp(
        op_id="wsfc.groups.list",
        handler_attr="wsfc_group_list",
        summary="List clustered roles / groups via ``Get-ClusterGroup`` (name/state/owner).",
        description=(
            "Runs ``Get-ClusterGroup`` and returns one row per clustered role "
            "(Name, State, OwnerNode). Read-only; the ``{rows, total}`` "
            "envelope is JSONFlux-reduced for a large cluster."
        ),
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="groups",
        tags=("read-only", "group", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate the cluster's roles and their Online/Offline "
                "state and current owner node (e.g. find the SQL FCI role's "
                "exact name). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": "{'rows': [{Name, State, OwnerNode}], 'total': <int>}.",
        },
    ),
    WsfcOp(
        op_id="wsfc.groups.state",
        handler_attr="wsfc_group_state",
        summary="Read one clustered role's state + owner via ``Get-ClusterGroup -Name``.",
        description=(
            "Runs ``Get-ClusterGroup -Name <name>`` and returns a flat dict "
            "with the stringified ``state`` (``Online`` / ``Offline`` / "
            "``Failed`` / ``PartialOnline`` / ``Pending``) and ``owner_node``. "
            "Read-only — the op a Sensor pins to watch a SQL FCI role's "
            "``Online`` state (``$.state``)."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"name": _GROUP_NAME_PROP},
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
                "state": {"type": ["string", "null"]},
                "owner_node": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="groups",
        tags=("read-only", "group", "lookup"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read one named role's state and owner (e.g. is the "
                "SQL FCI role Online and on which node?). Read-only; the "
                "sensor op for role health. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"name": "Required. The clustered role / group name."},
            "output_shape": "{'name', 'state', 'owner_node'}.",
        },
    ),
    WsfcOp(
        op_id="wsfc.groups.move",
        handler_attr="wsfc_group_move",
        summary="Fail a clustered role over to another node via ``Move-ClusterGroup`` (caution).",
        description=(
            "Runs ``Move-ClusterGroup -Name <name>`` (optionally ``-Node "
            "<target>``) to fail a role over to another node, then reads back "
            "the resulting state + owner. safety_level=caution — a recoverable "
            "planned failover (the governed failover path). For a hard "
            "offline/online use the dangerous ``wsfc.groups.offline`` / "
            "``wsfc.groups.online``."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _GROUP_NAME_PROP,
                "node": {
                    "type": "string",
                    "description": "Optional destination node; omit to let the cluster pick.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "action": {"type": "string"},
                "state": {"type": ["string", "null"]},
                "owner_node": {"type": ["string", "null"]},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["name", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="groups",
        tags=("write", "group", "move"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Fail a clustered role over to another node (planned "
                "failover / rebalance). Recoverable; safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The role / group to move.",
                "node": "Optional destination node.",
            },
            "output_shape": "{'name', 'action': 'move', 'state', 'owner_node', 'op_class'}.",
        },
    ),
    _power_op("wsfc.groups.offline", "wsfc_group_offline", "offline", "Stop-ClusterGroup"),
    _power_op("wsfc.groups.online", "wsfc_group_online", "online", "Start-ClusterGroup"),
)

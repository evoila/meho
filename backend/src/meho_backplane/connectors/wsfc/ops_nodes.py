# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""wsfc node ops — ``list`` / ``state`` (safe) + ``pause`` / ``resume`` (caution) + ``evict``.

Nodes are driven by the ``FailoverClusters`` cmdlets ``Get-ClusterNode`` /
``Suspend-ClusterNode`` / ``Resume-ClusterNode`` / ``Remove-ClusterNode`` over
the shared PowerShell-over-SSH transport.

* ``pause`` (``Suspend-ClusterNode``) and ``resume`` (``Resume-ClusterNode``)
  are ``caution`` — a recoverable maintenance action (drain roles off a node,
  then bring it back). ``pause -Drain`` moves the node's roles to other nodes
  first, so it is safe on a running cluster.
* ``evict`` (``Remove-ClusterNode -Force``) is ``dangerous`` +
  ``requires_approval`` (the rke2 mold): removing a node from cluster
  membership is irreversible (re-joining is a full add-node flow), so a
  dispatch parks for a human decision. Per the #3259 satellite table a
  destructive op is satellite-EXCLUDED by the tier ladder.

PowerShell injection safety
---------------------------

Operator-supplied strings (node name / target node) are interpolated only
inside single-quoted PowerShell literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`; the
``-Failback`` value is validated against a bounded enum before interpolation.

References
----------

* ``Get-ClusterNode`` / ``Suspend-ClusterNode`` / ``Resume-ClusterNode`` /
  ``Remove-ClusterNode`` (FailoverClusters):
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
    "NODE_OPS",
    "wsfc_node_evict",
    "wsfc_node_list",
    "wsfc_node_pause",
    "wsfc_node_resume",
    "wsfc_node_state",
]

#: The Get-ClusterNode projection. ``State`` is rendered as a string via the
#: ``ConvertTo-Json`` of the enum; the count-level rollup in ops_cluster keys
#: on the same stringified value.
_NODE_SELECT: str = "Name, State, NodeWeight, DynamicWeight, Id"

#: ``Resume-ClusterNode -Failback`` accepted values (confirmed from the Learn
#: "Accepted values" table): ``NoFailback`` / ``Immediate`` / ``Policy``.
_FAILBACK_VALUES: frozenset[str] = frozenset({"NoFailback", "Immediate", "Policy"})


async def wsfc_node_list(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.nodes.list`` — every cluster node (list-shaped)."""
    del params
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$n = @(Get-ClusterNode | Select-Object {_NODE_SELECT}); "
        "ConvertTo-Json -Depth 3 -InputObject @{ rows = $n; total = $n.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


async def wsfc_node_state(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.nodes.state`` — one node's state by name.

    Returns a flat dict with the stringified ``state`` so a Sensor can pin a
    per-node assertion (e.g. ``$.state`` equals ``Up``).
    """
    name: str = params["name"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$n = Get-ClusterNode -Name {ps_single_quote(name)}; "
        'ConvertTo-Json -Compress -InputObject @{ name = "$($n.Name)"; '
        'state = "$($n.State)"; node_weight = $n.NodeWeight; '
        'dynamic_weight = $n.DynamicWeight; id = "$($n.Id)" }'
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def wsfc_node_pause(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.nodes.pause`` (caution) — ``Suspend-ClusterNode``.

    Suspends (pauses) the node; ``drain=true`` (the default) moves its roles to
    other nodes first (``-Drain``), the safe maintenance path on a running
    cluster. An optional ``target_node`` names where to drain the roles — it
    requires ``drain=true`` because ``Suspend-ClusterNode`` only honours
    ``-TargetNode`` alongside ``-Drain``, so a ``target_node`` supplied with
    ``drain=false`` is rejected fail-closed rather than silently dropped.
    """
    name: str = params["name"]
    quoted = ps_single_quote(name)
    drain = params.get("drain", True)
    target_node = params.get("target_node")
    if target_node and not drain:
        raise ValueError(
            "target_node requires drain=true; Suspend-ClusterNode only honors "
            "-TargetNode with -Drain"
        )
    clauses = [f"Suspend-ClusterNode -Name {quoted}"]
    if drain:
        clauses.append("-Drain")
        if target_node:
            clauses.append(f"-TargetNode {ps_single_quote(target_node)}")
    suspend_expr = " ".join(clauses)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{suspend_expr} | Out-Null; "
        f"$n = Get-ClusterNode -Name {quoted}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true; "
        'name = "$($n.Name)"; state = "$($n.State)" }'
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {"name": name, "action": "pause", "state": data.get("state"), "op_class": "write"}


async def wsfc_node_resume(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.nodes.resume`` (caution) — ``Resume-ClusterNode``.

    Resumes a paused node. Optional ``failback`` (``NoFailback`` / ``Immediate``
    / ``Policy``) controls whether roles drained off at pause come back.
    """
    name: str = params["name"]
    quoted = ps_single_quote(name)
    clauses = [f"Resume-ClusterNode -Name {quoted}"]
    failback = params.get("failback")
    if failback is not None:
        if failback not in _FAILBACK_VALUES:
            raise ValueError(
                f"failback must be one of {sorted(_FAILBACK_VALUES)}; got {failback!r}"
            )
        clauses.append(f"-Failback {failback}")
    resume_expr = " ".join(clauses)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"{resume_expr} | Out-Null; "
        f"$n = Get-ClusterNode -Name {quoted}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true; "
        'name = "$($n.Name)"; state = "$($n.State)" }'
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {"name": name, "action": "resume", "state": data.get("state"), "op_class": "write"}


async def wsfc_node_evict(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.nodes.evict`` (dangerous, resume path only) — ``Remove-ClusterNode``.

    Runs ``Remove-ClusterNode -Name <name> -Force``. Irreversible (re-joining
    is a full add-node flow) — the op is ``requires_approval`` so a dispatch
    parks for a human decision before the node is evicted.
    """
    name: str = params["name"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Remove-ClusterNode -Name {ps_single_quote(name)} -Force; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {"name": name, "action": "evict", "op_class": "write"}


_NODE_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The cluster node name (the ``Name`` column, e.g. ``SQL01``).",
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

_NODE_ACTION_RESPONSE_SCHEMA: dict[str, Any] = {
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


NODE_OPS: tuple[WsfcOp, ...] = (
    WsfcOp(
        op_id="wsfc.nodes.list",
        handler_attr="wsfc_node_list",
        summary="List cluster nodes via ``Get-ClusterNode`` (name/state/weight).",
        description=(
            "Runs ``Get-ClusterNode`` and returns one row per node (Name, "
            "State, NodeWeight, DynamicWeight, Id). Read-only; the "
            "``{rows, total}`` envelope is JSONFlux-reduced for a large "
            "cluster."
        ),
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="nodes",
        tags=("read-only", "node", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate the cluster's nodes and their Up/Down state "
                "and quorum weight. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": "{'rows': [{Name, State, NodeWeight, ...}], 'total': <int>}.",
        },
    ),
    WsfcOp(
        op_id="wsfc.nodes.state",
        handler_attr="wsfc_node_state",
        summary="Read one cluster node's state via ``Get-ClusterNode -Name``.",
        description=(
            "Runs ``Get-ClusterNode -Name <name>`` and returns a flat dict "
            "with the node's stringified ``state`` (``Up`` / ``Down`` / "
            "``Paused`` / ``Joining``), quorum weight, and id. Read-only — a "
            "Sensor can pin a per-node ``$.state`` assertion."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"name": _NODE_NAME_PROP},
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
                "state": {"type": ["string", "null"]},
                "node_weight": {"type": ["integer", "null"]},
                "dynamic_weight": {"type": ["integer", "null"]},
                "id": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="nodes",
        tags=("read-only", "node", "lookup"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read one named node's current state (e.g. is ``SQL02`` "
                "Up?). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"name": "Required. The cluster node name."},
            "output_shape": "{'name', 'state', 'node_weight', 'dynamic_weight', 'id'}.",
        },
    ),
    WsfcOp(
        op_id="wsfc.nodes.pause",
        handler_attr="wsfc_node_pause",
        summary="Pause/drain a cluster node via ``Suspend-ClusterNode`` (caution).",
        description=(
            "Runs ``Suspend-ClusterNode -Name <name>``; ``drain`` (default "
            "true) adds ``-Drain`` so the node's roles move to other nodes "
            "first (the safe maintenance path), optionally to ``target_node``. "
            "safety_level=caution — a recoverable maintenance state change "
            "(resume brings the node back)."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _NODE_NAME_PROP,
                "drain": {
                    "type": "boolean",
                    "description": "Drain roles off the node first (``-Drain``). Default true.",
                },
                "target_node": {
                    "type": "string",
                    "description": (
                        "Optional node to drain the roles to (``-TargetNode``); "
                        "requires ``drain=true`` (rejected otherwise)."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_NODE_ACTION_RESPONSE_SCHEMA,
        group_key="nodes",
        tags=("write", "node", "pause"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Pause a node for maintenance (e.g. before patching). Drains "
                "roles to other nodes by default. Recoverable; "
                "safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The node to pause.",
                "drain": "Optional bool; default true (move roles off first).",
            },
            "output_shape": "{'name', 'action': 'pause', 'state', 'op_class': 'write'}.",
        },
    ),
    WsfcOp(
        op_id="wsfc.nodes.resume",
        handler_attr="wsfc_node_resume",
        summary="Resume a paused cluster node via ``Resume-ClusterNode`` (caution).",
        description=(
            "Runs ``Resume-ClusterNode -Name <name>``. Optional ``failback`` "
            "(``NoFailback`` / ``Immediate`` / ``Policy``) controls whether "
            "roles drained off at pause return. safety_level=caution — a "
            "recoverable state change."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": _NODE_NAME_PROP,
                "failback": {
                    "type": "string",
                    "enum": sorted(_FAILBACK_VALUES),
                    "description": "Optional ``-Failback`` mode for drained roles.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_NODE_ACTION_RESPONSE_SCHEMA,
        group_key="nodes",
        tags=("write", "node", "resume"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Bring a paused node back into the cluster (e.g. after "
                "patching). Recoverable; safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "name": "Required. The paused node to resume.",
                "failback": "Optional; NoFailback / Immediate / Policy.",
            },
            "output_shape": "{'name', 'action': 'resume', 'state', 'op_class': 'write'}.",
        },
    ),
    WsfcOp(
        op_id="wsfc.nodes.evict",
        handler_attr="wsfc_node_evict",
        summary="Evict a node from the cluster via ``Remove-ClusterNode`` (dangerous).",
        description=(
            "Runs ``Remove-ClusterNode -Name <name> -Force``. Irreversible — "
            "the node leaves cluster membership and re-joining is a full "
            "add-node flow. safety_level=dangerous, requires_approval=True: a "
            "dispatch parks for a human to approve before the node is evicted."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"name": _NODE_NAME_PROP},
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "action": {"type": "string"},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["name", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="nodes",
        tags=("write", "node", "evict"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Remove a node from cluster membership. Irreversible; "
                "approval-gated — parks for a human decision first. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"name": "Required. The node to evict."},
            "output_shape": "{'name', 'action': 'evict', 'op_class': 'write'}.",
        },
    ),
)

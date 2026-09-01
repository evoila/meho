# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""wsfc resource ops — ``list`` / ``dependency-report`` (both safe, list-shaped).

Cluster resources are driven by ``Get-ClusterResource`` and
``Get-ClusterResourceDependency`` over the shared PowerShell-over-SSH
transport. Both ops are read-only and list-shaped, so the dispatcher's JSONFlux
reducer spills a large set (a busy cluster can carry dozens of resources) to a
``result_query`` handle.

Both scripts are constants (no operator input), so there is no injection
surface. ``State`` / ``OwnerGroup`` / ``ResourceType`` are projected through
``"$( ... )"`` string interpolation rather than a bare ``Select-Object`` so the
JSON carries flat strings, not the nested ``Microsoft.FailoverClusters``
objects those properties otherwise serialise to.

References
----------

* ``Get-ClusterResource`` / ``Get-ClusterResourceDependency``
  (FailoverClusters):
  https://learn.microsoft.com/en-us/powershell/module/failoverclusters/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import pwsh_run
from meho_backplane.connectors.wsfc.ops import SSH_TRANSPORT_NOTE, WsfcOp, normalise_json_rows

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.wsfc.connector import WsfcConnector

__all__ = [
    "RESOURCE_OPS",
    "wsfc_resource_dependency_report",
    "wsfc_resource_list",
]

_RESOURCE_LIST_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$r = @(Get-ClusterResource | ForEach-Object { [pscustomobject]@{ "
    'Name = "$($_.Name)"; State = "$($_.State)"; '
    'OwnerGroup = "$($_.OwnerGroup)"; ResourceType = "$($_.ResourceType)" } }); '
    "ConvertTo-Json -Depth 3 -InputObject @{ rows = $r; total = $r.Count }"
)

_DEPENDENCY_REPORT_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$r = @(Get-ClusterResource | ForEach-Object { "
    "$dep = Get-ClusterResourceDependency -Resource $_.Name; "
    '[pscustomobject]@{ resource = "$($_.Name)"; owner_group = "$($_.OwnerGroup)"; '
    'dependency_expression = "$($dep.DependencyExpression)" } }); '
    "ConvertTo-Json -Depth 3 -InputObject @{ rows = $r; total = $r.Count }"
)


async def wsfc_resource_list(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.resources.list`` — every cluster resource (list-shaped)."""
    del params
    payload = await pwsh_run(connector, target, _RESOURCE_LIST_SCRIPT, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


async def wsfc_resource_dependency_report(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.resources.dependency-report`` — per-resource dependency expressions.

    Runs ``Get-ClusterResourceDependency`` per resource and returns
    ``{rows, total}`` (each row: ``resource`` / ``owner_group`` /
    ``dependency_expression``). List-shaped → JSONFlux-reduced.
    """
    del params
    payload = await pwsh_run(connector, target, _DEPENDENCY_REPORT_SCRIPT, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": True,
}


RESOURCE_OPS: tuple[WsfcOp, ...] = (
    WsfcOp(
        op_id="wsfc.resources.list",
        handler_attr="wsfc_resource_list",
        summary="List cluster resources via ``Get-ClusterResource`` (name/state/owner/type).",
        description=(
            "Runs ``Get-ClusterResource`` and returns one row per resource "
            "(Name, State, OwnerGroup, ResourceType). Read-only; the "
            "``{rows, total}`` envelope is JSONFlux-reduced for a large "
            "cluster."
        ),
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="resources",
        tags=("read-only", "resource", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to enumerate the cluster's resources and their state / "
                "owning group / type (IP address, network name, disk, SQL "
                "Server, ...). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": "{'rows': [{Name, State, OwnerGroup, ResourceType}], 'total': <int>}.",
        },
    ),
    WsfcOp(
        op_id="wsfc.resources.dependency-report",
        handler_attr="wsfc_resource_dependency_report",
        summary="Per-resource dependency expressions via ``Get-ClusterResourceDependency``.",
        description=(
            "Runs ``Get-ClusterResourceDependency`` for every cluster resource "
            "and returns one row per resource (``resource`` / ``owner_group`` / "
            "``dependency_expression``). Read-only; the ``{rows, total}`` "
            "envelope is JSONFlux-reduced. The dependency expression shows what "
            "must come online before a resource (e.g. a SQL FCI's network name "
            "depends on its IP and disk resources)."
        ),
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="resources",
        tags=("read-only", "resource", "dependency"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to see the cluster resources' dependency wiring — what "
                "must be online before each resource. Read-only; the right op "
                "to understand a SQL FCI's resource ordering. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'rows': [{resource, owner_group, dependency_expression}], 'total': <int>}."
            ),
        },
    ),
)

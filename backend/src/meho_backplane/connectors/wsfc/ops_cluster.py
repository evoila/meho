# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""wsfc cluster ops — ``get`` / ``quorum`` / ``validation-report`` (safe) + ``test`` (caution).

The cluster group is driven by ``Get-Cluster`` / ``Get-ClusterNode`` /
``Get-ClusterGroup`` / ``Get-ClusterResource`` / ``Get-ClusterQuorum`` and the
long-running ``Test-Cluster`` over the shared PowerShell-over-SSH transport.

* ``wsfc.cluster.get`` — the health rollup (node / group / resource up-counts
  in one round-trip). This is the **sensor workhorse**: it returns flat scalar
  fields (``nodes_up`` / ``groups_failed`` / ...) a bounded assertion can pin,
  plus the raw per-state count maps for observability.
* ``wsfc.cluster.quorum`` — the quorum *model* (``Get-ClusterQuorum``).
* ``wsfc.cluster.validation-report`` — enumerate the stored validation reports
  ``Test-Cluster`` has written (list-shaped → JSONFlux). Reading a specific
  report's per-test pass/fail is a follow-up (the report is an ``.mht`` on
  disk); this op lists what is available and when it was run.
* ``wsfc.cluster.test`` — RUN ``Test-Cluster`` (``caution``). See the
  duration note below.

Duration semantics for ``wsfc.cluster.test``
--------------------------------------------

``Test-Cluster`` runs the full validation suite and can take **minutes** on a
real cluster, so the handler forwards a raised ``timeout`` (:data:`_TEST_TIMEOUT`)
to :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run` — far above the
transport's 30 s default. On a running cluster the disruptive storage tests
(which need the disks offline) are skipped automatically for online disks;
scope the run with ``include`` to keep it short. The op is ``caution``, not
``safe``, precisely because it is a real, resource-touching validation run (and
because a full run is long enough to be worth an explicit operator decision).

PowerShell injection safety
---------------------------

``wsfc.cluster.test``'s optional ``include`` / ``report_name`` are the only
operator strings in this module; each is interpolated only inside a
single-quoted PowerShell literal via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`. Every other
script here is a constant (no injection surface).

References
----------

* ``Get-Cluster`` / ``Get-ClusterNode`` / ``Get-ClusterGroup`` /
  ``Get-ClusterResource`` / ``Get-ClusterQuorum`` / ``Test-Cluster``
  (FailoverClusters, Windows Server 2022 / PowerShell 5.1):
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
    "CLUSTER_OPS",
    "wsfc_cluster_get",
    "wsfc_cluster_quorum",
    "wsfc_cluster_test",
    "wsfc_cluster_validation_report",
]


#: Test-Cluster wall-clock budget (seconds). Test-Cluster runs the full
#: validation suite and can take minutes; the transport default (30 s) would
#: truncate the read and report a false failure on a validation that in fact
#: completed. 15 minutes is a generous ceiling for a small cluster.
_TEST_TIMEOUT: float = 900.0

#: The cluster health rollup script. One round-trip: reads the cluster, its
#: nodes, groups, and resources, and computes per-state count maps plus the
#: common health scalars. Every hashtable value is a plain PowerShell
#: *expression* (a ``@(... | Where-Object {...}).Count`` array-count or a
#: precomputed ``$*bs`` map variable) — no inline function calls as hashtable
#: values, which keeps the one-liner unambiguous to parse (the whole script is
#: assembled and executed remotely, never run through a local interpreter, so
#: the construct must be plainly correct by inspection). The
#: ``"$($_.State)" -eq 'Up'`` form compares the stringified state
#: (case-insensitive in PowerShell), so a rendering / casing difference in the
#: ``ClusterNodeState`` / ``ClusterGroupState`` / ``ClusterResourceState`` enum
#: cannot silently zero a count; the ``$*bs`` maps carry every state verbatim so
#: a state the scalars don't name still surfaces. ``$m[$k] = 1 + $m[$k]``
#: relies on ``1 + $null == 1`` for a first-seen key.
_CLUSTER_GET_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$c = Get-Cluster; "
    "$nodes = @(Get-ClusterNode); "
    "$groups = @(Get-ClusterGroup); "
    "$res = @(Get-ClusterResource); "
    '$nbs = @{}; foreach ($i in $nodes) { $k = "$($i.State)"; $nbs[$k] = 1 + $nbs[$k] }; '
    '$gbs = @{}; foreach ($i in $groups) { $k = "$($i.State)"; $gbs[$k] = 1 + $gbs[$k] }; '
    '$rbs = @{}; foreach ($i in $res) { $k = "$($i.State)"; $rbs[$k] = 1 + $rbs[$k] }; '
    "ConvertTo-Json -Depth 4 -Compress -InputObject @{ "
    'name = "$($c.Name)"; '
    "nodes_total = $nodes.Count; "
    "nodes_up = @($nodes | Where-Object { \"$($_.State)\" -eq 'Up' }).Count; "
    "nodes_down = @($nodes | Where-Object { \"$($_.State)\" -eq 'Down' }).Count; "
    "nodes_paused = @($nodes | Where-Object { \"$($_.State)\" -eq 'Paused' }).Count; "
    "nodes_by_state = $nbs; "
    "groups_total = $groups.Count; "
    "groups_online = @($groups | Where-Object { \"$($_.State)\" -eq 'Online' }).Count; "
    "groups_offline = @($groups | Where-Object { \"$($_.State)\" -eq 'Offline' }).Count; "
    "groups_failed = @($groups | Where-Object { \"$($_.State)\" -eq 'Failed' }).Count; "
    "groups_by_state = $gbs; "
    "resources_total = $res.Count; "
    "resources_online = @($res | Where-Object { \"$($_.State)\" -eq 'Online' }).Count; "
    "resources_failed = @($res | Where-Object { \"$($_.State)\" -eq 'Failed' }).Count; "
    "resources_by_state = $rbs }"
)

#: Get-ClusterQuorum → the quorum model + witness resource name. Constant.
_CLUSTER_QUORUM_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$q = Get-ClusterQuorum; "
    "ConvertTo-Json -Compress -InputObject @{ "
    'cluster = "$($q.Cluster)"; '
    'quorum_type = "$($q.QuorumType)"; '
    'quorum_resource = if ($q.QuorumResource) { "$($q.QuorumResource)" } else { $null } }'
)

#: Enumerate the validation reports Test-Cluster has written. The report
#: directory is the well-known ``%SystemRoot%\Cluster\Reports``; the script
#: builds the path from ``$env:SystemRoot`` so it is not hardcoded, and returns
#: the ``{rows, total}`` envelope (JSONFlux-reduced) even when the directory is
#: absent. Constant script (no operator input).
_VALIDATION_REPORT_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$dir = Join-Path $env:SystemRoot 'Cluster\\Reports'; "
    "$rows = @(); "
    "if (Test-Path $dir) { $rows = @(Get-ChildItem -Path $dir -File -ErrorAction SilentlyContinue "
    "| Where-Object { $_.Extension -in '.mht','.htm','.html' } "
    "| Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 50 "
    "| ForEach-Object { [pscustomobject]@{ name = $_.Name; path = $_.FullName; "
    "created = $_.LastWriteTimeUtc.ToString('o'); size_bytes = [int64]$_.Length } }) }; "
    "ConvertTo-Json -Depth 3 -InputObject @{ rows = $rows; total = $rows.Count }"
)


async def wsfc_cluster_get(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.cluster.get`` — the cluster health rollup.

    Returns flat scalar health fields (``nodes_up`` / ``groups_online`` /
    ``resources_failed`` / ...) plus the raw per-state count maps. Small flat
    dict, so it is never JSONFlux-reduced — the shape a Sensor can pin a
    bounded assertion on (e.g. ``$.nodes_up``).
    """
    del params  # declared empty in schema; intentionally ignored
    payload = await pwsh_run(connector, target, _CLUSTER_GET_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def wsfc_cluster_quorum(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.cluster.quorum`` — the quorum model + witness resource."""
    del params
    payload = await pwsh_run(connector, target, _CLUSTER_QUORUM_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def wsfc_cluster_validation_report(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.cluster.validation-report`` — list the stored reports.

    Returns ``{rows, total}`` (each row: ``name`` / ``path`` / ``created`` /
    ``size_bytes``), most-recent first. List-shaped, so the dispatcher's
    JSONFlux reducer spills a large set to a ``result_query`` handle.
    """
    del params
    payload = await pwsh_run(connector, target, _VALIDATION_REPORT_SCRIPT, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


async def wsfc_cluster_test(
    connector: WsfcConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``wsfc.cluster.test`` (caution) — RUN ``Test-Cluster``.

    Long-running (minutes); the handler forwards a raised
    :data:`_TEST_TIMEOUT`. Optional ``include`` scopes the run to named test
    categories; ``report_name`` names the generated report. Returns the
    generated report path (from the ``Test-Cluster`` FileInfo output).
    """
    include = params.get("include")
    include_clause = ""
    if include:
        if not isinstance(include, list) or not all(isinstance(i, str) and i for i in include):
            raise ValueError("include must be a list of non-empty test-category strings")
        quoted = ", ".join(ps_single_quote(i) for i in include)
        include_clause = f" -Include {quoted}"
    report_name = params.get("report_name")
    report_clause = ""
    if report_name:
        if not isinstance(report_name, str):
            raise ValueError("report_name must be a string")
        report_clause = f" -ReportName {ps_single_quote(report_name)}"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$r = Test-Cluster{include_clause}{report_clause} -Confirm:$false; "
        "$fi = $r | Where-Object { $_ -is [System.IO.FileInfo] } | Select-Object -First 1; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true; "
        "report_path = if ($fi) { $fi.FullName } else { $null } }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator, timeout=_TEST_TIMEOUT)
    data = payload if isinstance(payload, dict) else {}
    return {
        "action": "test",
        "report_path": data.get("report_path"),
        "op_class": "write",
    }


_EMPTY_PARAMS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}

_ROLLUP_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": ["string", "null"]},
        "nodes_total": {"type": "integer"},
        "nodes_up": {"type": "integer"},
        "nodes_down": {"type": "integer"},
        "nodes_paused": {"type": "integer"},
        "nodes_by_state": {"type": "object"},
        "groups_total": {"type": "integer"},
        "groups_online": {"type": "integer"},
        "groups_offline": {"type": "integer"},
        "groups_failed": {"type": "integer"},
        "groups_by_state": {"type": "object"},
        "resources_total": {"type": "integer"},
        "resources_online": {"type": "integer"},
        "resources_failed": {"type": "integer"},
        "resources_by_state": {"type": "object"},
    },
    "additionalProperties": True,
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


CLUSTER_OPS: tuple[WsfcOp, ...] = (
    WsfcOp(
        op_id="wsfc.cluster.get",
        handler_attr="wsfc_cluster_get",
        summary="Cluster health rollup: node/group/resource up-counts in one round-trip.",
        description=(
            "Reads ``Get-Cluster`` + ``Get-ClusterNode`` + ``Get-ClusterGroup`` "
            "+ ``Get-ClusterResource`` and returns the cluster name plus the "
            "per-state counts: ``nodes_up`` / ``nodes_down`` / ``nodes_total`` "
            "(and ``nodes_by_state``), the same for groups (roles) and "
            "resources. Read-only. A small flat dict — the right op for a "
            "Sensor to pin a bounded assertion on (e.g. ``$.nodes_up`` or "
            "``$.groups_failed``)."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_ROLLUP_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=("read-only", "cluster", "health"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call for a one-shot cluster health snapshot — how many nodes "
                "are Up, how many roles are Online, whether any group / "
                "resource is Failed. Read-only; the sensor workhorse op. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "Flat dict: {name, nodes_total, nodes_up, nodes_down, "
                "nodes_paused, nodes_by_state, groups_*, resources_*}."
            ),
        },
    ),
    WsfcOp(
        op_id="wsfc.cluster.quorum",
        handler_attr="wsfc_cluster_quorum",
        summary="Read the cluster quorum model via ``Get-ClusterQuorum``.",
        description=(
            "Runs ``Get-ClusterQuorum`` and returns the quorum ``quorum_type`` "
            "(``NodeMajority`` / ``NodeAndDiskMajority`` / "
            "``NodeAndFileShareMajority`` / ``DiskOnly``) and the witness "
            "``quorum_resource`` name (``null`` for node-majority). Read-only. "
            "For the witness resource's *online state*, use ``wsfc.witness.get``."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema={
            "type": "object",
            "properties": {
                "cluster": {"type": ["string", "null"]},
                "quorum_type": {"type": ["string", "null"]},
                "quorum_resource": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="cluster",
        tags=("read-only", "cluster", "quorum"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to read the cluster's quorum configuration — which "
                "quorum model is in effect and which resource is the witness. "
                "Read-only. For witness online state use ``wsfc.witness.get``. "
                + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": "{'cluster', 'quorum_type', 'quorum_resource'}.",
        },
    ),
    WsfcOp(
        op_id="wsfc.cluster.validation-report",
        handler_attr="wsfc_cluster_validation_report",
        summary="List the stored cluster validation reports (list-shaped, JSONFlux-reduced).",
        description=(
            "Enumerates the validation reports ``Test-Cluster`` has written "
            "under ``%SystemRoot%\\Cluster\\Reports`` and returns one row per "
            "report (``name`` / ``path`` / ``created`` / ``size_bytes``), "
            "most-recent first. Read-only; the ``{rows, total}`` envelope is "
            "JSONFlux-reduced to a handle for a large set. Reading a specific "
            "report's per-test pass/fail is a follow-up (the report is an "
            "``.mht`` on disk)."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_LIST_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=("read-only", "cluster", "validation"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to see which cluster validation reports exist and when "
                "each was run (e.g. after ``wsfc.cluster.test``). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": "{'rows': [{name, path, created, size_bytes}], 'total': <int>}.",
        },
    ),
    WsfcOp(
        op_id="wsfc.cluster.test",
        handler_attr="wsfc_cluster_test",
        summary="Run cluster validation via ``Test-Cluster`` (caution; LONG-running).",
        description=(
            "Runs ``Test-Cluster`` against the current cluster and returns the "
            "generated report path. safety_level=caution — this is a real "
            "validation RUN, and it is LONG-running (minutes): the handler "
            "raises the transport timeout to 15 minutes. On a running cluster "
            "the disruptive storage tests (which need the disks offline) are "
            "skipped automatically for online disks. Scope the run with "
            "``include`` (a list of test categories) to keep it short."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "include": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "description": (
                        "Optional list of ``Test-Cluster -Include`` test "
                        "categories (e.g. ``Inventory`` / ``Network`` / "
                        "``System Configuration``). Omit to run the full suite."
                    ),
                },
                "report_name": {
                    "type": "string",
                    "description": "Optional ``-ReportName`` for the generated report.",
                },
            },
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "report_path": {"type": ["string", "null"]},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["action", "op_class"],
            "additionalProperties": True,
        },
        group_key="cluster",
        tags=("write", "cluster", "validation", "long-running"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Run cluster validation (e.g. before adding a node or after a "
                "config change). LONG-running (minutes); safety_level=caution. "
                "Scope with ``include`` to shorten it. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "include": "Optional list of test categories to scope the run.",
                "report_name": "Optional report name.",
            },
            "output_shape": "{'action': 'test', 'report_path', 'op_class': 'write'}.",
        },
    ),
)

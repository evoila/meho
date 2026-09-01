# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`WsfcConnector`.

The connector manages **Windows Server Failover Clustering** — cluster / node /
group (clustered-role) / resource / quorum state, cluster validation, and
guarded role-move / failover writes — over SSH → PowerShell (``powershell``
running the Windows PowerShell 5.1 ``FailoverClusters`` module cmdlets), routed
through :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run` (the shared
PowerShell-over-SSH transport hoisted by #3260). Structurally it is a copy of
the winsrv estate mold (identity canary + per-domain op groups on the
``SshConnector`` base) and adopts the rke2 approval-parked-write mold for its
destructive ops.

The **target is any single cluster node**: the ``FailoverClusters`` cmdlets
fan out cluster-wide from whichever node runs them (they talk to the local
Cluster Service, which is the cluster-wide database), so one node is a
sufficient control point for the whole cluster. There is no per-node target
fan-out to arrange.

Op surface (19 ops across five groups; safety tier per the Initiative #3259
satellite table — reads ``safe``, recoverable writes ``caution``, destructive
``dangerous`` + ``requires_approval``):

* ``wsfc.about``                     [safe]      identity canary (node OS + cluster membership).
* ``wsfc.cluster.get``               [safe]      cluster health rollup (node/group counts).
* ``wsfc.cluster.quorum``            [safe]      Get-ClusterQuorum (quorum model + witness).
* ``wsfc.cluster.validation-report`` [safe]      list stored validation reports (→ JSONFlux).
* ``wsfc.cluster.test``              [caution]   Test-Cluster (LONG-running validation).
* ``wsfc.nodes.list``                [safe]      Get-ClusterNode (list-shaped → JSONFlux).
* ``wsfc.nodes.state``               [safe]      Get-ClusterNode -Name (one node).
* ``wsfc.nodes.pause``               [caution]   Suspend-ClusterNode (drain).
* ``wsfc.nodes.resume``              [caution]   Resume-ClusterNode.
* ``wsfc.nodes.evict``               [dangerous+approval]  Remove-ClusterNode -Force.
* ``wsfc.groups.list``               [safe]      Get-ClusterGroup (list-shaped → JSONFlux).
* ``wsfc.groups.state``              [safe]      Get-ClusterGroup -Name (one role).
* ``wsfc.groups.move``               [caution]   Move-ClusterGroup (governed failover).
* ``wsfc.groups.offline``            [dangerous+approval]  Stop-ClusterGroup.
* ``wsfc.groups.online``             [dangerous+approval]  Start-ClusterGroup.
* ``wsfc.resources.list``            [safe]      Get-ClusterResource (list-shaped → JSONFlux).
* ``wsfc.resources.dependency-report`` [safe]    per-resource dependency expressions (→ JSONFlux).
* ``wsfc.witness.get``               [safe]      quorum witness resource + online state.
* ``wsfc.witness.set``               [caution]   Set-ClusterQuorum (reconfigure the witness).

The dataclass + tuple shape mirrors
:mod:`~meho_backplane.connectors.winsrv.ops` so the registration walk reads
identically across SSH-transport connectors. The composition pattern
(``_wsfc_ops()`` importing per-domain op tuples) mirrors winsrv's ``ops.py``.

Grounding note — enum strings and output property names
-------------------------------------------------------

The ``FailoverClusters`` cmdlet reference pages on Microsoft Learn document
parameters and the output *.NET type name* but do **not** tabulate the output
objects' property names or the enum member values. The property names this
module reads (``State`` / ``OwnerNode`` / ``OwnerGroup`` / ``NodeWeight`` /
``DynamicWeight`` / ``Id``) and the state strings the health rollup compares
against (``Up`` / ``Down`` / ``Online`` / ``Offline`` / ``Failed``) are the
canonical ``Microsoft.FailoverClusters.PowerShell.ClusterNodeState`` /
``ClusterGroupState`` / ``ClusterResourceState`` members. To keep the rollup
robust against a rendering / casing difference, the count scripts compare the
**stringified** state (``"$($_.State)" -eq 'Up'`` — case-insensitive in
PowerShell) rather than the raw enum, and ``wsfc.cluster.get`` additionally
returns the raw per-state count maps, so a state the hardcoded scalars don't
name still surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "SSH_TRANSPORT_NOTE",
    "WSFC_OPS",
    "WSFC_WHEN_TO_USE_BY_GROUP",
    "WsfcOp",
    "normalise_json_rows",
]


#: Curated ``when_to_use`` strings per group key, consumed by
#: :meth:`WsfcConnector.register_operations` (imported into the connector, the
#: rke2 / winsrv precedent — the blurbs live with the op metadata, not the
#: transport class). Each entry covers a ``group_key`` declared across the wsfc
#: op tuples; the registration walk fails closed with a :class:`ValueError` if
#: a ``group_key`` lacks a curated entry.
WSFC_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "cluster": (
        "Use for cluster-wide facts and health before drilling into nodes / "
        "roles / resources: identity (``wsfc.about``), the health rollup "
        "(``wsfc.cluster.get`` — node/group/resource up-counts, the sensor "
        "workhorse), the quorum model (``wsfc.cluster.quorum``), and the "
        "stored validation reports (``wsfc.cluster.validation-report``). All "
        "read-only. ``wsfc.cluster.test`` RUNS a validation pass "
        "(``caution`` — LONG-running, minutes). Call ``wsfc.about`` first to "
        "confirm the target node is a live cluster member."
    ),
    "nodes": (
        "Use to inventory and control cluster nodes: list all "
        "(``wsfc.nodes.list``) or read one (``wsfc.nodes.state``) — both "
        "read-only — pause/drain (``wsfc.nodes.pause``) and resume "
        "(``wsfc.nodes.resume``) a node for maintenance (``caution``), and "
        "evict a node from cluster membership (``wsfc.nodes.evict`` — "
        "``dangerous`` + approval, irreversible). Pause drains roles off a "
        "node before patching; resume brings it back into rotation."
    ),
    "groups": (
        "Use to inventory and control clustered roles / groups (a SQL FCI "
        "instance is a group): list all (``wsfc.groups.list``) or read one "
        "(``wsfc.groups.state`` — the op a Sensor pins to watch a role's "
        "``Online`` state) — both read-only — move / fail a role over to "
        "another node (``wsfc.groups.move`` — ``caution``, the governed "
        "planned-failover path), and take a role offline / bring it online "
        "(``wsfc.groups.offline`` / ``wsfc.groups.online`` — ``dangerous`` + "
        "approval: a production-role state change is an outage / data-risk "
        "event)."
    ),
    "resources": (
        "Use to inventory cluster resources and their dependency wiring: list "
        "all resources with their state / owning group / type "
        "(``wsfc.resources.list``) and read the per-resource dependency "
        "expressions (``wsfc.resources.dependency-report``). Both read-only. "
        "The dependency report is how you see what must come online before a "
        "SQL FCI's network name / IP / disk resources."
    ),
    "witness": (
        "Use to read or reconfigure the cluster quorum witness: read the "
        "witness resource and its online state (``wsfc.witness.get`` — the op "
        "a Sensor pins to watch witness reachability, load-bearing on a "
        "2-node cluster) and reconfigure it (``wsfc.witness.set`` — "
        "``caution``: a disk or file-share witness, via ``Set-ClusterQuorum``; "
        "no cloud witness because its access key cannot ride the pwsh "
        "transport)."
    ),
}


#: Canonical SSH-only / pwsh transport reminder copied into every op's
#: ``llm_instructions``. Mirrors winsrv's ``SSH_TRANSPORT_NOTE`` — the
#: agent-facing descriptions must call out the PowerShell-over-SSH transport so
#: an LLM doesn't compose against a non-existent REST surface (Windows Server
#: Failover Clustering has no unified REST API).
SSH_TRANSPORT_NOTE: str = (
    "Windows Server Failover Clustering has no unified REST API; the "
    "underlying transport is PowerShell-over-SSH (powershell -EncodedCommand "
    "routed through asyncssh) driving the FailoverClusters module cmdlets on "
    "a cluster node, which operate cluster-wide from that one node."
)


@dataclass(frozen=True)
class WsfcOp:
    """Metadata for one wsfc op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod can splat
    the dataclass into the helper without per-op boilerplate. ``handler_attr``
    is the attribute name on
    :class:`~meho_backplane.connectors.wsfc.connector.WsfcConnector` that
    exposes the async handler; the connector resolves the bound method against
    itself at registration time so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler` walk
    recovers the callable from the persisted ``module.ClassName.method`` path.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


def normalise_json_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalise a ``ConvertTo-Json`` payload into a list of row dicts.

    PowerShell's ``ConvertTo-Json`` renders a **single**-element result as a
    flat object and a **multi**-element result as a JSON array; a zero-element
    result renders as ``null`` (an empty stdout is caught upstream by
    :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run` before it reaches
    here). This helper collapses all three shapes to a ``list[dict]`` so the
    list handlers walk a uniform structure — the winsrv ``normalise_json_rows``
    shape, shared across every wsfc list op so the ``{rows, total}`` envelope
    the JSONFlux reducer keys on is built identically.
    """
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


#: The identity canary op. ``wsfc.about`` is the operator-facing wrapper around
#: :meth:`WsfcConnector.fingerprint` — same vendor / product / node / cluster
#: payload, surfaced through the typed-op dispatcher so callers see the standard
#: :class:`OperationResult` envelope instead of the raw
#: :class:`FingerprintResult`. Mirrors winsrv's ``winsrv.about``.
_WSFC_ABOUT_OP = WsfcOp(
    op_id="wsfc.about",
    handler_attr="about",
    summary="Identify the cluster node + its cluster membership (OS + cluster name).",
    description=(
        "Connects to the cluster node over SSH and runs a single "
        "``powershell -EncodedCommand`` script that reads the machine "
        "hostname, the OS version / build, the Windows PowerShell version, "
        "whether the ``FailoverClusters`` module is present, and — when the "
        "node is a cluster member — the cluster name and functional level. "
        "Returns a flat dict with the vendor (``microsoft``), product "
        "(``windows-failover-cluster``), the OS version + build, the host "
        "name, and the cluster membership. Use to confirm the target is a "
        "reachable, clustered node before issuing higher-level cluster / node "
        "/ group ops; no params; safe on any healthy target."
    ),
    parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
    response_schema={
        "type": "object",
        "properties": {
            "vendor": {"type": "string"},
            "product": {"type": "string"},
            "version": {"type": ["string", "null"]},
            "build": {"type": ["string", "null"]},
            "hostname": {"type": ["string", "null"]},
            "cluster_name": {"type": ["string", "null"]},
            "cluster_functional_level": {"type": ["integer", "null"]},
            "failover_clusters_module": {"type": ["boolean", "null"]},
            "powershell_version": {"type": ["string", "null"]},
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="cluster",
    tags=("read-only", "identity", "wsfc"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the cluster node behind "
            "a target — its OS + whether it is a live cluster member and which "
            "cluster — or to confirm the node is reachable via SSH + "
            "PowerShell before issuing higher-level cluster ops. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; ``cluster_name`` is the cluster the node belongs to "
            "(``null`` when the node is not clustered), "
            "``failover_clusters_module`` whether the module is installed, "
            "``version`` / ``build`` the node OS."
        ),
    },
)


def _wsfc_ops() -> tuple[WsfcOp, ...]:
    """Return the merged registration tuple.

    Composition: ``wsfc.about`` (identity canary) + the per-domain op tuples
    (cluster / nodes / groups / resources / witness). Implemented as a function
    call rather than a literal-and-splat at module level so the import order
    stays linear: ``ops.py`` defines :class:`WsfcOp` + ``_WSFC_ABOUT_OP`` +
    :func:`normalise_json_rows`, then imports the per-domain op tuples from
    their modules (each depends only on this module plus its own helpers).
    Mirrors :func:`meho_backplane.connectors.winsrv.ops._winsrv_ops`.
    """
    from meho_backplane.connectors.wsfc.ops_cluster import CLUSTER_OPS
    from meho_backplane.connectors.wsfc.ops_groups import GROUP_OPS
    from meho_backplane.connectors.wsfc.ops_nodes import NODE_OPS
    from meho_backplane.connectors.wsfc.ops_resources import RESOURCE_OPS
    from meho_backplane.connectors.wsfc.ops_witness import WITNESS_OPS

    return (
        _WSFC_ABOUT_OP,
        *CLUSTER_OPS,
        *NODE_OPS,
        *GROUP_OPS,
        *RESOURCE_OPS,
        *WITNESS_OPS,
    )


#: The ops :class:`WsfcConnector` registers at lifespan startup. The shape of
#: each follow-on op is "import a new module-level tuple and splat it into
#: :data:`WSFC_OPS` via :func:`_wsfc_ops`" — the registration walk in
#: :meth:`WsfcConnector.register_operations` does not change.
WSFC_OPS: tuple[WsfcOp, ...] = _wsfc_ops()

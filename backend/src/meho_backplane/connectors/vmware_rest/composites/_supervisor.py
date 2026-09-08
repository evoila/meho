# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Namespace-management ``vmware.composite.supervisor.*`` handlers (#3281).

Governed Supervisor (Workload Control Plane / WCP) lifecycle on the
``vmware_rest`` connector. Before this module the connector had
namespace-management **reads** only; enabling / disabling a Supervisor ran
out-of-band (``govc`` / ``kubectl-vsphere``) and escaped the backplane's
policy / audit / approval path. The three composites here close that gap:

* ``vmware.composite.supervisor.enable`` --
  ``POST /vcenter/namespace-management/supervisors/{cluster}?action=enable_on_compute_cluster``
  (the current single-compute-cluster path; the ``clusters/{cluster}?action=enable``
  form is **deprecated as of vSphere 9.0** and is not used). Takes the
  nested ``EnableOnComputeClusterSpec`` body (``name`` / ``control_plane`` /
  ``workloads`` / optional ``zone``). ``safety_level="dangerous"`` +
  ``requires_approval=True``.
* ``vmware.composite.supervisor.disable`` --
  ``POST /vcenter/namespace-management/clusters/{cluster}?action=disable``
  (``DELETE clusters/{cluster}`` 404s -- the action form is the documented
  teardown). No request body. ``safety_level="dangerous"`` +
  ``requires_approval=True``.
* ``vmware.composite.supervisor.status`` --
  ``GET /vcenter/namespace-management/clusters/{cluster}`` -->
  ``config_status`` (``CONFIGURING`` -> ``RUNNING`` / ``ERROR``) +
  ``kubernetes_status`` (``READY`` / ``WARNING`` / ``ERROR``) + messages.
  Shaped so a runbook ``OperationCallVerify`` step or a Sensor assertion
  can poll ``config_status == "RUNNING"`` / ``ready == true`` inline (the
  scalars stay top-level, never buried in a set-shaped handle).
  ``safety_level="safe"`` + ``requires_approval=False``.

Generic-vs-typed
----------------

The three namespace-management paths are present in the pinned
``vcenter.yaml`` and land as ingested ``endpoint_descriptor`` rows, and
``vmware_rest`` is a **real** connector (it overrides
:meth:`~...connector.VmwareRestConnector.mount_op_path`), so an unstaged
generic ingested row dispatches through ``httpx`` -- it is not an
``unreplaced_auto_shim`` dead end. Generic dispatch is therefore
*mechanically* possible. It is nonetheless insufficient for a governed
enable: the body is a deeply nested, provider-discriminated spec
(``workloads.network.network_type`` x ``workloads.edge.provider``), the
enable is asynchronous (returns the Supervisor id immediately; readiness
is a separate 30-60 min poll), and the task requires the composite to
**refuse an unknown network provider loudly** -- none of which a raw
pass-through row can do. So enable / disable are typed composites that
own the spec validation + the readiness contract, and ``status`` is the
poll op they hand the runbook / Sensor. This matches the #3281 scope
("typed composite -- expected outcome").

Async readiness (non-blocking)
------------------------------

``enable`` deliberately does **not** block the dispatcher polling for the
30-60 min ``CONFIGURING`` -> ``RUNNING`` transition. It returns
``status="enabling"`` with the new Supervisor id and points the caller at
``vmware.composite.supervisor.status``; a runbook step / Sensor drives the
poll on its own cadence.

Governance seam
---------------

The two writes ride the same #2254 governed REST sub-op seam the other
REST write composites use
(:func:`~meho_backplane.connectors.vmware_rest.composites._write._write_sub_op`
-> :func:`~meho_backplane.operations.composite.enforce_subop_policy` ->
``_post_json``): the top-level composite's own ``dangerous`` /
``requires_approval=True`` posture parks for human approval at dispatch,
and the governed child POST re-applies policy (allowlist / grant) under
the shared ``dangerous`` / ``requires_approval=False`` sub-op posture. The
``status`` read rides the un-gated read sub-op seam
(:func:`~...composites._write._read_sub_op`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._write import (
    _read_sub_op,
    _unwrap_value,
    _write_sub_op,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "supervisor_disable_composite",
    "supervisor_enable_composite",
    "supervisor_status_composite",
]


# Canonical ``METHOD:/path`` op_ids -- byte-for-byte the strings the ingest
# parser emits from the pinned ``vcenter.yaml`` (the ``?action=<verb>`` query
# rides on the path key, and the ``{cluster}`` path var is a
# ClusterComputeResource moid). These are the governance op_ids fed to
# ``enforce_subop_policy`` via ``_write_sub_op`` and the read seam via
# ``_read_sub_op``; the spec-shelf reconcile lane
# (``tests/acceptance/test_supervisor_op_id_reconcile.py``) asserts each
# resolves against the canonical pinned spec.
_OP_ENABLE_ON_COMPUTE_CLUSTER: Final = (
    "POST:/vcenter/namespace-management/supervisors/{cluster}?action=enable_on_compute_cluster"
)
_OP_DISABLE: Final = "POST:/vcenter/namespace-management/clusters/{cluster}?action=disable"
_OP_GET_CLUSTER: Final = "GET:/vcenter/namespace-management/clusters/{cluster}"

#: REST governed-child manifests (parallel to ``_write._SUB_OPS_*``). Referenced
#: by :mod:`._governed_subops` so the discovery surface publishes each write
#: composite's grant set, and asserted by the reconcile lane.
_SUB_OPS_SUPERVISOR_ENABLE: tuple[str, ...] = (_OP_ENABLE_ON_COMPUTE_CLUSTER,)
_SUB_OPS_SUPERVISOR_DISABLE: tuple[str, ...] = (_OP_DISABLE,)

#: Authoritative 9.x ``Supervisors.Networks.Workload.NetworkType`` enum -- the
#: workload network stack. VDS + Foundation LB is ``VSPHERE``; the NSX VPC
#: model is ``NSX_VPC``; classic NSX-T is ``NSXT``.
_ALLOWED_NETWORK_TYPES: Final[frozenset[str]] = frozenset({"VSPHERE", "NSXT", "NSX_VPC"})

#: Authoritative 9.x ``Networks.Edges.EdgeProvider`` enum -- who provides edge
#: (load-balancer) services. VDS + Foundation LB is ``VSPHERE_FOUNDATION`` (no
#: separate NSX edge cluster required); the NSX models are ``NSX`` / ``NSX_VPC``
#: / ``NSX_ADVANCED``; ``HAPROXY`` is deprecated as of vSphere 9.0.
_ALLOWED_EDGE_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"VSPHERE_FOUNDATION", "NSX", "NSX_VPC", "NSX_ADVANCED", "HAPROXY"}
)

#: Config-status values that mean the enable has converged.
_CONFIG_STATUS_RUNNING: Final = "RUNNING"
_KUBERNETES_STATUS_READY: Final = "READY"

#: Default cap on the ``messages`` / ``conditions`` arrays the status op
#: returns inline. During ``CONFIGURING`` the conditions array can grow; the
#: cap keeps the response small + inline-pollable (the scalars stay top-level).
_STATUS_MESSAGES_DEFAULT_CAP: Final = 25


def _validate_network_provider(workloads: Any) -> dict[str, Any] | None:
    """Refuse an unknown network stack loudly; return a refusal envelope or ``None``.

    Reads ``workloads.network.network_type`` and ``workloads.edge.provider``
    and checks each against the authoritative 9.x enums
    (:data:`_ALLOWED_NETWORK_TYPES` / :data:`_ALLOWED_EDGE_PROVIDERS`). An
    unknown value -- or a missing network / edge object -- returns a
    structured ``status="unknown_network_provider"`` /
    ``status="invalid_spec"`` envelope naming the offending value and the
    allowed set, so the composite fails closed *before* dispatching the
    enable POST rather than letting vCenter reject an opaque body. Returns
    ``None`` when both are recognised.
    """
    if not isinstance(workloads, dict):
        return {
            "status": "invalid_spec",
            "guidance": "workloads must be an object with 'network' and 'edge'",
        }
    network = workloads.get("network")
    edge = workloads.get("edge")
    if not isinstance(network, dict) or not isinstance(edge, dict):
        return {
            "status": "invalid_spec",
            "guidance": "workloads.network and workloads.edge are both required objects",
        }
    network_type = network.get("network_type")
    provider = edge.get("provider")
    if network_type not in _ALLOWED_NETWORK_TYPES:
        return {
            "status": "unknown_network_provider",
            "network_type": network_type if isinstance(network_type, str) else None,
            "guidance": (
                f"workloads.network.network_type {network_type!r} is not a known "
                f"vSphere 9.x network stack; allowed: {sorted(_ALLOWED_NETWORK_TYPES)} "
                "(VSPHERE = VDS + Foundation LB, NSX_VPC = NSX VPC, NSXT = classic NSX-T)"
            ),
        }
    if provider not in _ALLOWED_EDGE_PROVIDERS:
        return {
            "status": "unknown_network_provider",
            "network_type": network_type,
            "edge_provider": provider if isinstance(provider, str) else None,
            "guidance": (
                f"workloads.edge.provider {provider!r} is not a known edge provider; "
                f"allowed: {sorted(_ALLOWED_EDGE_PROVIDERS)} (VSPHERE_FOUNDATION pairs "
                "with the VSPHERE network stack for the VDS + Foundation LB model)"
            ),
        }
    return None


def _validate_control_plane(control_plane: Any) -> dict[str, Any] | None:
    """Refuse a control-plane spec missing its load-bearing fields.

    The ``enable_on_compute_cluster`` body requires a ``control_plane`` with
    a management ``network`` and a ``storage_policy`` (the Supervisor API
    server's SPBM policy -- load-bearing on NFS, where there is no default
    policy). A missing field returns ``status="invalid_spec"``; otherwise
    ``None``.
    """
    if not isinstance(control_plane, dict):
        return {
            "status": "invalid_spec",
            "guidance": "control_plane must be an object with 'network' and 'storage_policy'",
        }
    if not isinstance(control_plane.get("network"), dict):
        return {
            "status": "invalid_spec",
            "guidance": "control_plane.network is a required object (management network)",
        }
    if not control_plane.get("storage_policy"):
        return {
            "status": "invalid_spec",
            "guidance": (
                "control_plane.storage_policy is required (the Supervisor API-server "
                "SPBM policy id); on NFS there is no default policy"
            ),
        }
    return None


async def supervisor_enable_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Enable a Supervisor on a single vSphere cluster (async; returns immediately).

    Op-id: ``vmware.composite.supervisor.enable``. Validates the nested
    ``EnableOnComputeClusterSpec`` server-side -- the network stack
    (:func:`_validate_network_provider`, refuses an unknown
    ``network_type`` / edge ``provider`` loudly) and the control-plane
    essentials (:func:`_validate_control_plane`) -- then issues
    ``POST /vcenter/namespace-management/supervisors/{cluster}?action=enable_on_compute_cluster``
    through the governed REST write seam. A parked/denied gate returns the
    :class:`OperationResult` verbatim and no enable fires. On success the
    200 body is the new Supervisor id (a bare string); the composite returns
    ``status="enabling"`` with that id and does **not** block on the
    30-60 min ``CONFIGURING`` -> ``RUNNING`` convergence -- the caller polls
    ``vmware.composite.supervisor.status``. A vim/REST fault
    (``AlreadyExists`` when the cluster already has a Supervisor,
    ``UnableToAllocateResource`` when the cluster is unlicensed) propagates
    as a transport error the dispatcher wraps ``connector_error``.
    """
    cluster = params["cluster"]
    workloads = params["workloads"]
    control_plane = params["control_plane"]

    refusal = _validate_control_plane(control_plane) or _validate_network_provider(workloads)
    if refusal is not None:
        return {"supervisor": None, "cluster": cluster, **refusal}

    sub_op_params: dict[str, Any] = {
        "cluster": cluster,
        "name": params["name"],
        "control_plane": control_plane,
        "workloads": workloads,
    }
    zone = params.get("zone")
    if zone is not None:
        sub_op_params["zone"] = zone

    gate, payload = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_ENABLE_ON_COMPUTE_CLUSTER,
        sub_op_params,
    )
    if gate is not None:
        return gate

    supervisor = _unwrap_value(payload)
    supervisor_id = supervisor if isinstance(supervisor, str) and supervisor else None
    return {
        "status": "enabling",
        "cluster": cluster,
        "supervisor": supervisor_id,
        "network_type": workloads["network"]["network_type"],
        "edge_provider": workloads["edge"]["provider"],
        "guidance": (
            "Supervisor enablement started (asynchronous). Poll "
            "vmware.composite.supervisor.status with this cluster until "
            "config_status == 'RUNNING' and kubernetes_status == 'READY' "
            "(typically 30-60 min); config_status == 'ERROR' needs operator "
            "intervention."
        ),
    }


async def supervisor_disable_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Disable (tear down) the Supervisor on a vSphere cluster (async).

    Op-id: ``vmware.composite.supervisor.disable``. Issues
    ``POST /vcenter/namespace-management/clusters/{cluster}?action=disable``
    through the governed REST write seam (``DELETE clusters/{cluster}``
    404s -- the action form is the documented teardown). The call takes no
    body and returns 204; the composite returns ``status="disabling"`` and
    does not block on the removal converging -- the caller polls
    ``vmware.composite.supervisor.status`` (``config_status`` moves through
    ``REMOVING``). A parked/denied gate returns the :class:`OperationResult`
    verbatim and no teardown fires.
    """
    cluster = params["cluster"]
    gate, _ = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_DISABLE,
        {"cluster": cluster},
    )
    if gate is not None:
        return gate
    return {
        "status": "disabling",
        "cluster": cluster,
        "guidance": (
            "Supervisor teardown started (asynchronous). Poll "
            "vmware.composite.supervisor.status with this cluster; "
            "config_status moves through 'REMOVING'. Teardown removes the "
            "control-plane VMs and worker nodes but leaves the cluster's "
            "networking / zone intact for a fresh re-enable."
        ),
    }


def _project_messages(items: Any, cap: int) -> tuple[list[dict[str, Any]], int]:
    """Cap + project a messages / conditions array; return ``(capped, total)``.

    Keeps the array small + inline (the status op stays pollable without a
    JSONFlux handle). Non-dict rows are dropped; ``total`` is the uncapped
    count so a caller sees the true size even when the list is truncated.
    """
    if not isinstance(items, list):
        return [], 0
    rows = [row for row in items if isinstance(row, dict)]
    return rows[:cap], len(rows)


async def supervisor_status_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any]:
    """Read a cluster's Supervisor config + kubernetes status (poll-friendly).

    Op-id: ``vmware.composite.supervisor.status``. Issues
    ``GET /vcenter/namespace-management/clusters/{cluster}`` (un-gated read)
    and reshapes ``Clusters.Info`` into a compact, inline-pollable envelope:
    the scalar ``config_status`` (``CONFIGURING`` / ``REMOVING`` /
    ``RUNNING`` / ``ERROR``) and ``kubernetes_status`` (``READY`` /
    ``WARNING`` / ``ERROR``) stay top-level so a runbook
    ``OperationCallVerify`` step or a Sensor assertion can read
    ``config_status == 'RUNNING'`` / ``ready == true`` directly -- never
    behind a set-shaped handle. The (potentially long during
    ``CONFIGURING``) ``messages`` + ``conditions`` arrays are capped inline
    to :data:`_STATUS_MESSAGES_DEFAULT_CAP` (override with
    ``messages_limit``); ``message_count`` / ``condition_count`` carry the
    uncapped sizes. Read-only -- never mutates cluster state. A vCenter 400
    (the cluster has no Supervisor enabled) propagates as a transport error
    the dispatcher wraps ``connector_error``.
    """
    cluster = params["cluster"]
    cap = params.get("messages_limit", _STATUS_MESSAGES_DEFAULT_CAP)

    info = _unwrap_value(
        await _read_sub_op(connector, target, operator, _OP_GET_CLUSTER, {"cluster": cluster})
    )
    info = info if isinstance(info, dict) else {}
    config_status = info.get("config_status")
    kubernetes_status = info.get("kubernetes_status")
    messages, message_count = _project_messages(info.get("messages"), cap)
    conditions, condition_count = _project_messages(info.get("conditions"), cap)
    return {
        "cluster": cluster,
        "config_status": config_status if isinstance(config_status, str) else None,
        "kubernetes_status": kubernetes_status if isinstance(kubernetes_status, str) else None,
        "ready": config_status == _CONFIG_STATUS_RUNNING
        and kubernetes_status == _KUBERNETES_STATUS_READY,
        "messages": messages,
        "conditions": conditions,
        "message_count": message_count,
        "condition_count": condition_count,
    }

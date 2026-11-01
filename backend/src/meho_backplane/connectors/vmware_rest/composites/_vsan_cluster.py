# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""vSAN cluster changes: turn on and configure vSAN, encryption, rekey.

* ``vmware.composite.vsan.cluster.configure`` (``dangerous``) -- turn vSAN on
  or off, pick ESA, set the space efficiency (deduplication and compression)
  and turn the performance service on or off, in one
  ``VsanVcClusterConfigSystem.VsanClusterReconfig``.
* ``vmware.composite.vsan.encryption.set`` (``dangerous``) -- turn data-at-rest
  and data-in-transit encryption on or off, with the same method
  (``dataEncryptionConfig``, ``dataInTransitEncryptionConfig``).
* ``vmware.composite.vsan.encryption.rekey`` (``dangerous``) -- a shallow or a
  deep rekey, ``VsanEncryptedClusterRekey_Task``, followed to its end.

The first two are vendor jobs: vSAN can take a long time (a space-efficiency
or encryption change writes the disks again). They answer ``accepted`` with
the vCenter task id as soon as vSAN took the job, and name the status read
(``vmware.task.info``). Before a start they look for a vSAN job that still
runs on the cluster; when they find one they return it (``already_running``)
and start nothing.

The ``VimVsanReconfigSpec`` fields are written in the field order of the
vendor type (vSAN Management SDK 8.0 U2, ``vsanmgmtObjects.py``), because the
SOAP call of vCenter 8.0 checks that order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest.composites._teardown_reads import moid_problem
from meho_backplane.connectors.vmware_rest.composites._vsan_common import (
    VSAN_CLUSTER_RECONFIG,
    VSAN_ENCRYPTED_CLUSTER_REKEY,
    cluster_moref,
    read_cluster,
    running_vsan_task,
    send_vsan_change,
    status_read,
    vsan_op_id,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_RETRIEVE_PROPERTIES,
    _task_timeout_issue,
    _unwrap_value,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import CLUSTER_MOID_PATTERN
from meho_backplane.connectors.vmware_rest.vim_body import VIM_TYPE_NAME_KEY
from meho_backplane.connectors.vmware_rest.vim_task import (
    TASK_STATE_ERROR,
    poll_vim_task,
    task_moref_value,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector
    from meho_backplane.operations._preview import PreviewContext

__all__ = [
    "build_configure_spec",
    "build_encryption_spec",
    "vsan_cluster_configure_composite",
    "vsan_cluster_configure_preview",
    "vsan_encryption_rekey_composite",
    "vsan_encryption_rekey_preview",
    "vsan_encryption_set_composite",
    "vsan_encryption_set_preview",
]

#: Child-op lists: the reads, then the one vSAN change (``_VIM_SUB_OPS_*`` and
#: ``_VSAN_SUB_OPS_*`` names, like the other composites).
_VSAN_SUB_OPS_CLUSTER_CONFIGURE: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    vsan_op_id(VSAN_CLUSTER_RECONFIG),
)
_VSAN_SUB_OPS_ENCRYPTION_SET: Final[tuple[str, ...]] = _VSAN_SUB_OPS_CLUSTER_CONFIGURE
_VSAN_SUB_OPS_ENCRYPTION_REKEY: Final[tuple[str, ...]] = (
    _OP_RETRIEVE_PROPERTIES,
    vsan_op_id(VSAN_ENCRYPTED_CLUSTER_REKEY),
)

#: Field order of ``VimVsanReconfigSpec`` (only the fields these ops send).
_RECONFIG_SPEC_ORDER: Final[tuple[str, ...]] = (
    "vsanClusterConfig",
    "dataEfficiencyConfig",
    "modify",
    "allowReducedRedundancy",
    "dataEncryptionConfig",
    "perfsvcConfig",
    "dataInTransitEncryptionConfig",
)

#: ``space_efficiency`` -> ``(dedupEnabled, compressionEnabled)``.
_SPACE_EFFICIENCY: Final[dict[str, tuple[bool, bool]]] = {
    "none": (False, False),
    "compression": (False, True),
    "deduplication_and_compression": (True, True),
}

#: Wall-clock bound of the rekey poll -- the 600 s convention; tests zero it.
_TASK_TIMEOUT_SECONDS = 600.0

STATUS_ACCEPTED: Final = "accepted"
STATUS_ALREADY_RUNNING: Final = "already_running"
STATUS_NOT_FOUND: Final = "not_found"
STATUS_INVALID_REQUEST: Final = "invalid_request"
STATUS_REKEYED: Final = "rekeyed"
STATUS_TIMEOUT: Final = "timeout"


def _ordered_spec(fields: dict[str, Any]) -> dict[str, Any]:
    """A ``VimVsanReconfigSpec`` with *fields* in the vendor field order."""
    spec: dict[str, Any] = {VIM_TYPE_NAME_KEY: "VimVsanReconfigSpec"}
    for name in _RECONFIG_SPEC_ORDER:
        if fields.get(name) is not None:
            spec[name] = fields[name]
    return spec


def _answer(
    status: str,
    cluster: str,
    *,
    requested: dict[str, Any],
    cluster_name: str | None = None,
    task: str | None = None,
    guidance: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "cluster": cluster,
        "cluster_name": cluster_name,
        "requested": requested,
        "task": task,
        "status_read": status_read(task) if task is not None else None,
        "guidance": guidance,
    }


# ===========================================================================
# The two reconfigure jobs
# ===========================================================================


def build_configure_spec(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(VimVsanReconfigSpec, requested)`` for ``vsan.cluster.configure``.

    ``requested`` echoes the settings the caller asked for, in MEHO's names.
    """
    requested: dict[str, Any] = {}
    fields: dict[str, Any] = {"modify": True}
    cluster_config: dict[str, Any] = {}
    if params.get("vsan_enabled") is not None:
        cluster_config["enabled"] = bool(params["vsan_enabled"])
        requested["vsan_enabled"] = cluster_config["enabled"]
    if params.get("esa") is not None:
        cluster_config["vsanEsaEnabled"] = bool(params["esa"])
        requested["esa"] = cluster_config["vsanEsaEnabled"]
    if cluster_config:
        fields["vsanClusterConfig"] = {VIM_TYPE_NAME_KEY: "VsanClusterConfigInfo", **cluster_config}
    space_efficiency = params.get("space_efficiency")
    if space_efficiency is not None:
        dedup, compression = _SPACE_EFFICIENCY[space_efficiency]
        fields["dataEfficiencyConfig"] = {
            VIM_TYPE_NAME_KEY: "VsanDataEfficiencyConfig",
            "dedupEnabled": dedup,
            "compressionEnabled": compression,
        }
        requested["space_efficiency"] = space_efficiency
    if params.get("performance_service") is not None:
        enabled = bool(params["performance_service"])
        fields["perfsvcConfig"] = {VIM_TYPE_NAME_KEY: "VsanPerfsvcConfig", "enabled": enabled}
        requested["performance_service"] = enabled
    if params.get("allow_reduced_redundancy") is not None:
        fields["allowReducedRedundancy"] = bool(params["allow_reduced_redundancy"])
        requested["allow_reduced_redundancy"] = fields["allowReducedRedundancy"]
    return _ordered_spec(fields), requested


def build_encryption_spec(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(VimVsanReconfigSpec, requested)`` for ``vsan.encryption.set``."""
    requested: dict[str, Any] = {}
    fields: dict[str, Any] = {"modify": True}
    at_rest = params.get("data_at_rest")
    if isinstance(at_rest, dict):
        config: dict[str, Any] = {
            VIM_TYPE_NAME_KEY: "VsanDataEncryptionConfig",
            "encryptionEnabled": bool(at_rest["enabled"]),
        }
        if at_rest.get("key_provider"):
            config["kmsProviderId"] = {
                VIM_TYPE_NAME_KEY: "KeyProviderId",
                "id": at_rest["key_provider"],
            }
        if at_rest.get("erase_disks_before_use") is not None:
            config["eraseDisksBeforeUse"] = bool(at_rest["erase_disks_before_use"])
        fields["dataEncryptionConfig"] = config
        requested["data_at_rest"] = dict(at_rest)
    in_transit = params.get("data_in_transit")
    if isinstance(in_transit, dict):
        transit: dict[str, Any] = {
            VIM_TYPE_NAME_KEY: "VsanDataInTransitEncryptionConfig",
            "enabled": bool(in_transit["enabled"]),
        }
        if in_transit.get("rekey_interval_minutes") is not None:
            transit["rekeyInterval"] = int(in_transit["rekey_interval_minutes"])
        fields["dataInTransitEncryptionConfig"] = transit
        requested["data_in_transit"] = dict(in_transit)
    if params.get("allow_reduced_redundancy") is not None:
        fields["allowReducedRedundancy"] = bool(params["allow_reduced_redundancy"])
        requested["allow_reduced_redundancy"] = fields["allowReducedRedundancy"]
    return _ordered_spec(fields), requested


def _configure_problem(params: dict[str, Any]) -> str | None:
    """Why a configure request cannot be sent, or ``None``."""
    if params.get("esa") is True and params.get("vsan_enabled") is not True:
        return (
            "esa: true needs vsan_enabled: true in the same call "
            "(ESA is chosen when vSAN is turned on)"
        )
    return None


async def _start_reconfigure(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    params: dict[str, Any],
    spec: dict[str, Any],
    requested: dict[str, Any],
    kind: str,
) -> dict[str, Any] | OperationResult:
    """Read the cluster, look for a running vSAN job, then start ``VsanClusterReconfig``.

    *kind* names the change in the texts (``configuration``, ``encryption``).
    """
    cluster = params["cluster"]
    problem = moid_problem(cluster, CLUSTER_MOID_PATTERN, "cluster")
    if problem is not None:
        return _answer(STATUS_INVALID_REQUEST, str(cluster), requested=requested, guidance=problem)
    props = await read_cluster(connector, target, operator, cluster)
    if props is None:
        return _answer(
            STATUS_NOT_FOUND,
            cluster,
            requested=requested,
            guidance=f"no cluster {cluster!r}; pass a cluster moid. Nothing was sent.",
        )
    name = props.get("name") if isinstance(props.get("name"), str) else None
    running = await running_vsan_task(connector, target, operator, props.get("recentTask"))
    if running is not None:
        return _answer(
            STATUS_ALREADY_RUNNING,
            cluster,
            requested=requested,
            cluster_name=name,
            task=running,
            guidance=(
                f"a vSAN job ({running}) already runs on cluster {cluster!r}. Nothing new was "
                "started. Follow it with vmware.task.info, then read the cluster again and "
                "decide whether this change is still needed."
            ),
        )
    gate, answer = await send_vsan_change(
        connector,
        target,
        operator,
        method=VSAN_CLUSTER_RECONFIG,
        body={"cluster": cluster_moref(cluster), "vsanReconfigSpec": spec},
        params={"cluster": cluster, "cluster_name": name, **requested},
    )
    if gate is not None:
        return gate
    task = task_moref_value(_unwrap_value(answer))
    if task is None:
        raise RuntimeError(
            f"vSAN {kind} change of cluster {cluster!r}: VsanClusterReconfig answered without "
            f"a task id ({answer!r}); read the cluster before you try again"
        )
    return _answer(
        STATUS_ACCEPTED,
        cluster,
        requested=requested,
        cluster_name=name,
        task=task,
        guidance=(
            f"vSAN accepted the {kind} change as task {task}; it is not done yet. Read "
            "vmware.task.info with this task id until it ends. Do not start the change again."
        ),
    )


async def vsan_cluster_configure_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Turn vSAN on or change its settings on one cluster (a vendor job).

    Op-id: ``vmware.composite.vsan.cluster.configure``. Reads the cluster
    (``not_found`` when it does not exist) and its recent tasks
    (``already_running`` when a vSAN job runs), then sends one gated
    ``VsanClusterReconfig`` and answers ``accepted`` with the task id.
    """
    spec, requested = build_configure_spec(params)
    problem = _configure_problem(params)
    if problem is not None:
        return _answer(
            STATUS_INVALID_REQUEST,
            str(params.get("cluster")),
            requested=requested,
            guidance=problem,
        )
    return await _start_reconfigure(
        connector,
        target,
        operator,
        params=params,
        spec=spec,
        requested=requested,
        kind="configuration",
    )


async def vsan_encryption_set_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Turn vSAN data-at-rest or data-in-transit encryption on or off (a vendor job).

    Op-id: ``vmware.composite.vsan.encryption.set``. The same steps as
    :func:`vsan_cluster_configure_composite`, with the encryption fields of
    the reconfigure spec.
    """
    spec, requested = build_encryption_spec(params)
    return await _start_reconfigure(
        connector,
        target,
        operator,
        params=params,
        spec=spec,
        requested=requested,
        kind="encryption",
    )


async def vsan_cluster_configure_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vsan.cluster.configure``: the cluster and the settings that change."""
    cluster = ctx.params.get("cluster")
    if not isinstance(cluster, str):
        return None
    spec, requested = build_configure_spec(ctx.params)
    return {"action": "configure_vsan", "cluster": cluster, "requested": requested, "spec": spec}


async def vsan_encryption_set_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vsan.encryption.set``: the cluster and the encryption settings."""
    cluster = ctx.params.get("cluster")
    if not isinstance(cluster, str):
        return None
    spec, requested = build_encryption_spec(ctx.params)
    return {
        "action": "set_vsan_encryption",
        "cluster": cluster,
        "requested": requested,
        "spec": spec,
    }


# ===========================================================================
# vsan.encryption.rekey -- VsanEncryptedClusterRekey_Task
# ===========================================================================


def _rekey_body(cluster: str, params: dict[str, Any]) -> dict[str, Any]:
    """The rekey body in the vendor argument order."""
    body: dict[str, Any] = {
        "encryptedCluster": cluster_moref(cluster),
        "deepRekey": bool(params.get("deep_rekey", False)),
    }
    if params.get("allow_reduced_redundancy") is not None:
        body["allowReducedRedundancy"] = bool(params["allow_reduced_redundancy"])
    return body


async def vsan_encryption_rekey_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Rekey the vSAN encryption of one cluster and wait for the task.

    Op-id: ``vmware.composite.vsan.encryption.rekey``. Reads the cluster
    (``not_found``), sends the gated ``VsanEncryptedClusterRekey_Task`` and
    follows the task. A task fault raises with vCenter's fault type and text;
    a poll timeout answers ``timeout`` with the task id (the rekey may still
    finish).
    """
    cluster = params["cluster"]
    deep = bool(params.get("deep_rekey", False))
    requested: dict[str, Any] = {"deep_rekey": deep}
    if params.get("allow_reduced_redundancy") is not None:
        requested["allow_reduced_redundancy"] = bool(params["allow_reduced_redundancy"])
    problem = moid_problem(cluster, CLUSTER_MOID_PATTERN, "cluster")
    if problem is not None:
        return _answer(STATUS_INVALID_REQUEST, str(cluster), requested=requested, guidance=problem)
    props = await read_cluster(connector, target, operator, cluster)
    if props is None:
        return _answer(
            STATUS_NOT_FOUND,
            cluster,
            requested=requested,
            guidance=f"no cluster {cluster!r}; pass a cluster moid. Nothing was sent.",
        )
    name = props.get("name") if isinstance(props.get("name"), str) else None
    gate, answer = await send_vsan_change(
        connector,
        target,
        operator,
        method=VSAN_ENCRYPTED_CLUSTER_REKEY,
        body=_rekey_body(cluster, params),
        params={"cluster": cluster, "cluster_name": name, **requested},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(answer),
        timeout_seconds=_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vsan.encryption.rekey: VsanEncryptedClusterRekey_Task on cluster {cluster!r} "
            f"failed: {outcome.fault_type or 'fault'}: "
            f"{outcome.error_message or '<no fault text>'}"
        )
    if outcome.timed_out:
        timed_out = _answer(
            STATUS_TIMEOUT,
            cluster,
            requested=requested,
            cluster_name=name,
            task=outcome.task,
            guidance=(
                f"the rekey task {outcome.task} did not end within "
                f"{int(_TASK_TIMEOUT_SECONDS)} seconds; a deep rekey writes all data again and "
                "can take hours. Follow it with vmware.task.info. Do not start it again."
            ),
        )
        timed_out["issues"] = [_task_timeout_issue("VsanEncryptedClusterRekey_Task", outcome.task)]
        return timed_out
    return _answer(
        STATUS_REKEYED,
        cluster,
        requested=requested,
        cluster_name=name,
        task=outcome.task,
        guidance="The rekey task ended well.",
    )


async def vsan_encryption_rekey_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """Preview ``vsan.encryption.rekey``: the cluster and the rekey kind."""
    cluster = ctx.params.get("cluster")
    if not isinstance(cluster, str):
        return None
    return {
        "action": "rekey_vsan_encryption",
        "cluster": cluster,
        "deep_rekey": bool(ctx.params.get("deep_rekey", False)),
    }

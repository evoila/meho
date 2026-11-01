# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the vSAN changes: the vSAN methods, the gated send, the reads.

The five vSAN changes (:mod:`._vsan_cluster`, :mod:`._vsan_disk_claim`,
:mod:`._vsan_disk_wipe`) call these vendor methods. Each vSAN method is a
module-level
:class:`~meho_backplane.connectors.vmware_rest._mount.VsanMethod`, so the
reconcile lanes find it and check it against the vendor files. Every vSAN
call goes through :meth:`VmwareRestConnector._post_vsan`: VI-JSON
``/vsan/<Type>/<moId>/<Method>`` on vCenter 9.x, SOAP to ``/vsanHealth`` on
vCenter 8.0.

The managed object ids come from the vendor:

* ``vsan-cluster-config-system`` and ``vsan-disk-management-system``: the
  vendor sample ``vsanapiutils.py`` of the vSAN Management SDK for Python.
* ``vsanSystemEx-<N>`` for the host ``host-<N>``: the vendor reference page
  of ``VsanSystemEx`` in the vSAN Management SDK 8.0 U2 ("It can be also
  accessed with MOID of 'vsanSystemEx-<N>' through vSAN service at vCenter
  server side").

``HostVsanSystem.QueryDisksForVsan`` is a VIM method with another VI-JSON
path per release: ``/HostVsanSystem/{moId}/QueryDisksForVsan`` in the
vendor files of 8.0 U1 to U3, and ``/vsan/HostVsanSystem/{moId}/...`` from
9.0 on. :func:`query_disks_for_vsan` picks the path of the target's
release.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.vmware_rest._mount import VsanMethod, vsan_method_path
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._teardown_reads import (
    moref_values,
    read_many,
    read_object,
)
from meho_backplane.connectors.vmware_rest.vim_body import unwrap_vim_value, vim_moref
from meho_backplane.operations.composite import enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors import OperationResult
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "CLUSTER_MO_TYPE",
    "HOST_MO_TYPE",
    "TASK_INFO_OP_ID",
    "VSAN_ADD_STORAGE_POOL_DISK",
    "VSAN_CLUSTER_RECONFIG",
    "VSAN_ENCRYPTED_CLUSTER_REKEY",
    "VSAN_HOST_QUERY_WIPE_DISK",
    "VSAN_HOST_WIPE_DISK",
    "VSAN_INITIALIZE_DISK_MAPPINGS",
    "cluster_moref",
    "disk_results",
    "host_vsan_system_ex",
    "moid_number",
    "query_disks_for_vsan",
    "read_cluster",
    "running_vsan_task",
    "send_vsan_change",
    "status_read",
    "vsan_op_id",
]

CLUSTER_MO_TYPE: Final = "ClusterComputeResource"
HOST_MO_TYPE: Final = "HostSystem"
_TASK_MO_TYPE: Final = "Task"

#: The status read of a vSAN job: one vCenter task by its id.
TASK_INFO_OP_ID: Final = "vmware.task.info"

VSAN_CLUSTER_RECONFIG = VsanMethod(
    mo_type="VsanVcClusterConfigSystem",
    moid="vsan-cluster-config-system",
    method="VsanClusterReconfig",
)
VSAN_ENCRYPTED_CLUSTER_REKEY = VsanMethod(
    mo_type="VsanVcClusterConfigSystem",
    moid="vsan-cluster-config-system",
    method="VsanEncryptedClusterRekey_Task",
)
VSAN_ADD_STORAGE_POOL_DISK = VsanMethod(
    mo_type="VimClusterVsanVcDiskManagementSystem",
    moid="vsan-disk-management-system",
    method="VsanAddStoragePoolDisk",
)
VSAN_INITIALIZE_DISK_MAPPINGS = VsanMethod(
    mo_type="VimClusterVsanVcDiskManagementSystem",
    moid="vsan-disk-management-system",
    method="InitializeDiskMappings",
)
#: ``VsanSystemEx`` is one object per host. ``{host}`` stands for the number
#: of the host's moid; :func:`host_vsan_system_ex` puts it in.
VSAN_HOST_WIPE_DISK = VsanMethod(
    mo_type="VsanSystemEx",
    moid="vsanSystemEx-{host}",
    method="VsanHostWipeDisk",
)
VSAN_HOST_QUERY_WIPE_DISK = VsanMethod(
    mo_type="VsanSystemEx",
    moid="vsanSystemEx-{host}",
    method="VsanHostQueryWipeDisk",
)

_HOST_VSAN_SYSTEM_MO_TYPE: Final = "HostVsanSystem"
_QUERY_DISKS_FOR_VSAN: Final = "QueryDisksForVsan"

#: Task states of a job that has not ended.
_RUNNING_TASK_STATES: Final = frozenset({"queued", "running"})


def vsan_op_id(method: VsanMethod) -> str:
    """The governance key of a vSAN method: its path in the vendor VI/JSON file.

    For example ``POST:/vsan/VsanVcClusterConfigSystem/{moId}/VsanClusterReconfig``,
    the op id the ingest of the 9.0 ``vi-json.yaml`` gives the same method.
    """
    return f"POST:{vsan_method_path(method.mo_type, '{moId}', method.method)}"


def moid_number(moid: str, prefix: str) -> str | None:
    """The number of *moid* after *prefix* (``host-12`` -> ``12``), else ``None``."""
    if not moid.startswith(prefix):
        return None
    number = moid[len(prefix) :]
    return number if number.isdigit() else None


def host_vsan_system_ex(method: VsanMethod, host: str) -> VsanMethod:
    """*method* on the ``VsanSystemEx`` of *host* (``host-12`` -> ``vsanSystemEx-12``)."""
    number = moid_number(host, "host-")
    if number is None:
        raise ValueError(f"host must be a moid like 'host-12', got {host!r}")
    return dataclasses.replace(method, moid=method.moid.replace("{host}", number))


def status_read(task: str) -> dict[str, Any]:
    """The read that follows a vSAN job: ``vmware.task.info`` with the task id."""
    return {"op_id": TASK_INFO_OP_ID, "params": {"task": task}}


async def send_vsan_change(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    method: VsanMethod,
    body: dict[str, Any],
    params: dict[str, Any],
) -> tuple[OperationResult | None, Any]:
    """Gate then send one vSAN change; ``(gate, None)`` or ``(None, answer)``.

    The same governance seam as the VIM writes (``_write._write_vmomi_sub_op``):
    the step is checked under :func:`vsan_op_id` with the logical *params*.
    When the seam returns a result (waits for approval, or refused), nothing
    is sent. Otherwise the method goes through ``_post_vsan``; a vSAN fault
    raises with vCenter's fault type and text.
    """
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_write._step_connector_id(connector),
        op_id=vsan_op_id(method),
        safety_level=_write._WRITE_SAFETY_LEVEL,
        requires_approval=_write._WRITE_REQUIRES_APPROVAL,
        target=target,
        params=params,
    )
    if gate is not None:
        return gate, None
    answer = await connector._post_vsan(target, method, operator=operator, json=body)
    return None, answer


async def read_cluster(
    connector: VmwareRestConnector, target: Any, operator: Operator, cluster: str
) -> dict[str, Any] | None:
    """The cluster's ``name`` and ``recentTask``; ``None`` when it does not exist."""
    return await read_object(
        connector,
        target,
        operator,
        mo_type=CLUSTER_MO_TYPE,
        moid=cluster,
        props=["name", "recentTask"],
    )


def _is_vsan_task(info: dict[str, Any]) -> bool:
    """``True`` when a task is a vSAN job: its description id or name names vSAN.

    No vendor file lists the description id of a ``VsanClusterReconfig``
    task, so this matches any task whose ``descriptionId`` or ``name`` has
    "vsan" in it. vSAN runs one change of a cluster at a time, so any vSAN
    job that runs on the cluster is a reason not to start another one.
    """
    text = f"{info.get('descriptionId') or ''} {info.get('name') or ''}"
    return "vsan" in text.lower()


async def running_vsan_task(
    connector: VmwareRestConnector, target: Any, operator: Operator, recent: Any
) -> str | None:
    """The id of a vSAN job that still runs on the cluster, else ``None``.

    Reads ``Task.info`` of the cluster's recent tasks. A help, not a lock: two
    callers can both look before either starts, and a job that just ended is
    no longer running.
    """
    task_ids = [moid for kind, moid in moref_values(recent) if kind in ("", _TASK_MO_TYPE)]
    if not task_ids:
        return None
    infos, _complete = await read_many(
        connector, target, operator, mo_type=_TASK_MO_TYPE, moids=task_ids, props=["info"]
    )
    for task_id in task_ids:
        info = infos.get(task_id, {}).get("info")
        if (
            isinstance(info, dict)
            and info.get("state") in _RUNNING_TASK_STATES
            and _is_vsan_task(info)
        ):
            return task_id
    return None


async def query_disks_for_vsan(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vsan_system: str,
    disks: list[str],
) -> list[dict[str, Any]]:
    """``HostVsanSystem.QueryDisksForVsan`` for the named disks of one host.

    Each row is a ``VsanHostDiskResult``: ``disk`` (the ``HostScsiDisk``),
    ``state`` (``inUse`` / ``eligible`` / ``ineligible``), ``vsanUuid`` and
    ``error``. The path is the one of the target's release (see the module
    text). A read: it is not gated.
    """
    body = {"canonicalName": list(disks)}
    if await connector._vsan_needs_soap(target, operator):
        path = f"/HostVsanSystem/{vsan_system}/QueryDisksForVsan"
    else:
        path = vsan_method_path(_HOST_VSAN_SYSTEM_MO_TYPE, vsan_system, _QUERY_DISKS_FOR_VSAN)
    answer = await connector._post_vmomi_json(target, path, operator=operator, json=body)
    return disk_results(answer)


def disk_results(answer: Any) -> list[dict[str, Any]]:
    """The dict rows of a list answer; one dict (a SOAP list of one) becomes a list."""
    value = unwrap_vim_value(answer)
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def cluster_moref(cluster: str) -> dict[str, str]:
    """The cluster as a VI-JSON managed object reference."""
    return vim_moref(CLUSTER_MO_TYPE, cluster)

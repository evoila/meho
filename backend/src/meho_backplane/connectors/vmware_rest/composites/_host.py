# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Host-domain write ``vmware.composite.host.*`` handlers (#3182).

Three vCenter-mediated **host-domain** write composites that fill the
coverage gap Initiative #2907's register named after a governed
nested-VCF factory bring-up hit three host writes with no backplane op
(each dropped to a documented out-of-band fallback):

* ``vmware.composite.host.datastore_mount_nfs`` --
  ``HostDatastoreSystem.CreateNasDatastore`` (mount an NFS export as a
  datastore on a host);
* ``vmware.composite.host.disk_mark_flash`` --
  ``HostStorageSystem.MarkAsSsd_Task`` / ``MarkAsNonSsd_Task`` (present a
  virtual disk as flash/HDD so vSAN-ready validation passes);
* ``vmware.composite.host.service_control`` --
  ``HostServiceSystem`` start/stop/restart + ``UpdateServicePolicy``,
  bounded to a curated server-side service allowlist.

All three ride the **same** governed VI-JSON write seam the #2893
disk-grow write established: every mutating vim method flows through
:func:`~meho_backplane.connectors.vmware_rest.composites._write._write_vmomi_sub_op`
(the #2254 ``enforce_subop_policy`` gate → ``_post_vmomi_json`` on the
documented ``/sdk/vim25/{release}`` base) -- no ``pyvmomi`` SDK
dependency. On a **vCenter** target the host is selected by display name
**or** moref (:func:`_resolve_host_moid`); on a **standalone ESXi**
target -- one no vCenter manages yet (#3332) -- the host *is* the target
(the well-known ``ha-host``), resolved via the VI-JSON seam without any
``GET:/vcenter/host`` listing (:func:`classify_host_target`).

Config-manager indirection
--------------------------

These vim methods are not on the ``HostSystem`` managed object -- they
are on its per-host config sub-managers
(``HostSystem.configManager.datastoreSystem`` /
``.storageSystem`` / ``.serviceSystem``, each a
``ManagedObjectReference``, spec-verified against ``HostConfigManager``
in the pinned ``vi-json.yaml``). So every handler first resolves the
host moid, then reads the one needed ``configManager.<sub>`` MoRef via a
single un-gated ``RetrievePropertiesEx`` (a read), and mounts the method
on that sub-manager's moid.

Set-shaped reduction
--------------------

``disk_mark_flash`` fans out over N disks and returns a per-disk
``results`` array (partial-failure tolerated), so its response is
JSONFlux-reduced to a handle by the dispatcher when large -- the same
auto-reduction ``vm.power.bulk`` relies on. The mount returns a single
datastore summary and service_control a single applied envelope; both
stay inline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.composites._write import (
    _OP_LIST_HOSTS,
    _read_sub_op,
    _unwrap_value,
    _write_vmomi_sub_op,
)
from meho_backplane.connectors.vmware_rest.host_target import (
    HOST_FLAVOR_ESXI,
    STANDALONE_ESXI_HOST_MOID,
    classify_host_target,
)
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
    unwrap_vim_value,
)
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "datastore_mount_nfs_composite",
    "disk_mark_flash_composite",
    "service_control_composite",
]


# vim (VI-JSON) governance op_ids -- the canonical ``METHOD:/path`` keys the
# ingest parser emits from ``vi-json.yaml`` (the moId rides the path as
# ``{moId}``). These are the op_ids fed to ``enforce_subop_policy`` via
# ``_write_vmomi_sub_op``; the concrete path (moId substituted) is what
# ``_post_vmomi_json`` POSTs. Kept out of any ``_SUB_OPS_*`` namespace so the
# vCenter-REST ingest-reconcile sweep does not treat a vi-json path as a
# ``vcenter.yaml`` row; the pinned ``vi-json.yaml`` reconcile asserts them.
#
# * ``CreateNasDatastore`` is **synchronous** -- its 200 body is the new
#   Datastore MoRef directly, NOT a Task, so it is never polled
#   (spec-verified: ``responses.200`` refers a ``Datastore`` instance).
# * ``MarkAsSsd_Task`` / ``MarkAsNonSsd_Task`` return a ``*_Task`` MoRef
#   polled to a terminal state. The pinned spec's method for the HDD
#   direction is ``MarkAsNonSsd_Task`` (NOT ``MarkAsHdd_Task``, the #3182
#   issue's mis-spelling) -- both are the same op keyed on the ``mode`` param.
# * ``StartService`` / ``StopService`` / ``RestartService`` /
#   ``UpdateServicePolicy`` are **synchronous** 204-No-Content methods (no
#   Task, no result).
_OP_CREATE_NAS_DATASTORE: Final = "POST:/HostDatastoreSystem/{moId}/CreateNasDatastore"
_OP_MARK_AS_SSD_TASK: Final = "POST:/HostStorageSystem/{moId}/MarkAsSsd_Task"
_OP_MARK_AS_NON_SSD_TASK: Final = "POST:/HostStorageSystem/{moId}/MarkAsNonSsd_Task"
_OP_START_SERVICE: Final = "POST:/HostServiceSystem/{moId}/StartService"
_OP_STOP_SERVICE: Final = "POST:/HostServiceSystem/{moId}/StopService"
_OP_RESTART_SERVICE: Final = "POST:/HostServiceSystem/{moId}/RestartService"
_OP_UPDATE_SERVICE_POLICY: Final = "POST:/HostServiceSystem/{moId}/UpdateServicePolicy"
# Canonical vi-json.yaml key for the config-manager read (the singleton
# PropertyCollector moId rides the path). The read is un-gated (a resolution
# read, like disk-grow's device read), but the op_id is listed on the sub-op
# manifests so the reconcile lane asserts the path exists in the pinned spec.
_OP_RETRIEVE_PROPERTIES: Final = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"

# Concrete runtime path for the config-manager read: the PropertyCollector is
# a singleton whose moId is the literal ``propertyCollector`` (the same
# concrete path the typed reads + read composites POST).
_VMOMI_RETRIEVE_PROPERTIES_PATH: Final = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"

#: vi-json sub-op manifests (parallel to ``_write._VIM_SUB_OPS_VM_DISK_GROW``;
#: named out of the ``_SUB_OPS_*`` namespace so the vcenter.yaml sweep skips
#: them). The pinned ``vi-json.yaml`` reconcile lane introspects these to
#: assert every declared vim path exists in the spec.
_VIM_SUB_OPS_HOST_DATASTORE_MOUNT_NFS: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_CREATE_NAS_DATASTORE,
)
_VIM_SUB_OPS_HOST_DISK_MARK_FLASH: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_MARK_AS_SSD_TASK,
    _OP_MARK_AS_NON_SSD_TASK,
)
_VIM_SUB_OPS_HOST_SERVICE_CONTROL: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_START_SERVICE,
    _OP_STOP_SERVICE,
    _OP_RESTART_SERVICE,
    _OP_UPDATE_SERVICE_POLICY,
)

# vim MO type + data-object ``_typeName`` discriminators (#3103: every
# DataObject in a request body carries its tag). ``HostNasVolumeSpec`` is the
# ``CreateNasDatastoreRequestType.spec`` data object.
_HOST_SYSTEM_MO_TYPE: Final = "HostSystem"
_HOST_NAS_VOLUME_SPEC_TYPE: Final = "HostNasVolumeSpec"

# ``HostSystem.configManager`` sub-manager property paths (spec-verified
# against ``HostConfigManager`` in the pinned ``vi-json.yaml``). Each read
# returns the per-host sub-manager's ``ManagedObjectReference``.
_PROP_CM_DATASTORE_SYSTEM: Final = "configManager.datastoreSystem"
_PROP_CM_STORAGE_SYSTEM: Final = "configManager.storageSystem"
_PROP_CM_SERVICE_SYSTEM: Final = "configManager.serviceSystem"

# Curated server-side service allowlist for ``service_control`` (#3182). A
# host service name NOT in this set is refused with a structured
# ``service_not_allowed`` before any resolution or write -- never
# passed through to the vim method (the standing no-arbitrary-exec posture,
# #2901). The set is the small family of ESXi services a diagnostic /
# upgrade arc legitimately toggles: SSH (``TSM-SSH``), the ESXi Shell
# (``TSM``), and the two time daemons (``ntpd`` / ``ptpd``). Each key is a
# real ``HostService.key`` (``HostServiceSystem.serviceInfo``).
_SERVICE_ALLOWLIST: Final[frozenset[str]] = frozenset({"TSM-SSH", "TSM", "ntpd", "ptpd"})

# operator-facing ``action`` -> synchronous HostServiceSystem method op_id.
_SERVICE_ACTION_OP_IDS: Final[dict[str, str]] = {
    "start": _OP_START_SERVICE,
    "stop": _OP_STOP_SERVICE,
    "restart": _OP_RESTART_SERVICE,
}

# operator-facing ``mode`` -> HostStorageSystem mark method op_id. ``flash``
# presents the disk as SSD; ``non_flash`` is the inverse (``MarkAsNonSsd_Task``).
_MARK_FLASH_MODE_OP_IDS: Final[dict[str, str]] = {
    "flash": _OP_MARK_AS_SSD_TASK,
    "non_flash": _OP_MARK_AS_NON_SSD_TASK,
}

# Default wall-clock bound for the MarkAs*_Task poll -- the 600s
# ``vm.disk.grow`` convention; module-global so tests can zero it.
_MARK_FLASH_TASK_TIMEOUT_SECONDS = 600.0


def _extract_single_moref_value(retrieve_result: Any, prop: str) -> str | None:
    """Pull the MoRef ``value`` for *prop* out of a RetrievePropertiesEx result.

    The config-manager read queries one ``HostSystem`` for one
    ``configManager.<sub>`` property whose ``val`` is a
    ``ManagedObjectReference``. Funnels ``val`` through
    :func:`unwrap_vim_value` (#3106) so a boxed MoRef normalises, then
    returns its ``value`` moid. ``None`` when the property is absent or not
    a MoRef -- the caller maps that to ``config_manager_unreadable``.
    """
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    if not isinstance(objects, list):
        return None
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for entry in obj.get("propSet", []) or []:
            if isinstance(entry, dict) and entry.get("name") == prop:
                val = unwrap_vim_value(entry.get("val"))
                value = val.get("value") if isinstance(val, dict) else None
                return value if isinstance(value, str) and value else None
    return None


async def _resolve_host_moid(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    host: str,
) -> tuple[str | None, str | None, list[str]]:
    """Resolve a host selector (display name **or** moref) to its HostSystem moid.

    Returns ``(moid, None, [])`` on a unique resolution, or
    ``(None, status, candidates)`` on failure -- ``status`` one of
    ``ambiguous_host`` / ``host_not_found`` and ``candidates`` the moids an
    ambiguous display name matched. Resolution ladder (both filters are on
    the read-only ``GET:/vcenter/host``):

    1. ``filter.names=[host]`` -- a unique display-name match wins; multiple
       matches refuse with the candidate moids (never the first row);
    2. no name match -- treat *host* as a moref and verify it via
       ``filter.hosts=[host]``; a single row confirms it, else
       ``host_not_found``.
    """
    by_name = await _list_host_moids(connector, target, operator, {"filter.names": [host]})
    if len(by_name) == 1:
        return by_name[0], None, []
    if len(by_name) > 1:
        return None, "ambiguous_host", by_name
    by_moref = await _list_host_moids(connector, target, operator, {"filter.hosts": [host]})
    if len(by_moref) == 1:
        return by_moref[0], None, []
    return None, "host_not_found", []


async def _list_host_moids(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    query: dict[str, Any],
) -> list[str]:
    """Return the ``host`` moids from one ``GET:/vcenter/host`` listing."""
    listing = await _read_sub_op(connector, target, operator, _OP_LIST_HOSTS, query)
    rows = _unwrap_value(listing)
    if not isinstance(rows, list):
        return []
    return [
        row["host"] for row in rows if isinstance(row, dict) and isinstance(row.get("host"), str)
    ]


async def _resolve_config_manager_moid(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    host_moid: str,
    prop: str,
) -> str | None:
    """Resolve one ``HostSystem.configManager.<sub>`` sub-manager moid (un-gated read)."""
    result = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=retrieve_properties_body(_HOST_SYSTEM_MO_TYPE, [host_moid], [prop]),
    )
    return _extract_single_moref_value(result, prop)


async def _resolve_host_and_manager(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    host: str | None,
    prop: str,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Resolve host moid + one config-manager sub-moid, or a refusal envelope.

    Returns ``(host_moid, manager_moid, None)`` on success or
    ``(None, None, envelope)`` on a pre-write refusal. Branches on the
    target's probe fingerprint (:func:`classify_host_target`, #3332): a
    **standalone ESXi** target resolves to the well-known
    :data:`STANDALONE_ESXI_HOST_MOID` (``ha-host``) directly -- no
    ``GET:/vcenter/host`` listing (absent on a host no vCenter manages) and
    the *host* selector is ignored (the host is the target); a **vCenter**
    target resolves *host* (display name or moref) through the existing
    ``GET:/vcenter/host`` ladder (unchanged); any **other reachable
    product** fails closed. The config-manager read is issued the same way
    for both flavors, so a standalone ESXi that cannot answer it fails
    closed exactly like a vCenter host. Envelope ``status`` is
    ``unsupported_host_target`` / ``host_required`` / ``host_not_found`` /
    ``ambiguous_host`` / ``config_manager_unreadable``; ``candidate_hosts``
    rides on ambiguity.
    """
    flavor, refusal = classify_host_target(target)
    if refusal is not None:
        return None, None, {**refusal, "host": host}
    if flavor == HOST_FLAVOR_ESXI:
        host_moid = STANDALONE_ESXI_HOST_MOID
    elif not host:
        # vCenter path needs a host selector; ESXi never reaches here.
        return None, None, {"status": "host_required", "host": host}
    else:
        resolved, failure, candidates = await _resolve_host_moid(
            connector, target, operator, host=host
        )
        if resolved is None:
            envelope: dict[str, Any] = {"status": failure, "host": host}
            if candidates:
                envelope["candidate_hosts"] = candidates
            return None, None, envelope
        host_moid = resolved
    manager_moid = await _resolve_config_manager_moid(
        connector, target, operator, host_moid=host_moid, prop=prop
    )
    if manager_moid is None:
        return None, None, {"status": "config_manager_unreadable", "host": host_moid}
    return host_moid, manager_moid, None


# ===========================================================================
# host.datastore_mount_nfs
# ===========================================================================


async def datastore_mount_nfs_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Mount an NFS export as a datastore on a host via ``CreateNasDatastore``.

    Op-id: ``vmware.composite.host.datastore_mount_nfs``. Resolves the host
    (name/moref) → its ``HostDatastoreSystem`` (config-manager read), builds
    a ``HostNasVolumeSpec`` and issues the **synchronous**
    ``HostDatastoreSystem.CreateNasDatastore`` through the governed vmomi
    write seam. The 200 body is the new Datastore MoRef directly (no task
    poll), so the composite returns ``status="mounted"`` with the datastore
    moid + a summary of the mount. A parked/denied gate returns the
    :class:`OperationResult` verbatim and no mount fires. A vim fault
    (``DuplicateName`` for an existing datastore, ``HostConfigFault`` for an
    unreachable export) propagates as a transport error the dispatcher wraps
    ``connector_error``.
    """
    host_moid, ds_system_moid, refusal = await _resolve_host_and_manager(
        connector, target, operator, host=params.get("host"), prop=_PROP_CM_DATASTORE_SYSTEM
    )
    if refusal is not None:
        return {**refusal, "datastore": None}

    nas_spec: dict[str, Any] = {
        VIM_TYPE_NAME_KEY: _HOST_NAS_VOLUME_SPEC_TYPE,
        "remoteHost": params["nfs_server"],
        "remotePath": params["remote_path"],
        "localPath": params["datastore_name"],
        "accessMode": params.get("access_mode", "readWrite"),
        "type": params.get("nfs_type", "NFS"),
    }
    gate, payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_CREATE_NAS_DATASTORE,
        vmomi_path=f"/HostDatastoreSystem/{ds_system_moid}/CreateNasDatastore",
        body={"spec": nas_spec},
        params={
            "host": host_moid,
            "nfs_server": params["nfs_server"],
            "remote_path": params["remote_path"],
            "datastore_name": params["datastore_name"],
        },
    )
    if gate is not None:
        return gate

    datastore = unwrap_vim_value(payload)
    datastore_moid = datastore.get("value") if isinstance(datastore, dict) else None
    return {
        "status": "mounted",
        "host": host_moid,
        "datastore": datastore_moid,
        "summary": {
            "datastore": datastore_moid,
            "name": params["datastore_name"],
            "nfs_server": params["nfs_server"],
            "remote_path": params["remote_path"],
            "access_mode": nas_spec["accessMode"],
            "type": nas_spec["type"],
        },
        "guidance": None,
    }


# ===========================================================================
# host.disk_mark_flash
# ===========================================================================


async def disk_mark_flash_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Mark host disks as flash (SSD) or non-flash (HDD) via ``MarkAs*_Task``.

    Op-id: ``vmware.composite.host.disk_mark_flash``. Resolves the host
    (name/moref) → its ``HostStorageSystem`` (config-manager read), then per
    ``scsiDiskUuid`` issues the mode's ``MarkAsSsd_Task`` (``mode="flash"``)
    or ``MarkAsNonSsd_Task`` (``mode="non_flash"``) through the governed
    vmomi write seam and polls the returned Task to a terminal state.
    Set-shaped: one ``results`` row per disk (partial failure tolerated -- a
    per-disk task fault / transport error / poll timeout is captured, not
    aborted), aggregated into ``summary`` counts. A parked/denied gate on
    the first disk returns the :class:`OperationResult` verbatim so no disk
    is marked -- the gate decision is identical across disks (same op_id,
    target, principal).
    """
    host_moid, storage_system_moid, refusal = await _resolve_host_and_manager(
        connector, target, operator, host=params.get("host"), prop=_PROP_CM_STORAGE_SYSTEM
    )
    if refusal is not None:
        return {**refusal, "mode": params.get("mode", "flash"), "results": [], "summary": None}

    mode = params.get("mode", "flash")
    op_id = _MARK_FLASH_MODE_OP_IDS[mode]
    method_name = op_id.rsplit("/", 1)[-1]
    results: list[dict[str, Any]] = []
    for disk_uuid in params["disk_uuids"]:
        gate, task_payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=op_id,
            vmomi_path=f"/HostStorageSystem/{storage_system_moid}/{method_name}",
            body={"scsiDiskUuid": disk_uuid},
            params={"host": host_moid, "scsiDiskUuid": disk_uuid, "mode": mode},
        )
        if gate is not None:
            # Deterministic across disks (same op_id/target/principal); on the
            # first disk nothing has been marked yet, so park the whole op.
            return gate
        results.append(await _poll_mark_disk(connector, target, operator, disk_uuid, task_payload))

    marked = sum(1 for row in results if row["status"] == "marked")
    return {
        "status": "marked" if marked == len(results) else "partial",
        "host": host_moid,
        "mode": mode,
        "results": results,
        "summary": {"marked": marked, "failed": len(results) - marked},
        "guidance": None,
    }


async def _poll_mark_disk(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    disk_uuid: str,
    task_payload: Any,
) -> dict[str, Any]:
    """Poll one MarkAs*_Task to terminal; return the per-disk ``results`` row.

    Partial-failure tolerant: a task fault / poll timeout / transport error
    on one disk is captured as a typed per-disk status (``faulted`` /
    ``timeout`` / ``error``) rather than aborting the fan-out, mirroring
    ``vm.power.bulk``'s per-entity capture.
    """
    try:
        outcome = await poll_vim_task(
            connector,
            target,
            operator,
            task=unwrap_vim_value(task_payload),
            timeout_seconds=_MARK_FLASH_TASK_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return {"disk_uuid": disk_uuid, "status": "error", "task": None, "error": str(exc)}
    if outcome.state == TASK_STATE_ERROR:
        return {
            "disk_uuid": disk_uuid,
            "status": "faulted",
            "task": outcome.task,
            "error": outcome.error_message or "<no fault reported>",
        }
    if outcome.timed_out:
        return {"disk_uuid": disk_uuid, "status": "timeout", "task": outcome.task, "error": None}
    return {"disk_uuid": disk_uuid, "status": "marked", "task": outcome.task, "error": None}


# ===========================================================================
# host.service_control
# ===========================================================================


async def service_control_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Start/stop/restart a bounded host service + optionally set its policy.

    Op-id: ``vmware.composite.host.service_control``. The service name is
    checked against the curated :data:`_SERVICE_ALLOWLIST` **first** -- an
    out-of-list name is refused with ``status="service_not_allowed"`` before
    any host resolution or write, never passed through to the vim method
    (the no-arbitrary-exec posture). Then resolves the host (name/moref) →
    its ``HostServiceSystem`` (config-manager read), applies the requested
    ``action`` (StartService/StopService/RestartService, synchronous
    204-No-Content), and -- when ``policy`` is supplied -- also
    ``UpdateServicePolicy``. Both writes flow through the governed vmomi
    seam; a parked/denied gate returns the :class:`OperationResult`
    verbatim. A vim fault (unknown service, config failure) propagates as a
    transport error the dispatcher wraps ``connector_error``.
    """
    service = params["service"]
    if service not in _SERVICE_ALLOWLIST:
        return {
            "status": "service_not_allowed",
            "host": params.get("host"),
            "service": service,
            "action": params.get("action"),
            "policy": params.get("policy"),
            "policy_updated": False,
            "allowed_services": sorted(_SERVICE_ALLOWLIST),
            "guidance": (
                f"service {service!r} is not in the curated host-service allowlist; "
                f"service_control is bounded to {sorted(_SERVICE_ALLOWLIST)} and never "
                "passes an arbitrary service name to the host"
            ),
        }

    host_moid, service_system_moid, refusal = await _resolve_host_and_manager(
        connector, target, operator, host=params.get("host"), prop=_PROP_CM_SERVICE_SYSTEM
    )
    if refusal is not None:
        return {**refusal, "service": service, "action": params["action"], "policy_updated": False}

    action = params["action"]
    action_op_id = _SERVICE_ACTION_OP_IDS[action]
    action_method = action_op_id.rsplit("/", 1)[-1]
    gate, _ = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=action_op_id,
        vmomi_path=f"/HostServiceSystem/{service_system_moid}/{action_method}",
        body={"id": service},
        params={"host": host_moid, "service": service, "action": action},
    )
    if gate is not None:
        return gate

    policy = params.get("policy")
    if policy is not None:
        gate, _ = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=_OP_UPDATE_SERVICE_POLICY,
            vmomi_path=f"/HostServiceSystem/{service_system_moid}/UpdateServicePolicy",
            body={"id": service, "policy": policy},
            params={"host": host_moid, "service": service, "policy": policy},
        )
        if gate is not None:
            return gate

    return {
        "status": "applied",
        "host": host_moid,
        "service": service,
        "action": action,
        "policy": policy,
        "policy_updated": policy is not None,
        "guidance": None,
    }

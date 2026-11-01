# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safety floor advertised by the VMware REST generic connector."""

from __future__ import annotations

import re

from meho_backplane.operations.ingest.safety_floors import register_ingest_safety_floor
from meho_backplane.operations.ingest.schemas import EndpointDescriptorProto

__all__ = [
    "VMWARE_APPLIANCE_AND_ACCESS_WRITES",
    "VMWARE_STOP_RESTART_AND_REMEDIATE_WRITES",
    "VMWARE_VIM_HOST_POWER_WRITES",
    "VMWARE_VIM_HOST_WAKES",
    "VMWARE_VIM_SNAPSHOT_CREATES",
    "VMWARE_VIM_VM_STATE_WRITES",
    "is_vmware_appliance_or_access_write",
    "is_vmware_host_maintenance_exit",
    "is_vmware_library_item_delete",
    "is_vmware_stop_restart_or_remediate_write",
    "is_vmware_tagging_removal",
    "is_vmware_vim_host_power_write",
    "is_vmware_vim_host_wake",
    "is_vmware_vim_snapshot_create",
    "is_vmware_vim_vm_state_write",
    "is_vmware_vm_relocate",
    "vmware_rest_safety_floor",
]

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Versioned catalogs whose ingested VM writes carry this floor. Both the 9.0
# (#3563/#3564) and the 8.0 U3 (#3569) generic catalogs share the same
# hardware-mutating VM write families, so the floor applies identically. The
# floor is registered once per ``(product, impl_id)``; this set scopes it to
# the qualified catalog versions rather than every future version.
_FLOORED_VERSIONS = frozenset({"8.0", "9.0"})


# The tagging removals are ``destructive`` (#3827): a category delete also
# removes every tag in it and detaches those tags from every object; a tag
# delete detaches it everywhere; a detach drops the association that
# governance (for example storage policy tags) relies on. ``destructive``
# means every seat waits for a second person, a standing grant can never
# satisfy the op, and the park needs a preview with the affected objects.
# The preview builders live in ``tagging_delete_preview.py``.
_TAG_ASSOCIATION_PATH = "/cis/tagging/tag-association"
_TAG_DETACH_ACTION_PREFIX = "action=detach"
_TAGGING_OBJECT_PATH_RE = re.compile(r"/cis/tagging/(category|tag)/\{[^/{}]+\}")

# A content-library item delete is ``destructive`` too (#4058): the item and
# its files are gone for good, like a deleted VM. The preview builder lives in
# ``library_item_delete_preview.py``; it reuses the planner of the governed
# ``vmware.composite.content_library.item.delete``.
_LIBRARY_ITEM_PATH_RE = re.compile(r"/content/library/item/\{[^/{}]+\}")

# A VM move that names a datastore copies the VM's disks (#4749). In a cold
# move the VM is off for the whole copy, and the copy fills the target
# datastore. The generic relocate (both forms) and the older-API method
# ``RelocateVM_Task`` are ``dangerous`` with approval, with or without a
# datastore in the call: the floor is on the operation, not on the request
# body. The preview builder in ``vm_relocate_preview.py`` shows the copy size.
_VM_PATH_RE = re.compile(r"/vcenter/vm/\{[^/{}]+\}")
_VM_RELOCATE_QUERIES = frozenset({"action=relocate", "action=relocate&vmw-task=true"})
_VIM_RELOCATE_PATH_RE = re.compile(r"/VirtualMachine/\{[^/{}]+\}/RelocateVM_Task")

# Ending maintenance mode on a host (#4967). The older-API method
# ``HostSystem.ExitMaintenanceMode_Task`` is ``dangerous`` with approval, like
# the typed ``vmware.composite.host.maintenance.exit`` and the mirror
# ``host.evacuate``: after it, DRS may move VMs onto the host and vSAN copies
# data back. Without this rule a person's generic call ran at once.
_VIM_EXIT_MAINTENANCE_PATH_RE = re.compile(r"/HostSystem/\{[^/{}]+\}/ExitMaintenanceMode_Task")

# Appliance, permission, session, certificate and host-image writes (#4973).
# They restore or restart vCenter, change who may do what, act as or log out
# another user, replace certificates, or apply a software image to hosts. As
# generic writes they were ``caution`` without approval, so a person's call ran
# at once. Each is ``dangerous`` with approval on both catalogs where the path
# exists. Entries are ``(method, path?query)`` without the ``/api`` mount;
# ``{}`` stands for any path parameter. The vSAN removals and the hybrid-link
# delete of the same Task need no entry here: the parser's delete-name rule
# (``meho_backplane/delete_names.py``) already makes them wait for approval.
VMWARE_APPLIANCE_AND_ACCESS_WRITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/appliance/recovery/restore/job"),
        ("POST", "/appliance/update/pending?action=rollback"),
        ("POST", "/appliance/update/pending/{}?action=stage-and-install"),
        ("POST", "/appliance/networking?action=change&vmw-task=true"),
        ("POST", "/appliance/networking?action=reset"),
        ("POST", "/AuthorizationManager/{}/SetEntityPermissions"),
        ("POST", "/AuthorizationManager/{}/ResetEntityPermissions"),
        ("POST", "/AuthorizationManager/{}/MergePermissions"),
        ("POST", "/vcenter/authorization/permissions"),
        ("PATCH", "/vcenter/authorization/permissions/{}"),
        ("POST", "/SessionManager/{}/ImpersonateUser"),
        ("POST", "/SessionManager/{}/TerminateSession"),
        ("PUT", "/vcenter/certificate-management/vcenter/tls"),
        ("POST", "/vcenter/certificate-management/vcenter/tls?action=replace-vmca-signed"),
        ("POST", "/vcenter/certificate-management/vcenter/vmca-root"),
        ("POST", "/esx/settings/inventory?action=apply&vmw-task=true"),
    }
)

# Calls that stop, restart, disconnect, remediate or open access (#4328): guest
# shutdown, reboot and standby; appliance power off and reboot; service stop
# and restart; SSH, shell, console and DCUI access; host disconnect; turning
# off a Supervisor; staging or applying a software image to a cluster or a
# host; installing an appliance update. As generic writes they were
# ``caution`` without approval, so a person's call ran at once. Each is
# ``dangerous`` with approval on the 8.0 and 9.0 catalogs. Same key format as
# :data:`VMWARE_APPLIANCE_AND_ACCESS_WRITES`; the query must match exactly.
VMWARE_STOP_RESTART_AND_REMEDIATE_WRITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/vcenter/vm/{}/guest/power?action=shutdown"),
        ("POST", "/vcenter/vm/{}/guest/power?action=reboot"),
        ("POST", "/vcenter/vm/{}/guest/power?action=standby"),
        ("POST", "/appliance/shutdown?action=poweroff"),
        ("POST", "/appliance/shutdown?action=reboot"),
        ("POST", "/appliance/services/{}?action=stop"),
        ("POST", "/appliance/services/{}?action=restart"),
        ("POST", "/vcenter/services/{}?action=stop"),
        ("POST", "/vcenter/services/{}?action=restart"),
        ("PUT", "/appliance/access/ssh"),
        ("PUT", "/appliance/access/shell"),
        ("PUT", "/appliance/access/consolecli"),
        ("PUT", "/appliance/access/dcui"),
        ("POST", "/vcenter/host/{}?action=disconnect"),
        ("POST", "/vcenter/namespace-management/clusters/{}?action=disable"),
        ("POST", "/esx/settings/clusters/{}/software?action=apply&vmw-task=true"),
        ("POST", "/esx/settings/clusters/{}/software?action=stage&vmw-task=true"),
        ("POST", "/esx/settings/hosts/{}/software?action=apply&vmw-task=true"),
        ("POST", "/esx/settings/hosts/{}/software?action=stage&vmw-task=true"),
        ("POST", "/appliance/update/pending/{}?action=install"),
    }
)

# VM power, terminate, clone, snapshot revert and migrate calls on the older
# API (VI-JSON, #3869). Each is a ``POST`` to ``/<Type>/{moId}/<Method>``, so
# the parser gave it ``caution`` without approval and a person's call ran at
# once. Each is ``dangerous`` with approval, like the typed ``vm.power``,
# ``vm.clone``, ``vm.snapshot.revert`` and ``vm.migrate`` and the generic
# ``/vcenter/vm/{vm}/power`` calls. ``RelocateVM_Task`` has its own rule above;
# the snapshot removals are deletes and the parser's delete-name rule already
# makes them wait.
VMWARE_VIM_VM_STATE_WRITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/VirtualMachine/{}/PowerOnVM_Task"),
        ("POST", "/VirtualMachine/{}/PowerOffVM_Task"),
        ("POST", "/VirtualMachine/{}/ResetVM_Task"),
        ("POST", "/VirtualMachine/{}/SuspendVM_Task"),
        ("POST", "/VirtualMachine/{}/ShutdownGuest"),
        ("POST", "/VirtualMachine/{}/RebootGuest"),
        ("POST", "/VirtualMachine/{}/StandbyGuest"),
        ("POST", "/VirtualMachine/{}/TerminateVM"),
        ("POST", "/VirtualMachine/{}/TerminateFaultTolerantVM_Task"),
        ("POST", "/VirtualMachine/{}/CloneVM_Task"),
        ("POST", "/VirtualMachine/{}/InstantClone_Task"),
        ("POST", "/VirtualMachine/{}/MigrateVM_Task"),
        ("POST", "/VirtualMachine/{}/RevertToCurrentSnapshot_Task"),
        ("POST", "/VirtualMachineSnapshot/{}/RevertToSnapshot_Task"),
        ("POST", "/Datacenter/{}/PowerOnMultiVM_Task"),
        ("POST", "/VirtualApp/{}/PowerOnVApp_Task"),
        ("POST", "/VirtualApp/{}/PowerOffVApp_Task"),
        ("POST", "/VirtualApp/{}/SuspendVApp_Task"),
        ("POST", "/VirtualApp/{}/CloneVApp_Task"),
        ("POST", "/HostAutoStartManager/{}/AutoStartPowerOn"),
        ("POST", "/HostAutoStartManager/{}/AutoStartPowerOff"),
    }
)

# Taking a VM snapshot on the older API (#3869) waits for approval but keeps
# its ``caution`` level, like the typed ``vm.snapshot.create``: a new snapshot
# changes nothing in the running VM.
VMWARE_VIM_SNAPSHOT_CREATES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/VirtualMachine/{}/CreateSnapshot_Task"),
        ("POST", "/VirtualMachine/{}/CreateSnapshotEx_Task"),
    }
)

# Host maintenance-mode start, reboot, shut down and standby on the older API
# (#4350). Each is ``dangerous`` with approval, like the typed
# ``host.maintenance.enter``, ``host.reboot``, ``host.shutdown`` and
# ``host.standby.enter``. Ending maintenance mode has its own rule above.
VMWARE_VIM_HOST_POWER_WRITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/HostSystem/{}/EnterMaintenanceMode_Task"),
        ("POST", "/HostSystem/{}/RebootHost_Task"),
        ("POST", "/HostSystem/{}/ShutdownHost_Task"),
        ("POST", "/HostSystem/{}/PowerDownHostToStandBy_Task"),
    }
)

# Waking a host from standby (#4350) waits for approval but keeps its
# ``caution`` level, like the typed ``host.standby.exit``.
VMWARE_VIM_HOST_WAKES: frozenset[tuple[str, str]] = frozenset(
    {("POST", "/HostSystem/{}/PowerUpHostFromStandBy_Task")}
)
_PATH_PARAM_RE = re.compile(r"\{[^/{}]+\}")


def _canonical(path: str) -> tuple[str, str]:
    """Split *path* into ``(path without the /api mount, query)``."""
    raw_path, _, query = path.partition("?")
    return raw_path.removeprefix("/api"), query


def _operation_key(method: str, path: str) -> tuple[str, str]:
    """Return ``(METHOD, path?query)`` without the /api mount and with ``{}`` path parameters."""
    canonical_path, query = _canonical(path)
    key = _PATH_PARAM_RE.sub("{}", canonical_path) + (f"?{query}" if query else "")
    return method.upper(), key


def _is_tag_detach(method: str, canonical_path: str, query: str) -> bool:
    """Identify the vCenter tag-association detach actions."""
    if method != "POST" or not query.startswith(_TAG_DETACH_ACTION_PREFIX):
        return False
    return canonical_path == _TAG_ASSOCIATION_PATH or canonical_path.startswith(
        _TAG_ASSOCIATION_PATH + "/"
    )


def is_vmware_tagging_removal(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` deletes a tag or category, or detaches a tag.

    Matches ``DELETE /cis/tagging/category/{id}``, ``DELETE /cis/tagging/tag/{id}``
    and the three ``POST /cis/tagging/tag-association[/{id}]?action=detach*``
    actions, with or without the ``/api`` mount in the path.
    """
    method = method.upper()
    canonical_path, query = _canonical(path)
    if method == "DELETE":
        return _TAGGING_OBJECT_PATH_RE.fullmatch(canonical_path) is not None
    return _is_tag_detach(method, canonical_path, query)


def is_vmware_library_item_delete(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is ``DELETE /content/library/item/{id}``.

    Matches with or without the ``/api`` mount in the path.
    """
    canonical_path, query = _canonical(path)
    return (
        method.upper() == "DELETE"
        and not query
        and _LIBRARY_ITEM_PATH_RE.fullmatch(canonical_path) is not None
    )


def is_vmware_vm_relocate(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is a VM move that can copy the VM's disks.

    Matches ``POST /vcenter/vm/{vm}?action=relocate``, the same with
    ``&vmw-task=true``, and ``POST /VirtualMachine/{moId}/RelocateVM_Task``,
    with or without the ``/api`` mount in the path.
    """
    if method.upper() != "POST":
        return False
    canonical_path, query = _canonical(path)
    if query in _VM_RELOCATE_QUERIES:
        return _VM_PATH_RE.fullmatch(canonical_path) is not None
    return not query and _VIM_RELOCATE_PATH_RE.fullmatch(canonical_path) is not None


def is_vmware_host_maintenance_exit(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is ``POST /HostSystem/{moId}/ExitMaintenanceMode_Task``.

    Matches with or without the ``/api`` mount in the path, with any
    spelling of the path parameter.
    """
    if method.upper() != "POST":
        return False
    canonical_path, query = _canonical(path)
    return not query and _VIM_EXIT_MAINTENANCE_PATH_RE.fullmatch(canonical_path) is not None


def is_vmware_appliance_or_access_write(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is in :data:`VMWARE_APPLIANCE_AND_ACCESS_WRITES`.

    Matches with or without the ``/api`` mount in the path and with any
    spelling of a path parameter. The query must match exactly, so
    ``POST /appliance/update/pending/{version}?action=stage`` does not count.
    """
    return _operation_key(method, path) in VMWARE_APPLIANCE_AND_ACCESS_WRITES


def is_vmware_stop_restart_or_remediate_write(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is in :data:`VMWARE_STOP_RESTART_AND_REMEDIATE_WRITES`.

    Matches like :func:`is_vmware_appliance_or_access_write`, so
    ``POST /appliance/shutdown?action=cancel`` and
    ``POST /vcenter/services/{service}?action=start`` do not count.
    """
    return _operation_key(method, path) in VMWARE_STOP_RESTART_AND_REMEDIATE_WRITES


def is_vmware_vim_vm_state_write(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is in :data:`VMWARE_VIM_VM_STATE_WRITES`.

    Matches with or without the ``/api`` mount in the path and with any
    spelling of the path parameter. A path with a query does not count.
    """
    return _operation_key(method, path) in VMWARE_VIM_VM_STATE_WRITES


def is_vmware_vim_snapshot_create(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is in :data:`VMWARE_VIM_SNAPSHOT_CREATES`.

    Matches like :func:`is_vmware_vim_vm_state_write`.
    """
    return _operation_key(method, path) in VMWARE_VIM_SNAPSHOT_CREATES


def is_vmware_vim_host_power_write(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is in :data:`VMWARE_VIM_HOST_POWER_WRITES`.

    Matches like :func:`is_vmware_vim_vm_state_write`.
    """
    return _operation_key(method, path) in VMWARE_VIM_HOST_POWER_WRITES


def is_vmware_vim_host_wake(method: str, path: str) -> bool:
    """Return whether ``(method, path)`` is in :data:`VMWARE_VIM_HOST_WAKES`.

    Matches like :func:`is_vmware_vim_vm_state_write`.
    """
    return _operation_key(method, path) in VMWARE_VIM_HOST_WAKES


def _requires_vmware_safety_floor(method: str, path: str) -> bool:
    """Identify VMware VM, host, appliance and access writes that require approval."""
    if method not in _WRITE_METHODS:
        return False
    if (
        is_vmware_vm_relocate(method, path)
        or is_vmware_host_maintenance_exit(method, path)
        or is_vmware_appliance_or_access_write(method, path)
        or is_vmware_stop_restart_or_remediate_write(method, path)
        or is_vmware_vim_vm_state_write(method, path)
        or is_vmware_vim_host_power_write(method, path)
    ):
        return True
    canonical_path, _query = _canonical(path)
    if canonical_path == "/vcenter/vm/{vm}/hardware":
        return True
    if canonical_path.startswith("/vcenter/vm/{vm}/hardware/"):
        return True
    if canonical_path.startswith("/vcenter/vm/{vm}/power/"):
        return True
    return (
        (method == "POST" and canonical_path == "/vcenter/vm")
        or (method == "DELETE" and canonical_path == "/vcenter/vm/{vm}")
        or canonical_path == "/vcenter/vm/{vm}/power"
    )


def vmware_rest_safety_floor(
    version: str, proto: EndpointDescriptorProto
) -> EndpointDescriptorProto:
    """Promote VM, tagging-removal, and library-item-delete writes for the VMware catalogs.

    The VM writes include the moves that copy disks (:func:`is_vmware_vm_relocate`)
    and the host maintenance-mode exit (:func:`is_vmware_host_maintenance_exit`).
    The appliance, permission, session, certificate and host-image writes are
    :func:`is_vmware_appliance_or_access_write`. The stop, restart,
    disconnect, remediate and access writes are
    :func:`is_vmware_stop_restart_or_remediate_write`. The older-API VM power,
    terminate, clone, snapshot revert and migrate calls are
    :func:`is_vmware_vim_vm_state_write`. The older-API host maintenance-mode
    start, reboot, shut down and standby are :func:`is_vmware_vim_host_power_write`.
    The older-API snapshot creates (:func:`is_vmware_vim_snapshot_create`) and
    the host wake from standby (:func:`is_vmware_vim_host_wake`) keep their
    level and wait for approval.
    """
    if version not in _FLOORED_VERSIONS:
        return proto
    method = proto.method.upper()
    if is_vmware_tagging_removal(method, proto.path) or is_vmware_library_item_delete(
        method, proto.path
    ):
        return proto.model_copy(update={"safety_level": "destructive", "requires_approval": True})
    if is_vmware_vim_snapshot_create(method, proto.path) or is_vmware_vim_host_wake(
        method, proto.path
    ):
        return proto.model_copy(update={"requires_approval": True})
    if not _requires_vmware_safety_floor(method, proto.path):
        return proto
    safety_level = proto.safety_level if proto.safety_level == "destructive" else "dangerous"
    return proto.model_copy(update={"safety_level": safety_level, "requires_approval": True})


register_ingest_safety_floor(
    product="vmware",
    impl_id="vmware-rest",
    floor=vmware_rest_safety_floor,
)

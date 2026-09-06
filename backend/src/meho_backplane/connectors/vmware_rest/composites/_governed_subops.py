# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Composite op_id -> governed child-op manifest map for discovery (#3349).

The single place that binds each write composite's op_id to the
``_SUB_OPS_*`` / ``_VIM_SUB_OPS_*`` manifest tuples it fans out to. The map
**references** those manifest constants (it never re-types the child op
ids), so it is derived from the manifests — the same tuples the ingest /
reconcile lanes pin — rather than a hand-maintained parallel list that could
drift. :func:`register_vmware_governed_subops` walks it at registration time
and feeds
:func:`meho_backplane.operations.governed_subops.register_governed_subops`,
which the ``GET /api/v1/operations/governed-subops`` surface reads so an
operator can assemble a composite's full grant set without reading source.

Only the **write** composites appear: their governed children park for a
service principal and therefore need a standing grant each. Read composites
(``_read.py``) auto-execute for a service principal and need no grant, and the
guest-ops composites (``_guest.py``) dispatch their child ops inline without a
declared manifest — neither has a grant set to discover here.

Rollback / delete legs are part of the manifests (e.g. ``_SUB_OPS_VM_CREATE``
carries ``DELETE:/vcenter/vm/{vm}``), so they flow through unchanged and the
discovery surface flags them un-grantable (delete-shaped) — telling the
operator a partial failure of that composite still needs a human.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _host, _write

#: Connector the vmware-rest composites dispatch against. The composites are
#: registered for vCenter 9.0 (``vmware-rest-9.0``); the discovery surface
#: uses it only to resolve a best-effort ``safety_level`` per child.
_VMWARE_REST_CONNECTOR_ID: Final[str] = "vmware-rest-9.0"

#: composite op_id -> the child op ids it may dispatch, concatenated from its
#: REST (``_SUB_OPS_*``) and vim (``_VIM_SUB_OPS_*``) manifests. Referenced,
#: never re-typed — this is the whole point of "derived from the manifests".
_GOVERNED_SUBOP_MANIFEST: Final[dict[str, tuple[str, ...]]] = {
    "vmware.composite.vm.create": _write._SUB_OPS_VM_CREATE + _write._VIM_SUB_OPS_VM_CREATE,
    "vmware.composite.vm.clone": _write._SUB_OPS_VM_CLONE,
    "vmware.composite.vm.deploy_from_library": _write._SUB_OPS_VM_DEPLOY_FROM_LIBRARY,
    "vmware.composite.vm.import_from_library": (
        _write._SUB_OPS_VM_IMPORT_FROM_LIBRARY + _write._VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY
    ),
    "vmware.composite.vm.snapshot.revert": _write._VIM_SUB_OPS_VM_SNAPSHOT_REVERT,
    "vmware.composite.vm.destroy": _write._VIM_SUB_OPS_VM_DESTROY,
    "vmware.composite.vm.migrate": _write._SUB_OPS_VM_MIGRATE + _write._VIM_SUB_OPS_VM_MIGRATE,
    "vmware.composite.vm.power.bulk": _write._SUB_OPS_VM_POWER_BULK,
    "vmware.composite.vm.power": _write._SUB_OPS_VM_POWER,
    "vmware.composite.vm.disk.grow": _write._VIM_SUB_OPS_VM_DISK_GROW,
    "vmware.composite.vm.disk.attach": _write._VIM_SUB_OPS_VM_DISK_ATTACH,
    "vmware.composite.vm.clone_from_template": _write._VIM_SUB_OPS_VM_CLONE_FROM_TEMPLATE,
    "vmware.composite.host.evacuate": (
        _write._SUB_OPS_HOST_EVACUATE + _write._VIM_SUB_OPS_HOST_EVACUATE
    ),
    "vmware.composite.host.detach_from_vds": (
        _write._SUB_OPS_HOST_DETACH_FROM_VDS + _write._VIM_SUB_OPS_HOST_DETACH_FROM_VDS
    ),
    "vmware.composite.network.portgroup.create": _write._VIM_SUB_OPS_NETWORK_PORTGROUP_CREATE,
    "vmware.composite.network.portgroup.security.set": (
        _write._VIM_SUB_OPS_NETWORK_PORTGROUP_SECURITY_SET
    ),
    "vmware.composite.cluster.patch": (
        _write._SUB_OPS_CLUSTER_PATCH + _write._VIM_SUB_OPS_CLUSTER_PATCH
    ),
    "vmware.composite.cluster.drs_rule.create": _write._VIM_SUB_OPS_CLUSTER_DRS_RULE_CREATE,
    "vmware.composite.folder.create": _write._VIM_SUB_OPS_FOLDER_CREATE,
    "vmware.composite.vm.resize": _write._SUB_OPS_VM_RESIZE,
    "vmware.composite.vm.nic.repoint": _write._SUB_OPS_VM_NIC_REPOINT,
    "vmware.composite.vm.device.cdrom": _write._SUB_OPS_VM_DEVICE_CDROM,
    "vmware.composite.guest.customization_spec.create": (
        _write._SUB_OPS_GUEST_CUSTOMIZATION_SPEC_CREATE
    ),
    "vmware.composite.vm.customize": _write._SUB_OPS_VM_CUSTOMIZE,
    "vmware.composite.host.datastore_mount_nfs": _host._VIM_SUB_OPS_HOST_DATASTORE_MOUNT_NFS,
    "vmware.composite.host.disk_mark_flash": _host._VIM_SUB_OPS_HOST_DISK_MARK_FLASH,
    "vmware.composite.host.service_control": _host._VIM_SUB_OPS_HOST_SERVICE_CONTROL,
}


def register_vmware_governed_subops() -> None:
    """Feed the composite→child-op map into the core discovery registry.

    Called once at composite-registration time (from
    ``register_vmware_composite_operations``). Idempotent — the core
    registry de-duplicates and overwrites-with-log on a changed payload.
    """
    from meho_backplane.operations.governed_subops import register_governed_subops

    for composite_op_id, sub_op_ids in _GOVERNED_SUBOP_MANIFEST.items():
        register_governed_subops(
            composite_op_id=composite_op_id,
            connector_id=_VMWARE_REST_CONNECTOR_ID,
            sub_op_ids=sub_op_ids,
        )

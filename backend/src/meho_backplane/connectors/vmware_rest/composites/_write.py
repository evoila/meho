# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: 19 protocol-driven composite handlers for the
# vSphere REST + VI-JSON write surface ship in one module per the issue
# body's design; splitting them by group would scatter the shared
# sub-op_id constants + helpers across files for no readability gain. Each
# handler's body is the documented orchestration workflow from #509's spec
# (plus the single-VM vm.power from #2301, the mutating VI-JSON disk-grow
# from #2893, the folder-template clone from #2894, the vim cluster /
# inventory writes — DRS-rule + folder create — from #2895, the #2891
# hardware writes — vm.resize / vm.nic.repoint / vm.device.cdrom, the
# GOSC create/apply from #2892, and the OVF/OVA content-library deploy
# from #2909).

"""Write-shaped ``vmware.composite.*`` handler functions (19 composites).

Companion to :mod:`._read`. Post-#2256 each handler is a module-level
``async def`` taking the dispatcher's composite-branch keyword args
``(operator, target, params, connector)`` -- the resolved connector
instance the #2251 substrate injects -- and issues every raw-REST sub-op
**directly on the connector's own authenticated session**
(``connector._get_json`` / ``connector._post_json`` mounted through
``connector.mount_op_path``) with no ``endpoint_descriptor`` lookup, so
the composite works on a fresh boot with **zero catalog ingest**
(Initiative #2249 / Goal #2247, the I-A write migration).

The one exception is :func:`host_evacuate_composite`, which additionally
declares ``dispatch_child`` for its recursive call into
``vmware.composite.vm.migrate`` -- a ``source_kind="composite"`` sub-op
routed through a registrar-guaranteed row (never an ingested primitive),
so that recursion keeps its ``dispatch_child`` path per #2248.

Preserving write governance on the direct path
----------------------------------------------

``dispatch_child`` re-ran the dispatcher's per-sub-op policy/approval
gate (property 3 of #508's four guarantees); a direct session call
bypasses :func:`~meho_backplane.operations.dispatcher.dispatch`, so a
now-internal *write* sub-op would otherwise execute un-gated. Every
mutating sub-call therefore routes through
:func:`~meho_backplane.operations.composite.enforce_subop_policy`
(Task #2254) **before** the direct ``connector._post_json`` fires: the
seam re-runs the same ``policy_gate`` against an in-memory descriptor
carrying the sub-op's declared governance and returns an
``awaiting_approval`` / ``denied`` :class:`OperationResult` when the gate
does not clear. The handler returns that verbatim -- the dispatcher
passes a handler-returned :class:`OperationResult` straight through, so an
internal write **queues** (or is denied) instead of silently running.

Sub-op governance posture
-------------------------

Each write sub-op declares ``safety_level="dangerous"`` +
``requires_approval=False``. The ``dangerous`` label is the honest
intrinsic-risk classification (create / delete / power / relocate /
patch / maintenance are all state-mutating); ``requires_approval=False``
keeps the **top-level composite** (``requires_approval=True`` in
:mod:`._register`) the single primary approval gate. Flooring a sub-op to
``requires_approval=True`` would double-gate: the approval-resume path
re-runs the handler with the top-level gate already satisfied, but
:func:`enforce_subop_policy` is not resume-aware, so it would re-queue the
first internal write forever. With ``requires_approval=False`` the seam
still (a) auto-executes for a human/service operator whose composite was
already approved, and (b) denies -- or, with an explicit
per-``(principal, op, target)`` grant, queues -- a ``dangerous`` write for
an agent principal, so no internal write drops below the governance it had
under ``dispatch_child``.

Every sub-op_id below is the canonical ``METHOD:/path`` key produced
by :func:`~meho_backplane.operations.ingest.openapi.parse_openapi`
from the ingested ``vcenter.yaml`` (G3.1-T2 / #408) and
``vi-json.yaml`` (G3.1-T3 / #503). The path strings come from
inspecting the canonical ``GOVC_PARITY_BENCHMARK`` tuple at
``backend/tests/acceptance/test_g07_vsphere_canary.py`` and the
vSphere REST URL anchors in #509's issue body -- never guessed. Post-#2256
they no longer resolve an ``endpoint_descriptor`` row; each handler splits
the ``METHOD:/path`` into its verb + spec-relative path, substitutes the
``{var}`` path params, and mounts the remainder onto the target's live
``/api`` (modern) / ``/rest`` (legacy/vcsim) prefix for the direct call
(see :func:`_read_sub_op` / :func:`_write_sub_op`).

Each composite returns a structured ``{"status": ...}`` envelope so
callers can branch on ``status`` without parsing free-form prose. The
status enums are listed on each composite's ``response_schema`` in
:mod:`.schemas`.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest.vim_body import (
    VIM_TYPE_NAME_KEY,
    retrieve_properties_body,
    unwrap_vim_value,
    vim_moref,
)
from meho_backplane.connectors.vmware_rest.vim_task import TASK_STATE_ERROR, poll_vim_task
from meho_backplane.operations.composite import DispatchChild, enforce_subop_policy

if TYPE_CHECKING:
    from meho_backplane.connectors.vmware_rest.connector import VmwareRestConnector

__all__ = [
    "cluster_drs_rule_create_composite",
    "cluster_patch_composite",
    "folder_create_composite",
    "guest_customization_spec_create_composite",
    "host_detach_from_vds_composite",
    "host_evacuate_composite",
    "vm_clone_composite",
    "vm_clone_from_template_composite",
    "vm_create_composite",
    "vm_customize_composite",
    "vm_deploy_from_library_composite",
    "vm_device_cdrom_composite",
    "vm_disk_grow_composite",
    "vm_migrate_composite",
    "vm_nic_repoint_composite",
    "vm_power_bulk_composite",
    "vm_power_composite",
    "vm_resize_composite",
    "vm_snapshot_revert_composite",
]


# Connector_id every write composite governs its sub-ops against (fed to
# :func:`enforce_subop_policy` for the in-memory descriptor + the
# ``vmware.composite.vm.migrate`` recursion routed through ``dispatch_child``).
_CONNECTOR_ID = "vmware-rest-9.0"

# Declared governance for every raw-REST *write* sub-op. ``dangerous`` is
# the intrinsic-risk label; ``requires_approval=False`` keeps the top-level
# composite the single approval gate (see the module docstring on why
# flooring a sub-op to True would double-gate the resume path).
_WRITE_SAFETY_LEVEL = "dangerous"
_WRITE_REQUIRES_APPROVAL = False

# ``{var}`` path-template placeholder pattern. vCenter moids are bare
# ``[A-Za-z0-9-]`` tokens, so a plain ``str.format`` matches the RFC6570
# simple-expansion the ingested path did.
_PATH_VAR_RE = re.compile(r"\{([^{}]+)\}")

# vCenter REST op_ids (canonical METHOD:/path keys from vcenter.yaml).
#
# Action-bearing endpoints. vCenter's OpenAPI spec models POST-with-side-effect
# endpoints as ``/<path>?action=<verb>`` — i.e. the action verb is part of the
# path key, not a body parameter. The canonical ``op_id`` for "power on a VM" is
# ``POST:/vcenter/vm/{vm}/power?action=start``; the ``?action=<verb>`` rides on
# the mounted path verbatim (httpx sends it as the request query string), so no
# ``action`` body param is ever constructed. Endpoints whose action verb is
# operator-chosen (power start/stop) build the op_id per-call via
# :func:`_power_vm_op_id`.
_OP_LIST_FOLDERS = "GET:/vcenter/folder"
_OP_LIST_VMS = "GET:/vcenter/vm"
_OP_GET_VM = "GET:/vcenter/vm/{vm}"
_OP_CREATE_VM = "POST:/vcenter/vm"
_OP_DELETE_VM = "DELETE:/vcenter/vm/{vm}"
# NIC attach on vm.create is the Ethernet adapter *create* resource. The
# pinned spec serves no ``PATCH:/vcenter/vm/{vm}/network`` (the #2970
# reconcile finding); attaching a NIC to a network is
# ``Vcenter.Vm.Hardware.Ethernet_create`` with the network in the
# ``backing`` spec.
_OP_CREATE_VM_NIC = "POST:/vcenter/vm/{vm}/hardware/ethernet"
# Per-VM adapter listing for host.detach_from_vds's NIC repoint fan-out
# (``Vcenter.Vm.Hardware.Ethernet_list``).
_OP_LIST_VM_NICS = "GET:/vcenter/vm/{vm}/hardware/ethernet"
_OP_RELOCATE_VM = "POST:/vcenter/vm/{vm}?action=relocate"
# Host listing (``Vcenter.Host_list``). The pinned spec serves no
# per-cluster ``GET:/vcenter/cluster/{cluster}/host`` (#2970); cluster
# scoping rides the ``Host.FilterSpec.clusters`` query filter instead.
_OP_LIST_HOSTS = "GET:/vcenter/host"
# Content-library template deploy. The pinned spec keys the deploy on the
# template item as a *path* param
# (``Vcenter.VmTemplate.LibraryItems_deploy``) and the operation is
# synchronous -- its 200 body is the deployed VM id, NOT a cis task
# (#2970; there is no ``vmw-task=true`` variant of this path).
_OP_DEPLOY_LIBRARY_VM = (
    "POST:/vcenter/vm-template/library-items/{templateLibraryItem}?action=deploy"
)
# OVF/OVA content-library deploy (``vm.deploy_from_library`` / #2909). The
# pinned spec keys the deploy on the OVF item as a *path* param
# (``Vcenter.Ovf.LibraryItem_deploy``); like the VMTX deploy it is
# synchronous, but its 200 body is a ``DeploymentResult`` structure
# (``succeeded`` / ``resource_id`` / ``error``) — a deploy that fails OVF /
# placement / network validation returns ``succeeded=false`` with an error
# report rather than raising, so the composite surfaces it as a structured
# status. Name-based item resolution rides the content-library find actions
# (both served by the pinned ``vcenter.yaml``): items via
# ``Content.Library.Item_find`` (filtered to ``type=ovf``), the optional
# scoping library via ``Content.Library_find`` — both return a bare array of
# id strings and mutate nothing, so they run un-gated like the REST listing
# reads.
_OP_DEPLOY_OVF_LIBRARY_ITEM = "POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy"
# Per-request timeout for the two *synchronous* library-item deploys
# (``_OP_DEPLOY_LIBRARY_VM`` / ``_OP_DEPLOY_OVF_LIBRARY_ITEM``). Both hold the
# POST connection open for the entire multi-GB disk copy with no
# ``vmw-task=true`` variant to poll (#2970), so a real appliance deploy runs
# well past the connector's 30s client-default read timeout and used to fault
# with a false ``deploy_error`` while vCenter finished the copy server-side
# (#3076). This override grants only these two calls a generous read/write
# ceiling (30 min); connect/pool stay at the fast client default so a dead
# target still fails fast, and every *other* call on the connector keeps the
# unchanged 30s default (raising the global client timeout would make ordinary
# reads hang on a dead target).
_LIBRARY_ITEM_DEPLOY_TIMEOUT = httpx.Timeout(connect=5.0, read=1800.0, write=1800.0, pool=5.0)
_OP_FIND_LIBRARY = "POST:/content/library?action=find"
_OP_FIND_LIBRARY_ITEM = "POST:/content/library/item?action=find"
# Content-library item type discriminator for OVF/OVA templates — the find
# filter that keeps a colliding non-OVF item name (ISO, other) from matching.
_OVF_LIBRARY_ITEM_TYPE = "ovf"
# ``Vcenter.Ovf.OvfParams`` subtype discriminator for injected OVF
# product-section properties. The pinned 9.0 ``/api`` schema keys the union on
# ``type`` (the legacy ``/rest`` ``@class`` form is not used on the modern
# mount); a single ``PropertyParams`` entry carries the operator's
# ``ovf_properties`` map as ``{id, value}`` rows.
_OVF_PROPERTY_PARAMS_TYPE = "PropertyParams"
_OP_GET_TASK = "GET:/cis/tasks/{task}"
# Per-host vLCM remediation (``Esx.Settings.Hosts.Software_apply$Task``).
# The pinned spec serves no ``POST:/vcenter/host/{host}?action=patch``
# (#2970); patching a host is the vLCM apply, whose 202 body is the cis
# task id the composite polls via ``_OP_GET_TASK``.
_OP_HOST_SOFTWARE_APPLY = "POST:/esx/settings/hosts/{host}/software?action=apply&vmw-task=true"
# There is NO dedicated ``distributed-portgroup(s)`` list resource in the
# REST Automation API: distributed portgroups are enumerated via the
# generic network resource filtered to ``DISTRIBUTED_PORTGROUP`` (the
# #1602 reconciliation lesson -- the singular ``distributed-portgroup``
# op_id #509 declared here was absent from the pinned spec). Each summary
# row is ``{network (id), name, type}``. Mirrors ``_read._OP_LIST_NETWORK``.
_OP_LIST_NETWORK = "GET:/vcenter/network"
_NETWORK_TYPE_DISTRIBUTED_PORTGROUP = "DISTRIBUTED_PORTGROUP"
# ``Vcenter.Vm.Hardware.Ethernet.BackingSpec.type`` value for a standard
# portgroup -- the default backing the NIC create / repoint specs target.
_NIC_BACKING_STANDARD_PORTGROUP = "STANDARD_PORTGROUP"
# ``Ethernet.BackingSpec.type`` value for a distributed portgroup — on the
# pre-9.0 vim create arm (#3099) this backing maps to
# ``VirtualEthernetCardDistributedVirtualPortBackingInfo``.
_NIC_BACKING_DISTRIBUTED_PORTGROUP = "DISTRIBUTED_PORTGROUP"
# REST Datastore.Info read — the pre-9.0 vim create arm (#3099) resolves the
# datastore display name off this to build the vim
# ``files.vmPathName = "[<name>] <vm>"`` home path.
_OP_GET_DATASTORE = "GET:/vcenter/datastore/{datastore}"
# #3115 folder-lookup datacenter-scoping reads. ``GET:/vcenter/folder``
# matches display names across every datacenter — and each datacenter ships
# a default VM folder named ``vm`` — so a multi-match resolution re-scopes
# via the placement pins' datacenter: the datacenter listing enumerates the
# candidate datacenters, and one identity+``datacenters`` intersection
# listing per datacenter reverse-maps a pin (host / resource pool /
# datastore) to the datacenter that owns it (no listing row carries a
# parent-datacenter key, so the intersection filter is the REST-native
# reverse map). All three listings are pinned-spec-served
# (``Vcenter.Datacenter_list`` / ``Vcenter.ResourcePool_list`` /
# ``Vcenter.Datastore_list``); the host probe rides ``_OP_LIST_HOSTS``.
_OP_LIST_DATACENTERS = "GET:/vcenter/datacenter"
_OP_LIST_RESOURCE_POOLS = "GET:/vcenter/resource-pool"
_OP_LIST_DATASTORES = "GET:/vcenter/datastore"
# REST Disk.Info read — the disk-grow park-time preview reads label +
# current capacity (bytes) off this; the disk id is the vim device key.
_OP_GET_VM_DISK = "GET:/vcenter/vm/{vm}/hardware/disk/{disk}"
# REST Cluster.Info read — the drs_rule park-time preview reads the
# cluster's display name off this (``Vcenter.Cluster.Info.name``); cosmetic,
# best-effort. ``/vcenter/cluster/{cluster}`` is the get half of the
# cluster module's list/get/evc-mode surface (spec-verified).
_OP_GET_CLUSTER = "GET:/vcenter/cluster/{cluster}"

# vim (VI-JSON) op_ids for the disk-grow write path. These are the
# canonical ``METHOD:/path`` keys the ingest parser emits from
# ``vi-json.yaml`` (the moId rides the path as ``{moId}``, mirroring the
# read composites' ``POST:/EventManager/{moId}/QueryEvents``). They are
# the *governance* op_ids fed to :func:`enforce_subop_policy`; the
# concrete path (moId substituted) is what
# :meth:`VmwareRestConnector._post_vmomi_json` POSTs to. Kept out of the
# ``_SUB_OPS_*`` namespace so the vCenter-REST ingest-reconcile sweep does
# not treat a vi-json path as a ``vcenter.yaml`` row; the pinned
# ``vi-json.yaml`` reconcile asserts them instead.
#
# ``ReconfigVM_Task`` is the only route to change a virtual disk's
# capacity: the pinned 9.0 REST spec's ``Disk.UpdateSpec`` carries only
# ``backing`` (no capacity field), so vim is the sole write path
# (spec-verified). ``RetrievePropertiesEx`` reads the VM's
# ``config.hardware.device`` to obtain the full ``VirtualDisk`` device
# (its ``key`` + current ``capacityInBytes``) the reconfigure edits, and
# is re-read by the shared Task-poll helper to drive the returned
# ``*_Task`` MoRef to a terminal state.
# Canonical vi-json.yaml path keys (moId as the ``{moId}`` template — the
# form the ingest parser emits and the pinned-spec reconcile asserts).
_OP_RECONFIG_VM_TASK = "POST:/VirtualMachine/{moId}/ReconfigVM_Task"
_OP_RETRIEVE_PROPERTIES = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
# Concrete runtime path for the config read: the PropertyCollector is a
# singleton whose moId is the literal ``propertyCollector`` (the same
# concrete path the typed reads + read composites POST). ``ReconfigVM_Task``
# substitutes the VM moid inline at call time.
_VMOMI_RETRIEVE_PROPERTIES_PATH = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"

#: vi-json sub-op manifest for ``vm.disk.grow`` (parallel to the REST
#: ``_SUB_OPS_*`` tuples, but named out of that namespace so the
#: vcenter.yaml ingest-reconcile sweep skips it). The pinned
#: ``vi-json.yaml`` reconcile lane introspects this to assert every
#: declared vim path exists in the spec.
_VIM_SUB_OPS_VM_DISK_GROW: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIG_VM_TASK,
)

# VI-JSON polymorphic-type discriminator (``Any._typeName`` in
# vi-json.yaml) + the VirtualDisk data-object type name. A device read
# from ``config.hardware.device`` carries ``_typeName`` so the handler can
# pick the VirtualDisk out of the mixed device list. Request-side, every
# DataObject in a vim body carries the tag too (#3103; see
# :mod:`..vim_body` for the live-differential grounding).
_VMOMI_TYPE_NAME_KEY = VIM_TYPE_NAME_KEY
_VIRTUAL_DISK_TYPE = "VirtualDisk"
_VIRTUAL_MACHINE_MO_TYPE = "VirtualMachine"
_PROP_CONFIG_HARDWARE_DEVICE = "config.hardware.device"

# Default wall-clock bound for the ReconfigVM_Task poll — mirrors the 600s
# ``vm.clone`` convention.
_DISK_GROW_TASK_TIMEOUT_SECONDS = 600.0

# vm.create's optional VHV leg (#3093) rides the vim substrate:
# ``VirtualMachineConfigSpec.nestedHVEnabled`` has no REST expression (the
# pinned ``vcenter.yaml`` serves no ``hardware_virtualization`` / ``nestedHV``
# spelling anywhere, pinned by the #3087 recipe lanes) and *raw* VI-JSON
# dispatch mounts on ``/api`` — a 9.x-fleet accommodation that 404s on
# vCenter 8.0.x (#2466) — so the composites' vim substrate
# (:func:`_write_vmomi_sub_op` → ``_post_vmomi_json`` on the documented
# ``/sdk/vim25/{release}`` base) is the only cross-version governed path to
# the flag. ``RetrievePropertiesEx`` is the shared Task-poll read.
#
# ``Folder.CreateVM_Task`` (#3099) is the create itself on pre-9.0 targets:
# bare REST ``POST /api/vcenter/vm`` is vendor-defective on vCenter 8.0.x —
# an opaque ``500 UNABLE_TO_ALLOCATE_RESOURCE {messages:[]}`` for every
# spec shape and placement (proven by live controlled probes), while the
# identical create through the vim surface succeeds. When the live
# ``about.version`` major is < 9 the whole create rides this task-polled
# vim path, with the SCSI controller, disks (#3117), NICs and the VHV flag
# folded into the one ``VirtualMachineConfigSpec`` (collapsing the #3093
# second call on that arm); 9.0+ and unresolved versions keep the REST
# create byte-identical.
_OP_CREATE_VM_TASK = "POST:/Folder/{moId}/CreateVM_Task"
_VIM_SUB_OPS_VM_CREATE: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIG_VM_TASK,
    _OP_CREATE_VM_TASK,
)

# Default wall-clock bound for the VHV ReconfigVM_Task poll — the 600s
# convention; module-global so tests can zero it.
_VM_CREATE_VHV_TASK_TIMEOUT_SECONDS = 600.0

# Default wall-clock bound for the pre-9.0 CreateVM_Task poll (#3099) —
# same 600s convention; module-global so tests can zero it.
_VM_CREATE_TASK_TIMEOUT_SECONDS = 600.0

#: REST ``Vcenter.Vm.GuestOS`` enum → vim ``VirtualMachineGuestOsIdentifier``
#: for the pre-9.0 CreateVM_Task arm (#3099). Spec-grounded on both sides:
#: every key is an enum value of the pinned ``vcenter.yaml``'s
#: ``Vcenter.Vm.GuestOS`` and every value an enum value of the pinned
#: ``vi-json.yaml``'s ``VirtualMachineGuestOsIdentifier_enum`` with the same
#: per-value description label (e.g. ``VMKERNEL_8`` / ``vmkernel8Guest`` are
#: both documented "VMware ESX 8"); the reconcile lane asserts both sides.
#: Deliberately curated, not exhaustive: the ``VMKERNEL_*`` family (the
#: nested-ESXi recipe, #3087) plus the common Linux / Windows / catch-all
#: identifiers. An unmapped enum fails closed with a structured
#: ``rolled_back`` before any sub-call — never a silent guess.
_VIM_GUEST_ID_BY_REST_GUEST_OS: Final[dict[str, str]] = {
    "VMKERNEL": "vmkernelGuest",
    "VMKERNEL_5": "vmkernel5Guest",
    "VMKERNEL_6": "vmkernel6Guest",
    "VMKERNEL_65": "vmkernel65Guest",
    "VMKERNEL_7": "vmkernel7Guest",
    "VMKERNEL_8": "vmkernel8Guest",
    "VMKERNEL_9": "vmkernel9Guest",
    "OTHER": "otherGuest",
    "OTHER_64": "otherGuest64",
    "OTHER_LINUX": "otherLinuxGuest",
    "OTHER_LINUX_64": "otherLinux64Guest",
    "UBUNTU": "ubuntuGuest",
    "UBUNTU_64": "ubuntu64Guest",
    "DEBIAN_12": "debian12Guest",
    "DEBIAN_12_64": "debian12_64Guest",
    "RHEL_8_64": "rhel8_64Guest",
    "RHEL_9_64": "rhel9_64Guest",
    "CENTOS_8_64": "centos8_64Guest",
    "CENTOS_9_64": "centos9_64Guest",
    "VMWARE_PHOTON_64": "vmwarePhoton64Guest",
    "WINDOWS_SERVER_2019": "windows2019srv_64Guest",
    "WINDOWS_SERVER_2021": "windows2019srvNext_64Guest",
    "WINDOWS_11_64": "windows11_64Guest",
}

# vim NIC shape for the pre-9.0 create arm (#3099). The ``device`` is
# declared as the base ``VirtualDevice`` and the ``backing`` as the base
# ``VirtualDeviceBackingInfo``, so both carry their concrete-subtype
# ``_typeName`` for the vim25-JSON binding to pick the subtype — and since
# #3103 every enclosing DataObject (the ``VirtualDeviceConfigSpec`` entry,
# the ConfigSpec, its ``files``) carries its own tag as well: the live
# 8.0.3 differential disproved the earlier "declared type == runtime type
# needs no ``_typeName``" premise. The DVPG backing's ``port`` is a
# ``DistributedVirtualSwitchPortConnection`` whose ``switchUuid`` /
# ``portgroupKey`` are resolved from the portgroup moid via the un-gated
# vmomi property reads below — the same ``key`` +
# ``config.distributedVirtualSwitch`` → ``uuid`` walk govc performs.
_VIRTUAL_VMXNET3_TYPE = "VirtualVmxnet3"
_DV_PORT_BACKING_TYPE = "VirtualEthernetCardDistributedVirtualPortBackingInfo"
_STANDARD_NETWORK_BACKING_TYPE = "VirtualEthernetCardNetworkBackingInfo"
_DV_PORT_CONNECTION_TYPE = "DistributedVirtualSwitchPortConnection"

# vim storage shape for the pre-9.0 create arm (#3117). ``Folder.CreateVM_Task``
# builds an *empty* VM from the ConfigSpec verbatim — unlike REST
# ``POST:/vcenter/vm``, which fabricates a guest-default disk controller — so a
# vim-arm VM lands with **no SCSI controller** unless the ConfigSpec folds one
# in, and the documented governed disk-add
# (``POST:/vcenter/vm/{vm}/hardware/disk``) then 500s
# ``UNABLE_TO_ALLOCATE_RESOURCE`` for lack of a free controller slot. The arm
# therefore always folds a single ``VirtualLsiLogicSASController`` into the
# create (the issue's minimum ask), and each requested disk is a
# ``VirtualDisk`` device-add with ``fileOperation: create`` bound to that
# controller (the preferred ask). Shapes are the canonical govmomi
# ``CreateDisk`` / ``CreateSCSIController`` form: LSI Logic SAS is the
# universally-bootable default (ESXi / Linux / Windows guests), the disk
# backing is ``VirtualDiskFlatVer2BackingInfo`` with an **empty** ``fileName``
# so vCenter auto-generates a unique path inside the VM home
# (``files.vmPathName``), ``diskMode: persistent`` and ``thinProvisioned:
# true``. Every DataObject carries its ``_typeName`` (#3103).
_VIRTUAL_LSILOGIC_SAS_CONTROLLER_TYPE = "VirtualLsiLogicSASController"
_VIRTUAL_DISK_FLAT_BACKING_TYPE = "VirtualDiskFlatVer2BackingInfo"
# ``VirtualSCSISharing`` enum value: a private (non-shared) SCSI bus.
_VIM_SCSI_NO_SHARING = "noSharing"
_VIM_DISK_MODE_PERSISTENT = "persistent"
# New-device temp keys are negative (the vim new-device convention); the folded
# controller and disks sit in a distinct band from the NIC keys (``-1``… above)
# so no add collides. The controller is ``-100``; disks count down from
# ``-200``.
_VIM_SCSI_CONTROLLER_KEY = -100
_VIM_DISK_KEY_BASE = -200
# SCSI reserves unit 7 for the controller itself; guest disks skip it.
_VIM_SCSI_CONTROLLER_UNIT = 7
# GiB → bytes for the ``capacity_gb`` disk param (``VirtualDisk.capacityInBytes``
# is ``xsd:long`` bytes, the non-deprecated capacity field).
_GIB_IN_BYTES = 1024**3
# ``Vcenter.Vm.Hardware.Disk.CreateSpec.type`` value threaded through the REST
# arm's ``CreateSpec.disks`` — a SCSI-attached ``new_vmdk`` (matches the
# governed raw disk-add's ``{"type": "SCSI", "new_vmdk": {...}}`` shape).
_REST_DISK_TYPE_SCSI = "SCSI"

# vim data-object ``_typeName`` discriminators for the request-body specs
# the write composites assemble by hand (#3103, all spec-verified against
# the pinned ``vi-json.yaml``): the ConfigSpec rides ``ReconfigVMRequestType
# .spec`` / ``CreateVMRequestType.config``, ``files`` is its
# ``VirtualMachineFileInfo``, ``deviceChange`` entries are
# ``VirtualDeviceConfigSpec``, the clone body is ``CloneVMRequestType.spec``
# (``VirtualMachineCloneSpec``) with a ``VirtualMachineRelocateSpec``
# ``location``, and the DVS detach spec is ``ReconfigureDvsRequestType.spec``
# (``DVSConfigSpec``) with ``DistributedVirtualSwitchHostMemberConfigSpec``
# member entries.
_VM_CONFIG_SPEC_TYPE = "VirtualMachineConfigSpec"
_VM_FILE_INFO_TYPE = "VirtualMachineFileInfo"
_VIRTUAL_DEVICE_CONFIG_SPEC_TYPE = "VirtualDeviceConfigSpec"
_VM_CLONE_SPEC_TYPE = "VirtualMachineCloneSpec"
_VM_RELOCATE_SPEC_TYPE = "VirtualMachineRelocateSpec"
_DVS_CONFIG_SPEC_TYPE = "DVSConfigSpec"
_DVS_HOST_MEMBER_CONFIG_SPEC_TYPE = "DistributedVirtualSwitchHostMemberConfigSpec"
_DVPG_MO_TYPE = "DistributedVirtualPortgroup"
_PROP_DVPG_KEY = "key"
_PROP_DVPG_DVS = "config.distributedVirtualSwitch"
_PROP_DVS_UUID = "uuid"

# The one-field ``VirtualMachineConfigSpec`` the VHV reconfigure sends. The
# ``"spec"`` key is the vim request type's genuine required parameter (the
# legacy-envelope sweep #2973 does not apply to vim bodies); the spec value
# is a DataObject, so it carries its ``_typeName`` (#3103).
_NESTED_HV_RECONFIG_BODY: Final[dict[str, Any]] = {
    "spec": {VIM_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE, "nestedHVEnabled": True}
}

# vim (VI-JSON) op_ids for the folder-template clone write path (#2894).
# Same ``METHOD:/path`` shape the ingest parser emits from ``vi-json.yaml``
# (moId rides the path as ``{moId}``); kept out of the ``_SUB_OPS_*``
# namespace so the vcenter.yaml reconcile sweep skips them (the pinned
# ``vi-json.yaml`` reconcile lane asserts them instead). ``CloneVM_Task`` is
# the canonical folder-template deploy API (what govc/terraform use) — the
# REST ``vm.clone`` content-library path cannot deploy a marked-as-template
# folder VM, and only the vim path supports inline customization at clone
# time. ``GetCustomizationSpec`` resolves a stored GOSC spec (by name) to the
# full ``CustomizationSpec`` embedded inline in the clone; the config read
# (``RetrievePropertiesEx``, shared with disk-grow) asserts the source is a
# template.
_OP_CLONE_VM_TASK = "POST:/VirtualMachine/{moId}/CloneVM_Task"
_OP_GET_CUSTOMIZATION_SPEC = "POST:/CustomizationSpecManager/{moId}/GetCustomizationSpec"

#: vi-json sub-op manifest for ``vm.clone_from_template`` (parallel to
#: ``_VIM_SUB_OPS_VM_DISK_GROW``; named out of the ``_SUB_OPS_*`` namespace so
#: the vcenter.yaml sweep skips it). The pinned ``vi-json.yaml`` reconcile
#: lane introspects this to assert every declared vim path exists in the spec.
_VIM_SUB_OPS_VM_CLONE_FROM_TEMPLATE: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_GET_CUSTOMIZATION_SPEC,
    _OP_CLONE_VM_TASK,
)

# vim ManagedObjectReference type discriminators for the clone placement
# MoRefs. A request-side vim MoRef serialises as ``{"_typeName":
# "ManagedObjectReference", "type": <T>, "value": <moid>}`` (#3103) — the
# handler wraps the operator-supplied placement moids into these.
_FOLDER_MO_TYPE = "Folder"
_RESOURCE_POOL_MO_TYPE = "ResourcePool"
_HOST_SYSTEM_MO_TYPE = "HostSystem"
_DATASTORE_MO_TYPE = "Datastore"

# The vim property the template assert reads off the resolved source VM.
_PROP_CONFIG_TEMPLATE = "config.template"

# Standard ``ServiceContent.customizationSpecManager`` singleton moid;
# overridable via the ``customization_spec_manager_moid`` param (mirrors the
# performance composite's ``perf_manager_moid`` default of ``"PerfMgr"``).
_DEFAULT_CUSTOMIZATION_SPEC_MANAGER_MOID = "CustomizationSpecManager"

# Default wall-clock bound for the CloneVM_Task poll — mirrors the 600s
# ``vm.clone`` convention.
_CLONE_FROM_TEMPLATE_TASK_TIMEOUT_SECONDS = 600.0

# vim (VI-JSON) op_ids for the #2895 cluster / inventory writes. Same
# governance-op_id discipline as the disk-grow vim constants: the
# ``METHOD:/path`` key the ingest parser emits from ``vi-json.yaml`` is the
# key fed to :func:`enforce_subop_policy`; the concrete path (moId
# substituted) is what :meth:`VmwareRestConnector._post_vmomi_json` POSTs.
# Kept out of the ``_SUB_OPS_*`` namespace so the vCenter-REST
# ingest-reconcile sweep does not treat a vi-json path as a ``vcenter.yaml``
# row; the pinned ``vi-json.yaml`` reconcile asserts them instead.
#
# ``ReconfigureComputeResource_Task`` is the only route to add a DRS
# affinity / anti-affinity rule by explicit VM list: no cluster-rules REST
# path exists and the tag-based compute-policies surface is semantically
# wrong (tag-scoped, spec-verified). It rides the pinned spec's
# ``ClusterConfigSpecEx.rulesSpec`` delta with ``modify=true`` and returns a
# ``*_Task`` MoRef to poll. ``Folder.CreateFolder`` is the only route to
# create a VM folder (``/vcenter/folder`` is GET-only, spec-verified) and is
# **synchronous** — it returns the new Folder MoRef directly, NOT a Task, so
# it is never polled.
_OP_RECONFIGURE_COMPUTE_RESOURCE_TASK = (
    "POST:/ClusterComputeResource/{moId}/ReconfigureComputeResource_Task"
)
_OP_CREATE_FOLDER = "POST:/Folder/{moId}/CreateFolder"

# vim data-object type discriminators (``_typeName``) for the DRS-rule
# reconfigure body. ``ReconfigureComputeResourceRequestType.spec`` is
# declared as the base ``ComputeResourceConfigSpec``, so a cluster
# reconfigure must tag the spec ``ClusterConfigSpecEx`` for the vim25-JSON
# binding to deserialise the subtype; likewise ``ClusterRuleSpec.info`` is
# declared as the base ``ClusterRuleInfo``, so the affinity / anti-affinity
# subtype is tagged. Since #3103 the ``ClusterRuleSpec`` array items and
# the ``VirtualMachine`` MoRefs are tagged too — the live 8.0.3
# differential disproved the earlier "declared type == runtime type needs
# no ``_typeName``" premise (spec-verified against the pinned
# ``vi-json.yaml``, whose ``Any.required`` names ``_typeName``).
_CLUSTER_CONFIG_SPEC_EX_TYPE = "ClusterConfigSpecEx"
_CLUSTER_RULE_SPEC_TYPE = "ClusterRuleSpec"
_CLUSTER_AFFINITY_RULE_TYPE = "ClusterAffinityRuleSpec"
_CLUSTER_ANTI_AFFINITY_RULE_TYPE = "ClusterAntiAffinityRuleSpec"
_CLUSTER_COMPUTE_RESOURCE_MO_TYPE = "ClusterComputeResource"
# Property path the collision / idempotence read queries on the cluster:
# ``ClusterComputeResource.configurationEx`` is a ``ClusterConfigInfoEx``
# whose ``rule`` array holds the existing ``ClusterRuleInfo`` rules
# (spec-verified). Read un-gated via the shared ``RetrievePropertiesEx``.
_PROP_CONFIGURATION_EX_RULE = "configurationEx.rule"

# operator-facing ``rule_type`` -> vim rule-info ``_typeName``.
_DRS_RULE_TYPE_TYPE_NAMES: dict[str, str] = {
    "affinity": _CLUSTER_AFFINITY_RULE_TYPE,
    "anti_affinity": _CLUSTER_ANTI_AFFINITY_RULE_TYPE,
}

# A DRS affinity / anti-affinity rule constrains the relative placement of
# >=2 VMs; a single-VM (or empty) rule is meaningless, so the handler
# refuses one (``status="insufficient_vms"``) before any write.
_DRS_RULE_MIN_VMS = 2

# Default wall-clock bound for the ReconfigureComputeResource_Task poll —
# mirrors the 600s disk-grow / vm.clone convention.
_DRS_RULE_TASK_TIMEOUT_SECONDS = 600.0

#: vi-json sub-op manifests for the #2895 writes (parallel to
#: ``_VIM_SUB_OPS_VM_DISK_GROW``; named out of the ``_SUB_OPS_*`` namespace
#: so the vcenter.yaml ingest-reconcile sweep skips them). The pinned
#: ``vi-json.yaml`` reconcile lane introspects these to assert every
#: declared vim path exists in the spec. drs_rule reads existing rules via
#: ``RetrievePropertiesEx`` then writes ``ReconfigureComputeResource_Task``;
#: folder.create is a single synchronous ``CreateFolder`` (no poll → no
#: ``RetrievePropertiesEx``).
_VIM_SUB_OPS_CLUSTER_DRS_RULE_CREATE: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
)
_VIM_SUB_OPS_FOLDER_CREATE: tuple[str, ...] = (_OP_CREATE_FOLDER,)

# vim (VI-JSON) op_ids for the #2970 real-spec repoints. The pinned
# ``vcenter.yaml`` serves NO REST path for VM snapshots, host maintenance
# mode, DRS migration recommendations, or DVS host membership -- those
# surfaces are vim-only in the pinned 9.0 spec (spec-verified), so the
# affected composite steps ride the same governed vmomi seam the
# disk-grow / clone / drs_rule writes established (#2893-#2895).
#
# * ``RevertToSnapshot_Task`` -- the only revert route; the snapshot
#   *listing* is a ``RetrievePropertiesEx`` read of the VM's ``snapshot``
#   property (``VirtualMachineSnapshotInfo.rootSnapshotList``).
# * ``EnterMaintenanceMode_Task`` / ``ExitMaintenanceMode_Task`` -- the
#   only maintenance routes. Both request types carry a required int
#   ``timeout`` (``<= 0`` means no vim-side timeout).
# * ``ReconfigureDvs_Task`` -- host removal from a DVS is a
#   ``DVSConfigSpec.host`` member spec with ``operation="remove"``; the
#   spec's required ``configVersion`` is read off ``config.configVersion``
#   first (``RetrievePropertiesEx``).
# * DRS recommendations are the ``ClusterComputeResource.drsRecommendation``
#   property (``ClusterDrsRecommendation[]`` with the per-VM
#   ``migrationList``), read via ``RetrievePropertiesEx``. Deprecated
#   since VI API 2.5 in favour of the generic ``recommendation`` action
#   list, but still served by the pinned spec and the only shape that
#   directly carries the vm -> destination-host migration pairs.
_OP_REVERT_TO_SNAPSHOT_TASK = "POST:/VirtualMachineSnapshot/{moId}/RevertToSnapshot_Task"
# The pre-9.0 vim destroy arm (#3198). ``VirtualMachine.Destroy_Task`` is the
# core vim delete method (task-polled); the 9.0+ arm uses the synchronous REST
# ``DELETE:/vcenter/vm/{vm}`` (:data:`_OP_DELETE_VM`) instead.
_OP_DESTROY_VM_TASK = "POST:/VirtualMachine/{moId}/Destroy_Task"
_OP_ENTER_MAINTENANCE_TASK = "POST:/HostSystem/{moId}/EnterMaintenanceMode_Task"
_OP_EXIT_MAINTENANCE_TASK = "POST:/HostSystem/{moId}/ExitMaintenanceMode_Task"
_OP_RECONFIGURE_DVS_TASK = "POST:/DistributedVirtualSwitch/{moId}/ReconfigureDvs_Task"

# vim MO/property names for the #2970 reads (``_HOST_SYSTEM_MO_TYPE`` and
# ``_CLUSTER_COMPUTE_RESOURCE_MO_TYPE`` above are reused for the MoRefs).
_DVS_MO_TYPE = "DistributedVirtualSwitch"
_VM_SNAPSHOT_MO_TYPE = "VirtualMachineSnapshot"
_PROP_SNAPSHOT = "snapshot"
_PROP_DRS_RECOMMENDATION = "drsRecommendation"
_PROP_DVS_CONFIG_VERSION = "config.configVersion"

# Required int ``timeout`` on the maintenance request types; ``0`` defers
# entirely to the poll's wall-clock bound below.
_MAINTENANCE_VIM_TIMEOUT: Final[int] = 0

# Wall-clock bounds for the #2970 vim task polls + the cluster.patch cis
# task poll -- the 600s ``vm.disk.grow`` / ``vm.clone_from_template``
# convention.
_SNAPSHOT_REVERT_TASK_TIMEOUT_SECONDS = 600.0
_VM_DESTROY_TASK_TIMEOUT_SECONDS = 600.0
_MAINTENANCE_TASK_TIMEOUT_SECONDS = 600.0
_DVS_RECONFIGURE_TASK_TIMEOUT_SECONDS = 600.0
_HOST_APPLY_TASK_TIMEOUT_SECONDS = 600.0

#: vi-json sub-op manifests for the #2970 repoints (parallel to
#: ``_VIM_SUB_OPS_VM_DISK_GROW``; named out of the ``_SUB_OPS_*`` namespace
#: so the vcenter.yaml ingest-reconcile sweep skips them). The pinned
#: ``vi-json.yaml`` reconcile lane introspects these to assert every
#: declared vim path exists in the spec.
_VIM_SUB_OPS_VM_SNAPSHOT_REVERT: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_REVERT_TO_SNAPSHOT_TASK,
)
_VIM_SUB_OPS_VM_MIGRATE: tuple[str, ...] = (_OP_RETRIEVE_PROPERTIES,)
#: vm.destroy's pre-9.0 vim arm (#3198): the ``Destroy_Task`` delete plus the
#: best-effort snapshot ``RetrievePropertiesEx`` the blast-radius preview reads.
_VIM_SUB_OPS_VM_DESTROY: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_DESTROY_VM_TASK,
)
_VIM_SUB_OPS_HOST_EVACUATE: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_ENTER_MAINTENANCE_TASK,
)
_VIM_SUB_OPS_CLUSTER_PATCH: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_ENTER_MAINTENANCE_TASK,
    _OP_EXIT_MAINTENANCE_TASK,
)
_VIM_SUB_OPS_HOST_DETACH_FROM_VDS: tuple[str, ...] = (
    _OP_RETRIEVE_PROPERTIES,
    _OP_RECONFIGURE_DVS_TASK,
)

# Hardware write ops (#2891). Post-clone reconfigure of a VM's virtual
# hardware, straight vSphere Automation REST. CPU/memory update and the
# ethernet/cdrom device sub-resources send the update spec's fields at the
# **top level** of the request body -- the modern ``/api`` surface takes the
# ``*Spec`` directly (``Cpu.UpdateSpec`` / ``Ethernet.UpdateSpec`` / ...),
# not the legacy ``/rest`` ``{"spec": {...}}`` envelope (#2973); the CD-ROM
# ``disconnect`` rides an ``?action=`` suffix like the other action
# endpoints (power / relocate / maintenance).
_OP_UPDATE_VM_CPU = "PATCH:/vcenter/vm/{vm}/hardware/cpu"
_OP_UPDATE_VM_MEMORY = "PATCH:/vcenter/vm/{vm}/hardware/memory"
_OP_GET_VM_NIC = "GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}"
_OP_UPDATE_VM_NIC = "PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}"
_OP_GET_VM_CDROM = "GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}"
_OP_UPDATE_VM_CDROM = "PATCH:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}"
_OP_DELETE_VM_CDROM = "DELETE:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}"
_OP_DISCONNECT_VM_CDROM = "POST:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}?action=disconnect"

# Guest customization (GOSC) sub-ops (#2892). Create a reusable named
# customization spec, then apply a saved one to a VM. Both are plain
# vCenter REST -- no vim fallback (the issue's grounded gap table).
_OP_CREATE_CUSTOMIZATION_SPEC = "POST:/vcenter/guest/customization-specs"
_OP_SET_VM_CUSTOMIZATION = "PUT:/vcenter/vm/{vm}/guest/customization"

#: Default Microsoft time-zone index for the Windows ``GuiUnattended.time_zone``
#: when the operator does not pin one. ``GuiUnattended.time_zone`` is a REQUIRED
#: *integer* index in the pinned schema (``Vcenter.Guest.GuiUnattended``,
#: vcenter.yaml:126181) -- distinct from the Linux tz-name string -- so a value
#: is always emitted. ``85`` is GMT; operators override via ``windows_time_zone``
#: (indices: https://support.microsoft.com/help/973627).
_DEFAULT_WINDOWS_TIME_ZONE: Final[int] = 85


def _power_vm_op_id(action: str) -> str:
    """Build the per-action canonical op_id for ``POST:/vcenter/vm/{vm}/power``.

    vCenter exposes ``start`` / ``stop`` / ``suspend`` / ``reset`` as four
    distinct ``?action=<verb>`` keys. The composite picks the key at call time
    so each action lands on its own governance evaluation, not as a free-form
    ``action`` parameter on a single shared op_id.
    """
    return f"POST:/vcenter/vm/{{vm}}/power?action={action}"


def _guest_power_vm_op_id(action: str) -> str:
    """Build the per-action canonical op_id for ``POST:/vcenter/vm/{vm}/guest/power``.

    The vAPI guest-power endpoint mediates a *soft* power transition through
    VMware Tools: ``?action=shutdown`` asks the guest OS for a clean
    shutdown, ``?action=reboot`` for a clean restart. Same action-in-the-path
    keying vCenter uses for hard power (:func:`_power_vm_op_id`), so the op_id
    round-trips through the ingest parser identically. Distinct from the hard
    ``/power`` endpoint: guest verbs require running Tools and return
    ``ServiceUnavailable`` (HTTP 503) when Tools is absent -- surfaced by the
    single-VM handler as a typed ``tools_unavailable`` status rather than a
    hang.
    """
    return f"POST:/vcenter/vm/{{vm}}/guest/power?action={action}"


# Recursive composite sub-op_id (host.evacuate -> vm.migrate). Routed
# through ``dispatch_child`` (a registrar-guaranteed ``source_kind="composite"``
# row), not the direct session -- per #2248 the composite->composite recursion
# is out of scope for the ingested-dispatch migration.
_OP_COMPOSITE_VM_MIGRATE = "vmware.composite.vm.migrate"

# Composite op_ids -- retained as the canonical documentation anchor.
_COMPOSITE_OP_ID_VM_CREATE = "vmware.composite.vm.create"
_COMPOSITE_OP_ID_VM_CLONE = "vmware.composite.vm.clone"
_COMPOSITE_OP_ID_VM_SNAPSHOT_REVERT = "vmware.composite.vm.snapshot.revert"
_COMPOSITE_OP_ID_VM_MIGRATE = "vmware.composite.vm.migrate"
_COMPOSITE_OP_ID_VM_POWER_BULK = "vmware.composite.vm.power.bulk"
_COMPOSITE_OP_ID_VM_POWER = "vmware.composite.vm.power"
_COMPOSITE_OP_ID_HOST_EVACUATE = "vmware.composite.host.evacuate"
_COMPOSITE_OP_ID_HOST_DETACH_FROM_VDS = "vmware.composite.host.detach_from_vds"
_COMPOSITE_OP_ID_CLUSTER_PATCH = "vmware.composite.cluster.patch"
_COMPOSITE_OP_ID_VM_RESIZE = "vmware.composite.vm.resize"
_COMPOSITE_OP_ID_VM_NIC_REPOINT = "vmware.composite.vm.nic.repoint"
_COMPOSITE_OP_ID_VM_DEVICE_CDROM = "vmware.composite.vm.device.cdrom"
_COMPOSITE_OP_ID_GUEST_CUSTOMIZATION_SPEC_CREATE = (
    "vmware.composite.guest.customization_spec.create"
)
_COMPOSITE_OP_ID_VM_CUSTOMIZE = "vmware.composite.vm.customize"

# Per-composite sub-op-id tuples. Pre-#2256 these fed the L2 pre-flight
# check that guarded a missing catalog ingest; the direct-session migration
# removed that coupling, so the tuples now serve as the canonical sub-op-path
# manifest the ingest-reconcile acceptance guard
# (``tests/test_connectors_vmware_rest_composites_l2_ingest_reconcile.py``)
# checks against the vCenter spec. Composite-to-composite sub-ops
# (``vmware.composite.*``) are listed for host.evacuate but are routed through
# ``dispatch_child``, not the direct session.
_POWER_ACTIONS: tuple[str, ...] = ("start", "stop", "suspend", "reset")

# Single-VM ``vm.power`` operator-facing verb -> raw sub-op_id. Hard verbs
# hit the ``/power`` endpoint (immediate, may lose in-guest state); the two
# ``guest_*`` verbs hit ``/guest/power`` (Tools-mediated soft transition).
# ``reset`` is the hard cycle; ``guest_reboot`` the clean one -- both are
# offered so the approver picks the blast radius they intend.
_SINGLE_POWER_VERB_OP_IDS: dict[str, str] = {
    "on": _power_vm_op_id("start"),
    "off": _power_vm_op_id("stop"),
    "reset": _power_vm_op_id("reset"),
    "guest_shutdown": _guest_power_vm_op_id("shutdown"),
    "guest_reboot": _guest_power_vm_op_id("reboot"),
}

#: The ``vm.power`` verbs routed through VMware Tools (``/guest/power``). A
#: failure on one of these is classified against the Tools state; a hard-power
#: failure never is (a 503 there is not a Tools signal).
_GUEST_POWER_VERBS: frozenset[str] = frozenset({"guest_shutdown", "guest_reboot"})
_SUB_OPS_VM_CREATE: tuple[str, ...] = (
    _OP_LIST_FOLDERS,
    _OP_CREATE_VM,
    _OP_DELETE_VM,
    _OP_CREATE_VM_NIC,
    _power_vm_op_id("start"),
    # #3099 pre-9.0 vim-arm resolution reads (both REST, vcenter.yaml-served):
    # the datastore display name for ``files.vmPathName`` and the network
    # display name for a standard-portgroup NIC backing.
    _OP_GET_DATASTORE,
    _OP_LIST_NETWORK,
    # #3115 folder-lookup datacenter-scoping reads (all REST,
    # vcenter.yaml-served): the datacenter listing plus the three
    # pin-to-datacenter intersection probes.
    _OP_LIST_DATACENTERS,
    _OP_LIST_HOSTS,
    _OP_LIST_RESOURCE_POOLS,
    _OP_LIST_DATASTORES,
)
_SUB_OPS_VM_CLONE: tuple[str, ...] = (
    _OP_GET_VM,
    _OP_DEPLOY_LIBRARY_VM,
)
_SUB_OPS_VM_DEPLOY_FROM_LIBRARY: tuple[str, ...] = (
    _OP_FIND_LIBRARY,
    _OP_FIND_LIBRARY_ITEM,
    _OP_DEPLOY_OVF_LIBRARY_ITEM,
    _power_vm_op_id("start"),
)
_SUB_OPS_VM_MIGRATE: tuple[str, ...] = (_OP_RELOCATE_VM,)
_SUB_OPS_VM_POWER_BULK: tuple[str, ...] = (
    _OP_LIST_VMS,
    *(_power_vm_op_id(action) for action in _POWER_ACTIONS),
)
_SUB_OPS_VM_POWER: tuple[str, ...] = tuple(dict.fromkeys(_SINGLE_POWER_VERB_OP_IDS.values()))
_SUB_OPS_HOST_EVACUATE: tuple[str, ...] = (
    _OP_LIST_VMS,
    _OP_COMPOSITE_VM_MIGRATE,
)
_SUB_OPS_HOST_DETACH_FROM_VDS: tuple[str, ...] = (
    _OP_LIST_NETWORK,
    _OP_LIST_VMS,
    _OP_LIST_VM_NICS,
    _OP_UPDATE_VM_NIC,
)
_SUB_OPS_CLUSTER_PATCH: tuple[str, ...] = (
    _OP_LIST_HOSTS,
    _OP_HOST_SOFTWARE_APPLY,
    _OP_GET_TASK,
)
_SUB_OPS_VM_RESIZE: tuple[str, ...] = (
    _OP_GET_VM,
    _OP_UPDATE_VM_CPU,
    _OP_UPDATE_VM_MEMORY,
)
_SUB_OPS_VM_NIC_REPOINT: tuple[str, ...] = (
    _OP_GET_VM_NIC,
    _OP_LIST_NETWORK,
    _OP_UPDATE_VM_NIC,
)
_SUB_OPS_VM_DEVICE_CDROM: tuple[str, ...] = (
    _OP_GET_VM_CDROM,
    _OP_UPDATE_VM_CDROM,
    _OP_DELETE_VM_CDROM,
    _OP_DISCONNECT_VM_CDROM,
)
_SUB_OPS_GUEST_CUSTOMIZATION_SPEC_CREATE: tuple[str, ...] = (_OP_CREATE_CUSTOMIZATION_SPEC,)
_SUB_OPS_VM_CUSTOMIZE: tuple[str, ...] = (
    _OP_LIST_VMS,
    _OP_SET_VM_CUSTOMIZATION,
    _power_vm_op_id("start"),
)


def _unwrap_value(payload: Any) -> Any:
    """Return the inner ``value`` field on a pre-7 envelope, else *payload*."""
    if isinstance(payload, dict) and set(payload.keys()) == {"value"}:
        return payload["value"]
    return payload


def _split_sub_op(op_id: str, params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """Split *op_id* + *params* into ``(method, spec-relative path, remainder)``.

    Extracts the ``{var}`` names from the ``METHOD:/path`` template,
    substitutes the matching *params* entries into the path (any
    ``?action=<verb>`` query segment rides along verbatim), and returns the
    remaining params -- the query bucket for a ``GET`` or the JSON body for a
    write. Mirrors the ``x-meho-param-loc`` path/query/body split the ingested
    dispatch performed, without any descriptor lookup.
    """
    method, _, path_template = op_id.partition(":")
    var_names = _PATH_VAR_RE.findall(path_template)
    path_params = {name: params[name] for name in var_names}
    path = path_template.format(**path_params) if path_params else path_template
    remainder = {k: v for k, v in params.items() if k not in path_params}
    return method, path, remainder


async def _read_sub_op(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    op_id: str,
    params: dict[str, Any] | None = None,
) -> Any:
    """Issue one read sub-call (``GET``) directly on the connector session.

    Splits the canonical ``METHOD:/path`` *op_id*, mounts the substituted
    path onto the target's live ``/api`` / ``/rest`` prefix, and dispatches
    through :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._get_json`
    (tenacity-retried, idempotent) with the remainder params as the query
    bucket. The query bucket is authored in the legacy ``filter.*`` style
    and keyed off the mount flavor by
    :meth:`VmwareRestConnector.adapt_op_query` (#2298) — bare param names
    on modern ``/api`` (which 400s the prefixed form), ``filter.*`` on
    legacy ``/rest`` — so the write composites' resolution listings behave
    the same as the read composites'. Read sub-ops carry no governance
    gate -- they are the safe resolution reads the write composites build
    their request bodies from. Transport / status failures raise
    :exc:`httpx.HTTPError`; load-bearing callers let it propagate (the
    dispatcher wraps it as ``connector_error`` for the composite parent).
    """
    method, path, query = _split_sub_op(op_id, params or {})
    if method != "GET":
        raise RuntimeError(f"_read_sub_op called with non-GET op_id {op_id!r}")
    mounted = await connector.mount_op_path(target, path, operator)
    adapted = await connector.adapt_op_query(target, query, operator)
    return await connector._get_json(target, mounted, operator=operator, params=adapted)


async def _write_sub_op(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    op_id: str,
    params: dict[str, Any],
    *,
    timeout: Any = httpx.USE_CLIENT_DEFAULT,
) -> tuple[OperationResult | None, Any]:
    """Gate then issue one *write* sub-call directly on the connector session.

    Runs :func:`~meho_backplane.operations.composite.enforce_subop_policy`
    with the sub-op's full logical *params* (so the durable
    :class:`~meho_backplane.db.models.ApprovalRequest` names the entity the
    write would touch) and the declared ``dangerous`` /
    ``requires_approval=False`` governance. When the seam returns a result
    (``awaiting_approval`` / ``denied``) the write is **not** issued -- the
    ``(gate, None)`` tuple signals the caller to return that
    :class:`OperationResult` verbatim. When the seam clears the gate
    (``None``), the sub-op is split into ``(verb, path, body)`` and dispatched
    through
    :meth:`~meho_backplane.connectors.adapters.http.HttpConnector._post_json`
    (honouring the actual ``POST`` / ``PATCH`` / ``DELETE`` verb); the parsed
    JSON payload rides back as ``(None, payload)``. Transport failures raise
    :exc:`httpx.HTTPError` for the caller to catch (partial-failure legs) or
    let propagate (load-bearing legs).

    ``timeout`` overrides the connector's default per-request timeout for
    this one sub-op. It defaults to :data:`httpx.USE_CLIENT_DEFAULT`, so
    every write sub-op keeps the connector's 30s client default unchanged;
    only the two synchronous library-item deploys pass an explicit long
    ceiling (:data:`_LIBRARY_ITEM_DEPLOY_TIMEOUT`, #3076).
    """
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        safety_level=_WRITE_SAFETY_LEVEL,
        requires_approval=_WRITE_REQUIRES_APPROVAL,
        target=target,
        params=params,
    )
    if gate is not None:
        return gate, None
    method, path, body = _split_sub_op(op_id, params)
    mounted = await connector.mount_op_path(target, path, operator)
    payload = await connector._post_json(
        target, mounted, operator=operator, verb=method, json=body or None, timeout=timeout
    )
    return None, payload


async def _write_vmomi_sub_op(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    vmomi_path: str,
    body: dict[str, Any],
    params: dict[str, Any],
) -> tuple[OperationResult | None, Any]:
    """Gate then issue one *mutating* vim (VI-JSON) sub-call on the connector session.

    The VI-JSON counterpart of :func:`_write_sub_op`: a vmomi POST that
    *mutates* (``ReconfigVM_Task``, ``CloneVM_Task``,
    ``ReconfigureComputeResource_Task`` …) is a write sub-op, not a
    transport detail, so it flows through the **same** #2254 governance
    seam the REST write sub-ops do. Runs
    :func:`~meho_backplane.operations.composite.enforce_subop_policy` with
    the vim method's canonical governance *op_id* (the ``METHOD:/path`` key
    the ingest parser emits from ``vi-json.yaml``, e.g.
    ``POST:/VirtualMachine/{moId}/ReconfigVM_Task``) and the sub-op's full
    logical *params* (so the durable
    :class:`~meho_backplane.db.models.ApprovalRequest` names the entity the
    write touches) under the shared ``dangerous`` /
    ``requires_approval=False`` posture.

    When the seam returns a result (``awaiting_approval`` / ``denied``) the
    vmomi write is **not** issued -- the ``(gate, None)`` tuple signals the
    caller to return that :class:`OperationResult` verbatim, so a
    policy-denied vmomi write never reaches the wire. When the seam clears
    the gate (``None``), the method is POSTed through
    :meth:`~meho_backplane.connectors.vmware_rest.connector.VmwareRestConnector._post_vmomi_json`
    (which mounts it on the documented VI-JSON base ``/sdk/vim25/{release}``,
    single ``/api`` fallback) and the parsed payload -- for a ``*_Task``
    method, the returned Task :class:`ManagedObjectReference` -- rides back
    as ``(None, payload)``. Transport failures raise :exc:`httpx.HTTPError`
    for the caller.

    ``vmomi_path`` is the concrete spec-relative method path (moId
    substituted, e.g. ``/VirtualMachine/vm-1/ReconfigVM_Task``); ``op_id``
    is the placeholder governance key so a per-``(principal, op, target)``
    grant matches regardless of which VM the write targets.
    """
    gate = await enforce_subop_policy(
        operator=operator,
        connector_id=_CONNECTOR_ID,
        op_id=op_id,
        safety_level=_WRITE_SAFETY_LEVEL,
        requires_approval=_WRITE_REQUIRES_APPROVAL,
        target=target,
        params=params,
    )
    if gate is not None:
        return gate, None
    payload = await connector._post_vmomi_json(target, vmomi_path, operator=operator, json=body)
    return None, payload


def _rolled_back(
    *,
    steps: list[str],
    failed_step: str,
    reason: str,
) -> dict[str, Any]:
    """Build the canonical rolled_back response envelope for :func:`vm_create_composite`."""
    return {
        "status": "rolled_back",
        "vm_id": None,
        "steps_succeeded": steps,
        "failed_step": failed_step,
        "rollback_reason": reason,
    }


async def _resolve_vm_list(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    filter_dict: dict[str, Any],
) -> list[dict[str, Any]]:
    """Resolve a VM filter to its listing rows via ``GET:/vcenter/vm``.

    Read-only: issues exactly one listing GET directly on the connector
    session, never a mutation. Filter keys are forwarded as ``filter.<key>``
    query params per the vCenter listing contract.

    Shared seam (#1608): the write handlers that fan out over a filtered
    VM set (:func:`vm_power_bulk_composite`, :func:`host_evacuate_composite`,
    :func:`host_detach_from_vds_composite`) call this at dispatch time, and
    their park-time preview builders (:mod:`._write_preview`) call the same
    function against the resolved connector at approval-park time — so the
    entity set the reviewer sees in ``proposed_effect`` is resolved by the
    same code path the approved dispatch will use.

    Raises :class:`RuntimeError` when the listing returns a non-list
    payload and :exc:`httpx.HTTPError` when the sub-op transport fails.
    Non-dict rows are dropped (the listing contract yields summary objects);
    per-row key validation stays with callers.
    """
    listing = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_LIST_VMS,
        {f"filter.{k}": v for k, v in filter_dict.items()},
    )
    vms = _unwrap_value(listing)
    if not isinstance(vms, list):
        raise RuntimeError(f"expected list from {_OP_LIST_VMS!r}, got {type(vms).__name__}")
    return [entry for entry in vms if isinstance(entry, dict)]


async def _resolve_cluster_hosts(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    cluster_moid: str,
) -> list[dict[str, Any]]:
    """Resolve a cluster's host listing rows via ``GET:/vcenter/host``.

    Read-only single GET directly on the connector session, scoped to the
    cluster via the ``Host.FilterSpec.clusters`` query filter -- the pinned
    spec serves no per-cluster ``GET:/vcenter/cluster/{cluster}/host``
    resource (#2970). Each ``Vcenter.Host.Summary`` row keeps the ``host``
    moid key the callers extract. Shared between
    :func:`cluster_patch_composite` (dispatch time) and its park-time preview
    builder in :mod:`._write_preview` (#1608) — same rationale as
    :func:`_resolve_vm_list`.

    Raises :class:`RuntimeError` on a non-list payload and
    :exc:`httpx.HTTPError` on a transport fault. Non-dict rows are dropped.
    """
    listing = await _read_sub_op(
        connector, target, operator, _OP_LIST_HOSTS, {"filter.clusters": [cluster_moid]}
    )
    entries = _unwrap_value(listing)
    if not isinstance(entries, list):
        raise RuntimeError(f"expected list from {_OP_LIST_HOSTS!r}, got {type(entries).__name__}")
    return [entry for entry in entries if isinstance(entry, dict)]


# ===========================================================================
# vm.create
# ===========================================================================


def _folder_lookup_rolled_back(
    steps: list[str], reason: str | None, candidates: list[str]
) -> dict[str, Any]:
    """``rolled_back`` envelope for a failed folder resolution (#3115).

    Non-empty *candidates* (the moids an ambiguous ``folder_name``
    matched) surface as ``candidate_folders`` so the operator can
    re-issue with the intended moid as the ``folder`` param.
    """
    envelope = _rolled_back(steps=steps, failed_step="folder_lookup", reason=reason or "")
    if candidates:
        envelope["candidate_folders"] = candidates
    return envelope


async def _resolve_pin_datacenter(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    pins: dict[str, Any],
) -> str | None:
    """Best-effort: the datacenter moid the placement pins live in (#3115).

    No REST listing row carries a parent-datacenter key, so the reverse
    map rides the listing FilterSpecs instead: every
    ``GET:/vcenter/<kind>`` intersects an identity filter (``hosts`` /
    ``resource_pools`` / ``datastores``) with ``datacenters``, so probing
    each datacenter with a pin returns a row only in the datacenter that
    owns the pin. Pins are tried most-specific-first (host, resource
    pool, datastore); a probe kind whose listing faults falls through to
    the next kind.

    Best-effort by design: returns ``None`` (unknown) when the target
    has fewer than two datacenters (scoping cannot change the outcome),
    when the datacenter listing faults, or when no datacenter claims any
    pin (stale moids). The caller then keeps the unscoped lookup — the
    ambiguity refusal in :func:`_resolve_folder_moid` stays the hard
    correctness net either way.
    """
    try:
        listing = await _read_sub_op(connector, target, operator, _OP_LIST_DATACENTERS, {})
    except httpx.HTTPError:
        return None
    rows = _unwrap_value(listing)
    if not isinstance(rows, list):
        return None
    datacenter_moids: list[str] = [
        row["datacenter"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("datacenter"), str)
    ]
    if len(datacenter_moids) < 2:
        return None
    probes = [
        (op_id, filter_key, pin)
        for pin_key, op_id, filter_key in (
            ("host", _OP_LIST_HOSTS, "filter.hosts"),
            ("resource_pool", _OP_LIST_RESOURCE_POOLS, "filter.resource_pools"),
            ("datastore", _OP_LIST_DATASTORES, "filter.datastores"),
        )
        if isinstance(pin := pins.get(pin_key), str)
    ]
    for op_id, filter_key, pin in probes:
        for datacenter_moid in datacenter_moids:
            try:
                found = await _read_sub_op(
                    connector,
                    target,
                    operator,
                    op_id,
                    {filter_key: [pin], "filter.datacenters": [datacenter_moid]},
                )
            except httpx.HTTPError:
                break  # this listing is unusable on the live target; next pin kind
            found_rows = _unwrap_value(found)
            if isinstance(found_rows, list) and any(isinstance(row, dict) for row in found_rows):
                return datacenter_moid
    return None


async def _list_folder_moids(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    folder_name: str,
    datacenter_moid: str | None = None,
) -> tuple[list[str], str | None]:
    """``(moids, failure_reason)`` for one ``GET:/vcenter/folder`` name lookup.

    ``datacenter_moid`` scopes the listing via the ``datacenters``
    filter. Failure modes: empty match list, no listing row carrying a
    string ``folder`` key.
    """
    query: dict[str, Any] = {"filter.names": [folder_name]}
    if datacenter_moid is not None:
        query["filter.datacenters"] = [datacenter_moid]
    folder_listing = await _read_sub_op(connector, target, operator, _OP_LIST_FOLDERS, query)
    folder_entries = _unwrap_value(folder_listing)
    if not isinstance(folder_entries, list) or not folder_entries:
        return [], f"folder name {folder_name!r} did not resolve to any moid"
    moids: list[str] = [
        entry["folder"]
        for entry in folder_entries
        if isinstance(entry, dict) and isinstance(entry.get("folder"), str)
    ]
    if not moids:
        return [], "folder listing row missing ``folder`` key"
    return moids, None


async def _resolve_folder_moid(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    folder_name: str | None,
    placement_pins: dict[str, Any] | None = None,
) -> tuple[str | None, str | None, list[str]]:
    """Resolve the create-target VM folder moid from its display name.

    Returns ``(moid, None, [])`` on success or ``(None, reason,
    candidate_moids)`` on failure -- the caller folds the reason (and,
    when non-empty, the candidates) into a ``rolled_back`` envelope via
    :func:`_folder_lookup_rolled_back`.

    Folder display names are unique only per parent folder, and every
    datacenter ships a default VM folder named ``vm`` — so on a
    multi-datacenter vCenter an unscoped name lookup can match several
    folders, and silently taking the first row created VMs in the wrong
    datacenter (#3115, proven live: datacenter-A placement pins mixed
    with a datacenter-B folder fault ``CreateVM_Task`` only at create
    time). Resolution ladder:

    1. unscoped ``filter.names`` lookup — a unique match wins (the
       pre-#3115 read pattern, byte-identical: no extra reads);
    2. multi-match with *placement_pins*: reverse-map one pin to its
       datacenter (:func:`_resolve_pin_datacenter`) and re-issue the
       lookup scoped via ``filter.datacenters``;
    3. still ambiguous — or ambiguous with no resolvable datacenter —
       → refuse with the candidate moids, never the first row.
    """
    if not isinstance(folder_name, str) or not folder_name:
        return None, "neither a `folder` moid nor a `folder_name` was supplied", []
    moids, failure = await _list_folder_moids(
        connector=connector, target=target, operator=operator, folder_name=folder_name
    )
    if failure is not None:
        return None, failure, []
    if len(moids) == 1:
        return moids[0], None, []
    datacenter_moid = None
    if placement_pins:
        datacenter_moid = await _resolve_pin_datacenter(
            connector=connector, target=target, operator=operator, pins=placement_pins
        )
    if datacenter_moid is not None:
        scoped_moids, scoped_failure = await _list_folder_moids(
            connector=connector,
            target=target,
            operator=operator,
            folder_name=folder_name,
            datacenter_moid=datacenter_moid,
        )
        if scoped_failure is not None:
            return (
                None,
                (
                    f"folder name {folder_name!r} has no usable match in the placement "
                    f"pins' datacenter {datacenter_moid}; the unscoped matches "
                    f"({', '.join(moids)}) belong to other datacenters"
                ),
                moids,
            )
        if len(scoped_moids) == 1:
            return scoped_moids[0], None, []
        moids = scoped_moids
    return (
        None,
        (
            f"folder name {folder_name!r} matched {len(moids)} folders "
            f"({', '.join(moids)}); pass the intended moid as the `folder` param "
            "to disambiguate"
        ),
        moids,
    )


async def _rollback_created_vm(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_id: str,
) -> None:
    """Issue ``DELETE:/vcenter/vm/{vm}`` to remove a half-created VM.

    Best-effort: rollback faults are swallowed -- the operator already knows
    the create flow failed, and a denied/queued rollback sub-op or a
    transport error must not mask the original failure. A gate result from
    the seam is ignored here for the same reason (the rollback DELETE is a
    cleanup, not an operator-requested action).
    """
    try:
        await _write_sub_op(connector, target, operator, _OP_DELETE_VM, {"vm": vm_id})
    except httpx.HTTPError:
        return


async def _enable_nested_hv(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_id: str,
) -> tuple[OperationResult | None, str | None]:
    """Enable VHV on a freshly-created (powered-off) VM via ``ReconfigVM_Task``.

    The #3093 leg of :func:`vm_create_composite`: issues the one-field
    ``nestedHVEnabled`` reconfigure through the governed vmomi write seam
    (:func:`_write_vmomi_sub_op` — version-correct on vCenter 8.0.x *and*
    9.x, unlike raw VI-JSON dispatch) and drives the returned ``*_Task``
    MoRef to a terminal state via :func:`poll_vim_task`.

    Returns ``(gate, failure_reason)``:

    * ``(gate, None)`` — the #2254 seam parked/denied the write; the caller
      returns the :class:`OperationResult` verbatim (no reconfigure fired).
    * ``(None, None)`` — VHV applied (task reached ``success``).
    * ``(None, reason)`` — the leg failed (transport fault, task fault, or
      poll timeout); the caller folds *reason* into vm.create's existing
      rollback contract. A timeout counts as failure: the composite can
      only report ``created`` when every requested step verifiably landed.
    """
    try:
        gate, task_payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=_OP_RECONFIG_VM_TASK,
            vmomi_path=f"/VirtualMachine/{vm_id}/ReconfigVM_Task",
            body=_NESTED_HV_RECONFIG_BODY,
            params={"vm": vm_id, "nested_hv": True},
        )
        if gate is not None:
            return gate, None
        outcome = await poll_vim_task(
            connector,
            target,
            operator,
            task=_unwrap_value(task_payload),
            timeout_seconds=_VM_CREATE_VHV_TASK_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return None, f"nested_hv reconfigure failed: {exc}"
    if outcome.state == TASK_STATE_ERROR:
        return None, (
            f"nested_hv ReconfigVM_Task on vm {vm_id!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return None, (
            f"nested_hv ReconfigVM_Task {outcome.task} did not reach a terminal "
            f"state within {int(_VM_CREATE_VHV_TASK_TIMEOUT_SECONDS)}s"
        )
    return None, None


def _vim_create_required(about_version: str | None) -> bool:
    """``True`` when the live ``about.version`` major is a resolvable pre-9.0.

    The #3099 arm gate, mirroring :func:`_deploy_eula_field_name`'s
    version-conditional pattern (#3075): a resolvable pre-9.0 major routes
    the create through vim ``Folder.CreateVM_Task`` (bare REST
    ``POST /api/vcenter/vm`` is vendor-defective on vCenter 8.0.x — an
    opaque ``500 UNABLE_TO_ALLOCATE_RESOURCE`` for every spec shape and
    placement, proven live, while the identical vim create succeeds);
    9.0+ — and an unresolved version — keep the REST create byte-identical.
    """
    major = about_version.split(".", 1)[0].strip() if about_version else ""
    return major.isdigit() and int(major) < 9


async def _resolve_datastore_name(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    datastore_moid: str,
) -> tuple[str | None, str | None]:
    """Resolve a datastore moid to its display name via ``GET:/vcenter/datastore/{datastore}``.

    Returns ``(name, None)`` on success or ``(None, reason)`` when the
    ``Datastore.Info`` payload carries no usable name — the caller folds the
    reason into a ``rolled_back`` envelope. A transport fault propagates
    (the dispatcher wraps it as ``connector_error``), mirroring
    :func:`_resolve_folder_moid`.
    """
    info = _unwrap_value(
        await _read_sub_op(
            connector, target, operator, _OP_GET_DATASTORE, {"datastore": datastore_moid}
        )
    )
    name = info.get("name") if isinstance(info, dict) else None
    if not isinstance(name, str) or not name:
        return None, f"datastore {datastore_moid!r} info returned no display name"
    return name, None


def _build_dvpg_retrieve_params(portgroup_moid: str) -> dict[str, Any]:
    """Build the one-object DVPG property read for the vim NIC backing (#3099).

    A single ``RetrievePropertiesEx`` reading ``key`` +
    ``config.distributedVirtualSwitch`` off the ``DistributedVirtualPortgroup``
    — the two properties the ``DistributedVirtualSwitchPortConnection``
    needs (the DVS moid is then resolved to its ``uuid`` in a second read).
    ``_typeName``-annotated via the shared trio helper (#3103).
    """
    return retrieve_properties_body(
        _DVPG_MO_TYPE, [portgroup_moid], [_PROP_DVPG_KEY, _PROP_DVPG_DVS]
    )


async def _resolve_vim_nic_backing(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    nic: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Build one NIC's vim backing for the pre-9.0 create arm (#3099).

    Returns ``(backing, None)`` or ``(None, reason)``:

    * ``DISTRIBUTED_PORTGROUP`` → ``VirtualEthernetCardDistributedVirtualPortBackingInfo``
      with the port connection's ``switchUuid`` + ``portgroupKey`` resolved
      through two un-gated vmomi property reads (DVPG ``key`` +
      ``config.distributedVirtualSwitch``, then the owning switch's
      ``uuid`` — the walk govc performs for the same backing).
    * ``STANDARD_PORTGROUP`` (the schema default) →
      ``VirtualEthernetCardNetworkBackingInfo`` keyed by the network's
      display name, resolved via the ``GET:/vcenter/network`` listing.
    * anything else (``OPAQUE_NETWORK``) fails closed — it has no
      implemented vim expression on this arm.
    """
    network = nic.get("network")
    if not isinstance(network, str) or not network:
        return None, "nic entry carries no ``network`` moid"
    backing_type = nic.get("backing_type", _NIC_BACKING_STANDARD_PORTGROUP)
    if backing_type == _NIC_BACKING_DISTRIBUTED_PORTGROUP:
        portgroup_read = await connector._post_vmomi_json(
            target,
            _VMOMI_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=_build_dvpg_retrieve_params(network),
        )
        portgroup_key = _extract_single_prop(portgroup_read, _PROP_DVPG_KEY)
        switch_ref = _extract_single_prop(portgroup_read, _PROP_DVPG_DVS)
        switch_moid = switch_ref.get("value") if isinstance(switch_ref, dict) else None
        switch_type = switch_ref.get("type") if isinstance(switch_ref, dict) else None
        if (
            not isinstance(portgroup_key, str)
            or not isinstance(switch_moid, str)
            or not isinstance(switch_type, str)
        ):
            return None, (
                f"distributed portgroup {network!r} did not resolve to a "
                "portgroup key + owning switch"
            )
        uuid_read = await connector._post_vmomi_json(
            target,
            _VMOMI_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=_build_single_prop_retrieve_params(switch_type, switch_moid, _PROP_DVS_UUID),
        )
        switch_uuid = _extract_single_prop(uuid_read, _PROP_DVS_UUID)
        if not isinstance(switch_uuid, str) or not switch_uuid:
            return None, f"switch {switch_moid!r} owning portgroup {network!r} has no uuid"
        return {
            _VMOMI_TYPE_NAME_KEY: _DV_PORT_BACKING_TYPE,
            "port": {
                _VMOMI_TYPE_NAME_KEY: _DV_PORT_CONNECTION_TYPE,
                "switchUuid": switch_uuid,
                "portgroupKey": portgroup_key,
            },
        }, None
    if backing_type == _NIC_BACKING_STANDARD_PORTGROUP:
        listing = _unwrap_value(
            await _read_sub_op(
                connector, target, operator, _OP_LIST_NETWORK, {"filter.networks": [network]}
            )
        )
        first = listing[0] if isinstance(listing, list) and listing else None
        network_name = first.get("name") if isinstance(first, dict) else None
        if not isinstance(network_name, str) or not network_name:
            return None, f"network {network!r} did not resolve to a display name"
        return {
            _VMOMI_TYPE_NAME_KEY: _STANDARD_NETWORK_BACKING_TYPE,
            "deviceName": network_name,
        }, None
    return None, (
        f"nic backing_type {backing_type!r} has no vim expression on the pre-9.0 "
        "CreateVM_Task arm; use DISTRIBUTED_PORTGROUP or STANDARD_PORTGROUP"
    )


def _disk_capacities_bytes(disks: list[dict[str, Any]]) -> tuple[list[int] | None, str | None]:
    """Validate the ``disks`` param and return per-disk capacities in bytes (#3117).

    Each entry is a ``{capacity_gb: int}`` spec (the dispatch schema
    already floors ``capacity_gb`` at 1 and requires it; this is the
    fail-closed handler-side net for a hand-built call that skips
    validation). Returns ``(bytes_list, None)`` or ``(None, reason)`` —
    the caller folds the reason into a ``rolled_back`` envelope before any
    write, mirroring the ``guest_id_mapping`` / ``placement_params``
    fail-closed refusals.
    """
    capacities: list[int] = []
    for index, disk in enumerate(disks):
        capacity_gb = disk.get("capacity_gb") if isinstance(disk, dict) else None
        if isinstance(capacity_gb, bool) or not isinstance(capacity_gb, int) or capacity_gb < 1:
            return None, (
                f"disks[{index}] needs a positive integer ``capacity_gb``; got {capacity_gb!r}"
            )
        capacities.append(capacity_gb * _GIB_IN_BYTES)
    return capacities, None


def _scsi_unit_number(index: int) -> int:
    """The SCSI unit number for the *index*-th folded disk, skipping unit 7.

    SCSI reserves unit 7 for the controller, so guest disks fill 0-6 then
    resume at 8 (the govmomi convention). Keeps the folded disks on the
    single controller the arm adds without ever colliding with its unit.
    """
    return index if index < _VIM_SCSI_CONTROLLER_UNIT else index + 1


def _build_vim_disk_device_changes(capacities_bytes: list[int]) -> list[dict[str, Any]]:
    """Build the controller + per-disk ``deviceChange`` adds for the vim arm (#3117).

    Always emits one ``VirtualLsiLogicSASController`` add (so a fresh
    vim-arm VM has a controller for the governed REST disk-add even when no
    disks were requested — the issue's minimum ask), followed by one
    ``VirtualDisk`` add per requested capacity, each with ``fileOperation:
    create`` and bound to that controller. The disk backing uses an empty
    ``fileName`` (vCenter auto-generates a unique path inside the VM home),
    ``persistent`` mode and thin provisioning — the canonical govmomi
    ``CreateDisk`` shape. Every DataObject carries its ``_typeName`` (#3103).
    """
    controller: dict[str, Any] = {
        _VMOMI_TYPE_NAME_KEY: _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
        "operation": "add",
        "device": {
            _VMOMI_TYPE_NAME_KEY: _VIRTUAL_LSILOGIC_SAS_CONTROLLER_TYPE,
            "key": _VIM_SCSI_CONTROLLER_KEY,
            "busNumber": 0,
            "sharedBus": _VIM_SCSI_NO_SHARING,
        },
    }
    disk_changes = [
        {
            _VMOMI_TYPE_NAME_KEY: _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
            "operation": "add",
            "fileOperation": "create",
            "device": {
                _VMOMI_TYPE_NAME_KEY: _VIRTUAL_DISK_TYPE,
                "key": _VIM_DISK_KEY_BASE - index,
                "controllerKey": _VIM_SCSI_CONTROLLER_KEY,
                "unitNumber": _scsi_unit_number(index),
                "capacityInBytes": capacity_bytes,
                "backing": {
                    _VMOMI_TYPE_NAME_KEY: _VIRTUAL_DISK_FLAT_BACKING_TYPE,
                    "fileName": "",
                    "diskMode": _VIM_DISK_MODE_PERSISTENT,
                    "thinProvisioned": True,
                },
            },
        }
        for index, capacity_bytes in enumerate(capacities_bytes)
    ]
    return [controller, *disk_changes]


def _build_rest_disk_specs(capacities_bytes: list[int]) -> list[dict[str, Any]]:
    """Build the REST ``CreateSpec.disks`` list (#3117).

    Each entry is a flat ``Vcenter.Vm.Hardware.Disk.CreateSpec`` — a
    SCSI-attached ``new_vmdk`` sized in bytes (``int64``). vCenter
    fabricates the backing controller for these disks the same way it does
    for the guest-default boot disk, so the REST arm needs no explicit
    controller. Thin-vs-thick follows the datastore default (``new_vmdk``
    carries no provisioning knob — the recipe's documented gap).
    """
    return [
        {"type": _REST_DISK_TYPE_SCSI, "new_vmdk": {"capacity": capacity_bytes}}
        for capacity_bytes in capacities_bytes
    ]


def _build_vim_create_request(
    *,
    name: str,
    guest_id: str,
    cpu_count: int,
    memory_mib: int,
    datastore_name: str,
    nic_backings: list[dict[str, Any]],
    disk_capacities_bytes: list[int],
    nested_hv: bool,
    pool_moid: str,
    host_moid: str | None,
) -> dict[str, Any]:
    """Assemble the ``Folder.CreateVM_Task`` request body (#3099, #3117).

    ``CreateVMRequestType`` (spec-verified against the pinned
    ``vi-json.yaml``): ``{config: VirtualMachineConfigSpec, pool:
    ResourcePool-MoRef, host?: HostSystem-MoRef}``. The ConfigSpec carries
    the same logical inputs the REST CreateSpec did — ``name`` /
    ``guestId`` / ``numCPUs`` / ``memoryMB`` — plus the vim-only shape:
    ``files.vmPathName`` (the VM home, ``"[<datastore-name>] <name>"``) and
    a ``deviceChange`` list, and ``nestedHVEnabled`` folded inline when the
    operator asked for the #3093 leg. Every DataObject in the body carries
    its ``_typeName`` discriminator (#3103).

    The ``deviceChange`` list is always non-empty on this arm (#3117): it
    leads with a ``VirtualLsiLogicSASController`` add (so a fresh vim-arm VM
    always has a controller for the governed REST disk-add — REST
    ``POST:/vcenter/vm`` fabricates one, ``CreateVM_Task`` does not),
    followed by the per-disk ``VirtualDisk`` ``fileOperation: create`` adds
    bound to it, then the NIC (vmxnet3, negative temp keys — the new-device
    convention) adds.
    """
    config: dict[str, Any] = {
        _VMOMI_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE,
        "name": name,
        "guestId": guest_id,
        "numCPUs": cpu_count,
        "memoryMB": memory_mib,
        "files": {
            _VMOMI_TYPE_NAME_KEY: _VM_FILE_INFO_TYPE,
            "vmPathName": f"[{datastore_name}] {name}",
        },
    }
    if nested_hv:
        config["nestedHVEnabled"] = True
    nic_changes = [
        {
            _VMOMI_TYPE_NAME_KEY: _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
            "operation": "add",
            "device": {
                _VMOMI_TYPE_NAME_KEY: _VIRTUAL_VMXNET3_TYPE,
                "key": -(index + 1),
                "backing": backing,
            },
        }
        for index, backing in enumerate(nic_backings)
    ]
    config["deviceChange"] = [
        *_build_vim_disk_device_changes(disk_capacities_bytes),
        *nic_changes,
    ]
    body: dict[str, Any] = {"config": config, "pool": _moref(_RESOURCE_POOL_MO_TYPE, pool_moid)}
    if host_moid is not None:
        body["host"] = _moref(_HOST_SYSTEM_MO_TYPE, host_moid)
    return body


async def _issue_vim_create(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    folder_moid: str,
    body: dict[str, Any],
    gate_params: dict[str, Any],
    name: str,
) -> tuple[OperationResult | None, str | None, str | None]:
    """Gate + issue ``CreateVM_Task`` and poll it to terminal (#3099).

    Returns ``(gate, vm_id, failure_reason)``:

    * ``(gate, None, None)`` — the #2254 seam parked/denied the write.
    * ``(None, vm_id, None)`` — the task succeeded; ``vm_id`` is the new
      VirtualMachine moid from ``TaskInfo.result``.
    * ``(None, None, reason)`` — transport fault, task fault, poll timeout,
      or an unreadable task result; the caller folds *reason* into the
      ``rolled_back`` envelope (nothing was verifiably created, so there is
      nothing to delete — a timed-out create may still land in the
      background, which the reason spells out).
    """
    try:
        gate, task_payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=_OP_CREATE_VM_TASK,
            vmomi_path=f"/Folder/{folder_moid}/CreateVM_Task",
            body=body,
            params=gate_params,
        )
        if gate is not None:
            return gate, None, None
        outcome = await poll_vim_task(
            connector,
            target,
            operator,
            task=_unwrap_value(task_payload),
            timeout_seconds=_VM_CREATE_TASK_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return None, None, f"create via CreateVM_Task failed: {exc}"
    if outcome.state == TASK_STATE_ERROR:
        return (
            None,
            None,
            (
                f"CreateVM_Task creating {name!r} faulted: "
                f"{outcome.error_message or '<no fault reported>'}"
            ),
        )
    if outcome.timed_out:
        return (
            None,
            None,
            (
                f"CreateVM_Task {outcome.task} did not reach a terminal state within "
                f"{int(_VM_CREATE_TASK_TIMEOUT_SECONDS)}s; the create may still complete "
                "in the background — poll the task or list the folder before retrying"
            ),
        )
    vm_id = _extract_task_vm_moid(outcome.result)
    if vm_id is None:
        return None, None, "CreateVM_Task succeeded but its result carried no VirtualMachine moid"
    return None, vm_id, None


async def _power_on_created_vm(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_id: str,
) -> tuple[OperationResult | None, str | None]:
    """Issue the gated power-on leg of ``vm.create``; ``(gate, failure_reason)``.

    Shared by the REST and vim create arms (#3099) — the power endpoint is
    not part of the 8.0.x create defect, so both arms finish through the
    same REST sub-op. A parked/denied gate rides back for the caller to
    return verbatim; a transport fault becomes the failure reason the
    caller folds into the rollback contract.
    """
    try:
        gate, _ = await _write_sub_op(
            connector, target, operator, _power_vm_op_id("start"), {"vm": vm_id}
        )
    except httpx.HTTPError as exc:
        return None, f"power_on failed: {exc}"
    return gate, None


async def _vm_create_via_vim(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """The pre-9.0 arm of :func:`vm_create_composite` (#3099): vim ``CreateVM_Task``.

    Same logical inputs, same response envelope, same rollback contract as
    the REST arm — only the create transport differs. NICs, disks (each a
    ``VirtualDisk`` ``fileOperation: create`` add on the always-folded SCSI
    controller, #3117) and the optional ``nested_hv`` flag fold into the one
    ``VirtualMachineConfigSpec`` (the create is atomic vCenter-side, so
    there are no per-NIC / per-disk / per-flag partial-failure legs), and
    the task is polled to terminal through the shared #2893 substrate.
    Fail-closed validations run before any sub-call: an unmapped
    ``guest_os`` enum, missing ``resource_pool`` / ``datastore`` pins (vim
    needs an explicit pool and VM home — vCenter-side placement defaulting
    exists only on the REST create), an invalid disk ``capacity_gb``, and an
    unsupported NIC backing each return a structured ``rolled_back``.
    """
    folder_name = params.get("folder_name")
    name = params["name"]
    guest_os = params["guest_os"]
    cpu_count = int(params.get("cpu_count", 1))
    memory_mib = int(params.get("memory_mib", 1024))
    nics: list[dict[str, Any]] = list(params.get("nics") or [])
    disks: list[dict[str, Any]] = list(params.get("disks") or [])
    nested_hv = bool(params.get("nested_hv", False))
    power_on = bool(params.get("power_on_after_create", False))
    pool_moid = params.get("resource_pool")
    datastore_moid = params.get("datastore")
    host_moid = params.get("host")

    guest_id = _VIM_GUEST_ID_BY_REST_GUEST_OS.get(guest_os)
    if guest_id is None:
        return _rolled_back(
            steps=[],
            failed_step="guest_id_mapping",
            reason=(
                f"guest_os {guest_os!r} has no vim guestId mapping for the pre-9.0 "
                "CreateVM_Task arm; supported identifiers: "
                + ", ".join(sorted(_VIM_GUEST_ID_BY_REST_GUEST_OS))
            ),
        )
    if not isinstance(pool_moid, str) or not isinstance(datastore_moid, str):
        return _rolled_back(
            steps=[],
            failed_step="placement_params",
            reason=(
                "resource_pool and datastore moids are required on a pre-9.0 target: "
                "vim CreateVM_Task needs an explicit pool and a datastore for the VM "
                "home (files.vmPathName) — vCenter-side placement defaulting exists "
                "only on the REST create"
            ),
        )
    disk_capacities_bytes, disk_err = _disk_capacities_bytes(disks)
    if disk_capacities_bytes is None:
        return _rolled_back(steps=[], failed_step="disk_spec", reason=disk_err or "")

    steps: list[str] = []
    folder_moid: str | None
    folder_pin = params.get("folder")
    if isinstance(folder_pin, str):
        # #3115 explicit pin: the moid rides ``CreateVM_Task`` verbatim — no
        # display-name lookup, no ``folder_lookup`` ledger entry.
        folder_moid = folder_pin
    else:
        folder_moid, folder_err, folder_candidates = await _resolve_folder_moid(
            connector=connector,
            target=target,
            operator=operator,
            folder_name=folder_name,
            placement_pins={
                "host": host_moid,
                "resource_pool": pool_moid,
                "datastore": datastore_moid,
            },
        )
        if folder_moid is None:
            return _folder_lookup_rolled_back(steps, folder_err, folder_candidates)
        steps.append("folder_lookup")

    datastore_name, datastore_err = await _resolve_datastore_name(
        connector=connector, target=target, operator=operator, datastore_moid=datastore_moid
    )
    if datastore_name is None:
        return _rolled_back(steps=steps, failed_step="datastore_lookup", reason=datastore_err or "")

    nic_backings: list[dict[str, Any]] = []
    for nic in nics:
        backing, nic_err = await _resolve_vim_nic_backing(connector, target, operator, nic=nic)
        if backing is None:
            return _rolled_back(steps=steps, failed_step="network_lookup", reason=nic_err or "")
        nic_backings.append(backing)

    create_body = _build_vim_create_request(
        name=name,
        guest_id=guest_id,
        cpu_count=cpu_count,
        memory_mib=memory_mib,
        datastore_name=datastore_name,
        nic_backings=nic_backings,
        disk_capacities_bytes=disk_capacities_bytes,
        nested_hv=nested_hv,
        pool_moid=pool_moid,
        host_moid=host_moid if isinstance(host_moid, str) else None,
    )
    # Identity-only gate params: the durable ApprovalRequest names the
    # blast radius (what gets created, where) without the assembled vim
    # body — mirroring the clone-from-template gate discipline.
    gate_params = {
        "name": name,
        "folder_name": folder_name,
        "folder": folder_moid,
        "guest_os": guest_os,
        "resource_pool": pool_moid,
        "datastore": datastore_moid,
        "host": host_moid,
        "cpu_count": cpu_count,
        "memory_mib": memory_mib,
        "nics": [nic.get("network") for nic in nics],
        "disks": [disk.get("capacity_gb") for disk in disks],
        "nested_hv": nested_hv,
    }
    gate, vm_id, create_failure = await _issue_vim_create(
        connector=connector,
        target=target,
        operator=operator,
        folder_moid=folder_moid,
        body=create_body,
        gate_params=gate_params,
        name=name,
    )
    if gate is not None:
        return gate
    if create_failure is not None or vm_id is None:
        return _rolled_back(steps=steps, failed_step="create", reason=create_failure or "")
    steps.append("create")
    if disks:
        steps.append("disk_attach")
    if nics:
        steps.append("nic_attach")
    if nested_hv:
        steps.append("nested_hv")

    if power_on:
        gate, power_failure = await _power_on_created_vm(
            connector=connector, target=target, operator=operator, vm_id=vm_id
        )
        if gate is not None:
            return gate
        if power_failure is not None:
            await _rollback_created_vm(
                connector=connector, target=target, operator=operator, vm_id=vm_id
            )
            return _rolled_back(steps=steps, failed_step="power_on", reason=power_failure)
        steps.append("power_on")

    created: dict[str, Any] = {
        "status": "created",
        "vm_id": vm_id,
        "steps_succeeded": steps,
        "failed_step": None,
        "rollback_reason": None,
    }
    if "nested_hv" in params:
        created["nested_hv"] = nested_hv
    return created


async def vm_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a VM with NIC attach, optional VHV enable + optional power-on.

    Op-id: ``vmware.composite.vm.create``. See module docstring for the
    sub-op chain, the direct-session governance seam, and rollback semantics.
    The optional ``nested_hv`` leg (#3093) runs after NIC attach and strictly
    **before** any power-on — a powered-on reconfigure of
    ``nestedHVEnabled`` is invalid — and follows the same rollback contract
    as the other post-create steps. Optional placement pins (#3096:
    ``resource_pool`` / ``datastore`` / ``host`` moids) thread into the
    CreateSpec ``placement`` alongside the resolved folder moid; absent
    pins leave vCenter's placement defaulting untouched and keep the
    create body byte-identical to a pre-#3096 call.

    Storage (#3117): the optional ``disks`` param (``[{capacity_gb}]``)
    lands the VM with data disks in the one create. On the REST arm each
    disk threads into the CreateSpec ``disks`` (vCenter fabricates the
    controller); on the pre-9.0 vim arm — where ``CreateVM_Task`` builds
    the VM verbatim and adds no controller — the arm **always** folds a
    ``VirtualLsiLogicSASController`` (so a fresh VM has a controller for the
    governed REST disk-add even with no ``disks``) plus a ``VirtualDisk``
    ``fileOperation: create`` per requested disk. Absent ``disks`` keeps the
    REST create body byte-identical to a pre-#3117 call.

    Folder resolution (#3115): an explicit ``folder`` moid pin skips the
    display-name lookup entirely (on both arms; the ``folder_lookup``
    ledger entry is omitted); a multi-match ``folder_name`` lookup
    re-scopes via the placement pins' datacenter
    (:func:`_resolve_folder_moid`), and a residual ambiguity refuses with
    the candidate moids (``candidate_folders``) instead of silently
    taking the first row — the silent first-row pick created VMs in the
    wrong datacenter on multi-datacenter vCenters.

    Version-conditional create transport (#3099): when the live
    ``about.version`` major is < 9 the whole create rides vim
    ``Folder.CreateVM_Task`` (:func:`_vm_create_via_vim`) — bare REST
    ``POST /api/vcenter/vm`` is vendor-defective on vCenter 8.0.x — with
    NICs, disks and ``nested_hv`` folded into the one ConfigSpec. 9.0+ and
    unresolved versions keep the REST path below byte-identical.
    """
    about_version = await connector._about_version(target, operator)
    if _vim_create_required(about_version):
        return await _vm_create_via_vim(
            operator=operator, target=target, params=params, connector=connector
        )

    folder_name = params.get("folder_name")
    name = params["name"]
    guest_os = params["guest_os"]
    cpu_count = int(params.get("cpu_count", 1))
    memory_mib = int(params.get("memory_mib", 1024))
    nics: list[dict[str, Any]] = list(params.get("nics") or [])
    disks: list[dict[str, Any]] = list(params.get("disks") or [])
    placement_pins: dict[str, Any] = {
        key: params[key] for key in ("resource_pool", "datastore", "host") if key in params
    }
    nested_hv = bool(params.get("nested_hv", False))
    power_on = bool(params.get("power_on_after_create", False))

    disk_capacities_bytes, disk_err = _disk_capacities_bytes(disks)
    if disk_capacities_bytes is None:
        return _rolled_back(steps=[], failed_step="disk_spec", reason=disk_err or "")

    steps: list[str] = []

    folder_moid: str | None
    folder_pin = params.get("folder")
    if isinstance(folder_pin, str):
        # #3115 explicit pin: the moid rides the CreateSpec placement
        # verbatim — no display-name lookup, no ``folder_lookup`` ledger
        # entry.
        folder_moid = folder_pin
    else:
        folder_moid, folder_err, folder_candidates = await _resolve_folder_moid(
            connector=connector,
            target=target,
            operator=operator,
            folder_name=folder_name,
            placement_pins=placement_pins,
        )
        if folder_moid is None:
            return _folder_lookup_rolled_back(steps, folder_err, folder_candidates)
        steps.append("folder_lookup")

    create_spec: dict[str, Any] = {
        "name": name,
        "guest_OS": guest_os,
        "placement": {"folder": folder_moid, **placement_pins},
        "cpu": {"count": cpu_count},
        "memory": {"size_MiB": memory_mib},
    }
    if disk_capacities_bytes:
        # #3117: fold the requested disks into the CreateSpec inline (vCenter
        # fabricates the controller). Absent disks keep the create body
        # byte-identical to the pre-#3117 shape.
        create_spec["disks"] = _build_rest_disk_specs(disk_capacities_bytes)
    try:
        gate, create_payload = await _write_sub_op(
            connector, target, operator, _OP_CREATE_VM, create_spec
        )
    except httpx.HTTPError as exc:
        # Create failed; nothing to roll back.
        return _rolled_back(steps=steps, failed_step="create", reason=f"create failed: {exc}")
    if gate is not None:
        return gate
    vm_id = _unwrap_value(create_payload)
    if not isinstance(vm_id, str):
        return _rolled_back(
            steps=steps,
            failed_step="create",
            reason=f"create returned non-string vm id payload: {type(vm_id).__name__}",
        )
    steps.append("create")
    if disks:
        # Folded into the CreateSpec above — a distinct ledger entry so the
        # envelope names the storage that landed, mirroring ``nic_attach``.
        steps.append("disk_attach")

    for nic in nics:
        # ``Ethernet.CreateSpec``: the network rides the ``backing`` spec
        # (type + network id); a bare top-level ``network`` key exists on no
        # NIC resource in the pinned spec (#2970).
        nic_spec = {
            "backing": {
                "type": nic.get("backing_type", _NIC_BACKING_STANDARD_PORTGROUP),
                "network": nic.get("network"),
            }
        }
        try:
            gate, _ = await _write_sub_op(
                connector, target, operator, _OP_CREATE_VM_NIC, {"vm": vm_id, **nic_spec}
            )
        except httpx.HTTPError as exc:
            await _rollback_created_vm(
                connector=connector, target=target, operator=operator, vm_id=vm_id
            )
            return _rolled_back(
                steps=steps,
                failed_step="nic_attach",
                reason=f"nic attach for network={nic.get('network')!r} failed: {exc}",
            )
        if gate is not None:
            return gate
    if nics:
        steps.append("nic_attach")

    if nested_hv:
        gate, vhv_failure = await _enable_nested_hv(
            connector=connector, target=target, operator=operator, vm_id=vm_id
        )
        if gate is not None:
            return gate
        if vhv_failure is not None:
            await _rollback_created_vm(
                connector=connector, target=target, operator=operator, vm_id=vm_id
            )
            return _rolled_back(steps=steps, failed_step="nested_hv", reason=vhv_failure)
        steps.append("nested_hv")

    if power_on:
        gate, power_failure = await _power_on_created_vm(
            connector=connector, target=target, operator=operator, vm_id=vm_id
        )
        if gate is not None:
            return gate
        if power_failure is not None:
            await _rollback_created_vm(
                connector=connector, target=target, operator=operator, vm_id=vm_id
            )
            return _rolled_back(steps=steps, failed_step="power_on", reason=power_failure)
        steps.append("power_on")

    created: dict[str, Any] = {
        "status": "created",
        "vm_id": vm_id,
        "steps_succeeded": steps,
        "failed_step": None,
        "rollback_reason": None,
    }
    if "nested_hv" in params:
        # Applied state, echoed only when the operator asked for the leg —
        # a param-absent call keeps today's envelope byte-identical (#3093).
        created["nested_hv"] = nested_hv
    return created


# ===========================================================================
# vm.clone
# ===========================================================================


def _extract_cis_task_id(payload: Any) -> str | None:
    """Pull a cis task id out of a ``vmw-task=true`` 202 response payload.

    The pinned spec types the 202 body as the bare task-id string; the
    legacy ``/rest`` mount wraps it (``{"value": ...}``) and some builds
    key it ``{"task": ...}`` -- all three shapes are tolerated.
    """
    unwrapped = _unwrap_value(payload)
    if isinstance(unwrapped, dict):
        candidate = unwrapped.get("task") or unwrapped.get("value")
        if isinstance(candidate, str):
            return candidate
    elif isinstance(unwrapped, str):
        return unwrapped
    return None


async def _poll_cis_task(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    task_id: str,
    timeout_seconds: float,
) -> str | None:
    """Poll ``GET:/cis/tasks/{task}`` to a terminal state; ``None`` on success.

    Returns an operator-facing failure reason on FAILED / deadline elapse
    so the caller can fold it into its own status envelope (the
    cluster.patch per-host loop maps it to ``status='stopped'``).
    Transport faults raise :exc:`httpx.HTTPError` for the caller.
    """
    deadline = time.monotonic() + timeout_seconds
    poll_interval = 1.0
    while time.monotonic() < deadline:
        task_payload = await _read_sub_op(
            connector, target, operator, _OP_GET_TASK, {"task": task_id}
        )
        task = _unwrap_value(task_payload)
        if isinstance(task, dict):
            status = task.get("status")
            if status == "SUCCEEDED":
                return None
            if status == "FAILED":
                return f"cis task {task_id!r} reported FAILED: " + str(
                    task.get("error") or "<no error reported>"
                )
        await asyncio.sleep(poll_interval)
    return (
        f"cis task {task_id!r} did not reach a terminal state within "
        f"{int(timeout_seconds)}s (poll GET:/cis/tasks/{task_id} for final state)"
    )


async def vm_clone_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Clone a VM from a content-library template via the synchronous deploy.

    Op-id: ``vmware.composite.vm.clone``. The pinned spec's
    ``Vcenter.VmTemplate.LibraryItems_deploy`` is synchronous -- its 200
    body is the deployed VM id, and no ``vmw-task=true`` variant of the
    path exists (#2970) -- so the composite returns ``status='completed'``
    with the new VM id directly; there is no task to poll.
    """
    source_vm = params["source_vm"]
    target_name = params["target_name"]
    library_item = params["library_item"]

    # Source config drives CloneSpec; the read is a no-op when the
    # source VM lookup fails (httpx.HTTPError surfaces upstream).
    await _read_sub_op(connector, target, operator, _OP_GET_VM, {"vm": source_vm})

    gate, deploy_payload = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_DEPLOY_LIBRARY_VM,
        {"templateLibraryItem": library_item, "spec": {"name": target_name}},
        # Synchronous deploy — the POST is held open for the whole copy (#3076).
        timeout=_LIBRARY_ITEM_DEPLOY_TIMEOUT,
    )
    if gate is not None:
        return gate
    vm_id = _unwrap_value(deploy_payload)
    if not isinstance(vm_id, str):
        raise RuntimeError(
            f"vm.clone: deploy returned no VM id (payload={deploy_payload!r}); the "
            "pinned deploy operation is synchronous and its 200 body is the "
            "deployed VirtualMachine id"
        )

    return {
        "status": "completed",
        "task_id": None,
        "vm_id": vm_id,
        "guidance": None,
    }


# ===========================================================================
# vm.deploy_from_library (OVF/OVA content-library deploy -- #2909)
# ===========================================================================
#
# Retires ``govc library.deploy`` for OVF/OVA appliances (the HoloRouter OVA
# and friends). The deploy is the synchronous
# ``POST:/vcenter/ovf/library-item/{ovfLibraryItemId}?action=deploy``; unlike
# ``vm.clone`` (whose 200 body is a bare VM id) its 200 body is a
# ``DeploymentResult`` structure, so a deploy that fails OVF / network-mapping
# / placement validation returns ``succeeded=false`` with an error report —
# surfaced here as a structured ``deploy_failed`` status, never a raw fault.
# Name-based item resolution rides the content-library find actions (both in
# the pinned ``vcenter.yaml``), un-gated reads that mutate nothing.


def _deploy_issue(category: str, severity: str, message: str) -> dict[str, Any]:
    """Build one issue projection ``{category, severity, message}``."""
    return {"category": category, "severity": severity, "message": message}


def _deploy_failure(
    status: str,
    *,
    library_item_id: str | None = None,
    candidates: list[str] | None = None,
    issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a non-``deployed`` response envelope (resolution / deploy failure)."""
    return {
        "status": status,
        "vm_id": None,
        "resource_type": None,
        "library_item_id": library_item_id,
        "powered_on": False,
        "issues": issues or [],
        "candidates": candidates,
    }


def _ovf_message(entry: dict[str, Any]) -> str:
    """Best human-readable message from one OVF error/warning/info entry.

    An ``Vcenter.Ovf.OvfError`` carries the localizable text under
    ``message.default_message`` (INPUT category), or under
    ``error.messages[].default_message`` (SERVER category); a VALIDATION
    entry may carry only a ``name``. Defensive: any missing shape yields a
    placeholder, never a raise.
    """
    message = entry.get("message")
    if isinstance(message, dict):
        default = message.get("default_message")
        if isinstance(default, str) and default:
            return default
    error = entry.get("error")
    if isinstance(error, dict):
        for msg in error.get("messages") or []:
            if isinstance(msg, dict):
                default = msg.get("default_message")
                if isinstance(default, str) and default:
                    return default
    name = entry.get("name")
    if isinstance(name, str) and name:
        return f"invalid OVF input parameter {name!r}"
    return "<no message reported>"


def _extract_ovf_issues(error_report: Any) -> list[dict[str, Any]]:
    """Flatten an OVF ``ResultInfo`` (errors / warnings / information) to issues.

    Returns one projection per reported message, severity-tagged. Empty when
    the report is missing/malformed or carries no messages — so a clean
    deploy yields ``[]`` and a warning-only deploy still surfaces the
    warnings on the ``deployed`` envelope.
    """
    if not isinstance(error_report, dict):
        return []
    issues: list[dict[str, Any]] = []
    for severity, key in (("error", "errors"), ("warning", "warnings"), ("info", "information")):
        for entry in error_report.get(key) or []:
            if isinstance(entry, dict):
                category = entry.get("category")
                issues.append(
                    _deploy_issue(
                        category if isinstance(category, str) else "unknown",
                        severity,
                        _ovf_message(entry),
                    )
                )
    return issues


async def _find_content_library_ids(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    op_id: str,
    spec: dict[str, Any],
) -> list[str]:
    """Issue one content-library ``?action=find`` POST as an un-gated read.

    The find actions (``Content.Library_find`` / ``Content.Library.Item_find``)
    return a bare array of id strings and mutate nothing, so they skip the
    :func:`enforce_subop_policy` seam like the REST listing reads — but they
    are POST-shaped, so they ride ``_post_json`` rather than :func:`_read_sub_op`
    (which is GET-only). The ``FindSpec`` is sent at the **top level** of the
    request body -- the modern ``/api`` surface takes the ``FindSpec`` directly
    (``Content.Library.FindSpec`` / ``Content.Library.Item.FindSpec``), not the
    legacy ``/rest`` ``{"spec": {...}}`` envelope, which it rejects with
    ``400 INVALID_ARGUMENT`` (#3071 — the resolution-step sibling of the
    write-body envelope fix #2973). Transport faults raise
    :exc:`httpx.HTTPError` for the caller.
    """
    method, _, path = op_id.partition(":")
    mounted = await connector.mount_op_path(target, path, operator)
    payload = await connector._post_json(target, mounted, operator=operator, verb=method, json=spec)
    ids = _unwrap_value(payload)
    if not isinstance(ids, list):
        raise RuntimeError(f"expected id list from {op_id!r}, got {type(ids).__name__}")
    return [entry for entry in ids if isinstance(entry, str)]


async def _resolve_deploy_library_item(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    params: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve the OVF library-item id from *params*.

    Returns ``(item_id, None)`` on success or ``(None, envelope)`` with a
    terminal response dict on failure (``invalid_reference`` /
    ``library_not_found`` / ``ambiguous_library`` / ``item_not_found`` /
    ``ambiguous_item``). ``library_item`` (an id) short-circuits; otherwise
    ``library_item_name`` is resolved via ``Content.Library.Item_find``
    (filtered to ``type=ovf``), optionally scoped to the library
    ``library_name`` resolves to via ``Content.Library_find``. Ambiguity is
    refused before any deploy so the operator re-dispatches by explicit id.
    """
    passthrough = params.get("library_item")
    if isinstance(passthrough, str) and passthrough:
        return passthrough, None

    item_name = params.get("library_item_name")
    if not isinstance(item_name, str) or not item_name:
        return None, _deploy_failure(
            "invalid_reference",
            issues=[
                _deploy_issue(
                    "input",
                    "error",
                    "supply library_item (id) or library_item_name to identify the OVF item",
                )
            ],
        )

    library_id: str | None = None
    library_name = params.get("library_name")
    if isinstance(library_name, str) and library_name:
        library_ids = await _find_content_library_ids(
            connector, target, operator, op_id=_OP_FIND_LIBRARY, spec={"name": library_name}
        )
        if not library_ids:
            return None, _deploy_failure(
                "library_not_found",
                issues=[
                    _deploy_issue("input", "error", f"library {library_name!r} matched no library")
                ],
            )
        if len(library_ids) > 1:
            return None, _deploy_failure(
                "ambiguous_library",
                candidates=library_ids,
                issues=[
                    _deploy_issue(
                        "input",
                        "error",
                        f"library {library_name!r} matched {len(library_ids)} libraries",
                    )
                ],
            )
        library_id = library_ids[0]

    item_spec: dict[str, Any] = {"name": item_name, "type": _OVF_LIBRARY_ITEM_TYPE}
    if library_id is not None:
        item_spec["library_id"] = library_id
    item_ids = await _find_content_library_ids(
        connector, target, operator, op_id=_OP_FIND_LIBRARY_ITEM, spec=item_spec
    )
    if not item_ids:
        return None, _deploy_failure(
            "item_not_found",
            issues=[
                _deploy_issue("input", "error", f"OVF item {item_name!r} matched no library item")
            ],
        )
    if len(item_ids) > 1:
        return None, _deploy_failure(
            "ambiguous_item",
            candidates=item_ids,
            issues=[
                _deploy_issue(
                    "input", "error", f"OVF item {item_name!r} matched {len(item_ids)} items"
                )
            ],
        )
    return item_ids[0], None


def _deploy_eula_field_name(about_version: str | None) -> str:
    """Return the OVF ``ResourcePoolDeploymentSpec`` EULA-accept wire field name.

    A genuine 8.0-vs-9.0 field-name divergence: the pinned 9.0 Automation
    spec keys the field ``accept_all_eula`` (lowercase), but vCenter 8.0.x —
    and every pre-9.0 release — rejects that key with ``HTTP 400
    UNEXPECTED_INPUT`` and expects the legacy Automation name
    ``accept_all_EULA`` (``govmomi``'s ``DeploymentSpec.AcceptAllEULA`` ⇒
    ``accept_all_EULA``; a live ``govc library.deploy`` onto 8.0.3 succeeded
    with that shape). Gate off the live ``about.version`` major component: a
    resolvable pre-9.0 major gets the caps form; 9.0+ — and an unresolved
    version, which falls back to the pinned-spec form — keep the lowercase
    form so 9.0 dispatch stays byte-identical.
    """
    major = about_version.split(".", 1)[0].strip() if about_version else ""
    if major.isdigit() and int(major) < 9:
        return "accept_all_EULA"
    return "accept_all_eula"


def _build_ovf_deploy_body(params: dict[str, Any], eula_field: str) -> dict[str, Any]:
    """Build the ``{deployment_spec, target}`` OVF deploy request body.

    ``resource_pool`` is the required deploy-target anchor; ``host`` / ``folder``
    refine it. ``network_mappings`` is sent verbatim as the OVF-key → network-moid
    map (the pinned 9.0 spec models it as a map, not an array). ``ovf_properties``
    folds into a single ``PropertyParams`` entry in ``additional_parameters``.
    The EULA-accept flag defaults to true (deploying a curated item accepts its
    EULA) and is keyed by *eula_field* — the version-aware wire name resolved by
    :func:`_deploy_eula_field_name`.
    """
    deployment_spec: dict[str, Any] = {eula_field: bool(params.get("accept_all_eula", True))}
    _put_if_str(deployment_spec, "name", params.get("name"))
    network_mappings = params.get("network_mappings")
    if isinstance(network_mappings, dict) and network_mappings:
        deployment_spec["network_mappings"] = {str(k): str(v) for k, v in network_mappings.items()}
    _put_if_str(deployment_spec, "storage_provisioning", params.get("storage_provisioning"))
    _put_if_str(deployment_spec, "storage_profile_id", params.get("storage_profile"))
    _put_if_str(deployment_spec, "default_datastore_id", params.get("datastore"))
    ovf_properties = params.get("ovf_properties")
    if isinstance(ovf_properties, dict) and ovf_properties:
        deployment_spec["additional_parameters"] = [
            {
                "type": _OVF_PROPERTY_PARAMS_TYPE,
                "properties": [{"id": str(k), "value": str(v)} for k, v in ovf_properties.items()],
            }
        ]

    deploy_target: dict[str, Any] = {"resource_pool_id": params["resource_pool"]}
    _put_if_str(deploy_target, "host_id", params.get("host"))
    _put_if_str(deploy_target, "folder_id", params.get("folder"))
    return {"deployment_spec": deployment_spec, "target": deploy_target}


async def _power_on_deployed_vm(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_id: str,
) -> tuple[bool, dict[str, Any] | None]:
    """Best-effort power-on of a freshly deployed VM; ``(powered_on, issue?)``.

    A power-on fault (or, defensively, a parked gate that cannot occur once
    the deploy write cleared the same posture) never demotes the already-
    successful deploy — it returns ``(False, issue)`` so the caller keeps
    ``status='deployed'`` and folds the issue into the report.
    """
    try:
        gate, _ = await _write_sub_op(
            connector, target, operator, _power_vm_op_id("start"), {"vm": vm_id}
        )
    except httpx.HTTPError as exc:
        return False, _deploy_issue(
            "power_on", "warning", f"deploy succeeded but power-on failed: {exc}"
        )
    if gate is not None:
        return False, _deploy_issue(
            "power_on", "warning", "deploy succeeded but the follow-on power-on was not authorized"
        )
    return True, None


async def vm_deploy_from_library_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Deploy an OVF/OVA content-library item to a new VM.

    Op-id: ``vmware.composite.vm.deploy_from_library``. Resolves the library
    item (id passthrough or name lookup, ambiguity-refusing), issues the
    synchronous OVF deploy through the governed direct-session seam, and maps
    the ``DeploymentResult`` to a structured envelope: ``deployed`` on
    ``succeeded=true``, ``deploy_failed`` on ``succeeded=false`` (with the
    report's per-issue messages), or ``deploy_error`` when the deploy call
    itself faults (HTTP 400/404 for invalid / missing placement resources) —
    so a placement or network-mapping error is a structured status, never a
    raw vendor error. With ``power_on`` the deployed VM is started best-effort.
    """
    try:
        item_id, resolution_error = await _resolve_deploy_library_item(
            connector=connector, target=target, operator=operator, params=params
        )
    except httpx.HTTPError as exc:
        # A content-library ``?action=find`` call faulted (e.g. a 4xx). Surface
        # the vCenter status + message as a structured ``resolve_error`` instead
        # of letting the raw ``httpx.HTTPStatusError`` escape as an opaque
        # ``connector_error`` (#3071).
        return _deploy_failure(
            "resolve_error",
            issues=[
                _deploy_issue(
                    "resolve",
                    "error",
                    f"content-library lookup faulted: {_vsphere_fault_detail(exc)}",
                )
            ],
        )
    if resolution_error is not None:
        return resolution_error
    assert item_id is not None  # resolution_error is None ⇒ item_id resolved

    about_version = await connector._about_version(target, operator)
    eula_field = _deploy_eula_field_name(about_version)
    deploy_params = {"ovfLibraryItemId": item_id, **_build_ovf_deploy_body(params, eula_field)}
    try:
        gate, deploy_payload = await _write_sub_op(
            connector,
            target,
            operator,
            _OP_DEPLOY_OVF_LIBRARY_ITEM,
            deploy_params,
            # Synchronous deploy — the POST is held open for the whole copy (#3076).
            timeout=_LIBRARY_ITEM_DEPLOY_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        return _deploy_failure(
            "deploy_error",
            library_item_id=item_id,
            issues=[
                _deploy_issue(
                    "placement",
                    "error",
                    f"OVF deploy call faulted: {_vsphere_fault_detail(exc)}",
                )
            ],
        )
    if gate is not None:
        return gate

    result = _unwrap_value(deploy_payload)
    if not isinstance(result, dict):
        raise RuntimeError(
            f"vm.deploy_from_library: deploy returned no DeploymentResult "
            f"(payload={deploy_payload!r}); the pinned deploy is synchronous and its "
            "200 body is a DeploymentResult structure"
        )
    issues = _extract_ovf_issues(result.get("error"))
    if not result.get("succeeded"):
        return _deploy_failure("deploy_failed", library_item_id=item_id, issues=issues)

    resource = result.get("resource_id")
    vm_id = resource.get("id") if isinstance(resource, dict) else None
    resource_type = resource.get("type") if isinstance(resource, dict) else None
    if not isinstance(vm_id, str):
        raise RuntimeError(
            "vm.deploy_from_library: deploy reported succeeded=true but no "
            f"resource_id.id (resource_id={resource!r})"
        )

    powered_on = False
    if bool(params.get("power_on", False)):
        powered_on, power_issue = await _power_on_deployed_vm(connector, target, operator, vm_id)
        if power_issue is not None:
            issues.append(power_issue)

    return {
        "status": "deployed",
        "vm_id": vm_id,
        "resource_type": resource_type if isinstance(resource_type, str) else None,
        "library_item_id": item_id,
        "powered_on": powered_on,
        "issues": issues,
        "candidates": None,
    }


# ===========================================================================
# vm.snapshot.revert (VI-JSON -- #2970)
# ===========================================================================
#
# The pinned vcenter.yaml serves NO snapshot resource at all (no
# ``GET:/vcenter/vm/{vm}/snapshot``, no ``?action=revert`` path -- the
# #2970 reconcile finding), so both halves ride vim: the listing is a
# ``RetrievePropertiesEx`` read of ``VirtualMachine.snapshot``
# (``VirtualMachineSnapshotInfo.rootSnapshotList``, a recursive
# ``VirtualMachineSnapshotTree``), and the revert is the governed
# ``VirtualMachineSnapshot.RevertToSnapshot_Task`` write polled to a
# terminal state -- the #2893 substrate.


def _build_single_prop_retrieve_params(mo_type: str, moid: str, prop: str) -> dict[str, Any]:
    """Build a ``RetrievePropertiesEx`` body reading one property of one object.

    A single ``PropertyFilterSpec`` scoped directly to the managed object;
    the singleton ``propertyCollector`` moId rides the path, so the body is
    only the method args (the shape the typed reads + the disk-grow /
    clone-template config reads send), ``_typeName``-annotated via the
    shared trio helper — the live-verified 200 shape (#3103).
    """
    return retrieve_properties_body(mo_type, [moid], [prop])


def _extract_single_prop(retrieve_result: Any, prop: str) -> Any:
    """Pull one property's ``val`` off a single-object RetrievePropertiesEx result.

    ``val`` is an ``Any`` placeholder, so live VI-JSON boxes primitives and
    arrays in it -- :func:`unwrap_vim_value` strips the boxes (#3106).
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
                return unwrap_vim_value(entry.get("val"))
    return None


def _flatten_snapshot_tree(nodes: Any) -> list[dict[str, Any]]:
    """Flatten a ``VirtualMachineSnapshotTree`` list into ``{name, snapshot}`` rows.

    Walks ``childSnapshotList`` depth-first. Each row carries the snapshot
    display ``name`` and the ``VirtualMachineSnapshot`` moid (the MoRef's
    ``value``) -- the same two keys the pre-#2970 REST listing rows carried,
    so the ambiguity / not-found envelopes keep their candidate shape.
    """
    rows: list[dict[str, Any]] = []
    if not isinstance(nodes, list):
        return rows
    for node in nodes:
        if not isinstance(node, dict):
            continue
        moref = node.get("snapshot")
        moid = moref.get("value") if isinstance(moref, dict) else None
        name = node.get("name")
        if isinstance(moid, str) and isinstance(name, str):
            rows.append({"name": name, "snapshot": moid})
        rows.extend(_flatten_snapshot_tree(node.get("childSnapshotList")))
    return rows


async def vm_snapshot_revert_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Revert a VM to a named snapshot. Idempotent; ambiguity-rejecting.

    Op-id: ``vmware.composite.vm.snapshot.revert``. Multiple snapshots
    sharing the name -> ``status='ambiguous'``; missing -> ``not_found``.
    Revert never dispatches on either. The revert is a vim ``*_Task``:
    a task fault raises (the dispatcher wraps it ``connector_error``);
    a poll timeout returns ``status='timeout'`` with the task id.
    """
    vm_moid = params["vm"]
    snapshot_name = params["snapshot_name"]

    tree_result = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=_build_single_prop_retrieve_params(_VIRTUAL_MACHINE_MO_TYPE, vm_moid, _PROP_SNAPSHOT),
    )
    snapshot_info = _extract_single_prop(tree_result, _PROP_SNAPSHOT)
    root_list = snapshot_info.get("rootSnapshotList") if isinstance(snapshot_info, dict) else None
    entries = _flatten_snapshot_tree(root_list)
    matches = [e for e in entries if e["name"] == snapshot_name]
    if not matches:
        return {
            "status": "not_found",
            "snapshot_id": None,
            "candidates": [],
            "guidance": f"no snapshot named {snapshot_name!r} on vm {vm_moid!r}",
        }
    if len(matches) > 1:
        return {
            "status": "ambiguous",
            "snapshot_id": None,
            "candidates": matches,
            "guidance": (
                "multiple snapshots share the requested name -- pass "
                "``snapshot_id`` explicitly to disambiguate"
            ),
        }
    snapshot_moid = matches[0]["snapshot"]
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_REVERT_TO_SNAPSHOT_TASK,
        vmomi_path=f"/{_VM_SNAPSHOT_MO_TYPE}/{snapshot_moid}/RevertToSnapshot_Task",
        body={},
        params={"vm": vm_moid, "snapshot_name": snapshot_name, "snapshot": snapshot_moid},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_SNAPSHOT_REVERT_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"vm.snapshot.revert: RevertToSnapshot_Task on vm {vm_moid!r} snapshot "
            f"{snapshot_moid!r} faulted: {outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return {
            "status": "timeout",
            "snapshot_id": snapshot_moid,
            "candidates": [],
            "guidance": (
                f"RevertToSnapshot_Task {outcome.task} did not reach a terminal state "
                f"within {int(_SNAPSHOT_REVERT_TASK_TIMEOUT_SECONDS)}s; poll the task -- "
                "the revert may still complete in the background"
            ),
        }
    return {
        "status": "reverted",
        "snapshot_id": snapshot_moid,
        "candidates": [],
        "guidance": None,
    }


# ===========================================================================
# vm.destroy — the first governed destructive delete (#3198)
# ===========================================================================


def _vim_destroy_required(about_version: str | None) -> bool:
    """``True`` when the live ``about.version`` major is a resolvable pre-9.0.

    The #3198 destroy arm gate, mirroring :func:`_vim_create_required`
    (#3099): a resolvable pre-9.0 major routes the destroy through the vim
    ``VirtualMachine.Destroy_Task`` (task-polled) — the pyvmomi coverage the
    dual-transport connector keeps for older vCenter, where REST lifecycle
    coverage is partial (the versioned-connector posture). 9.0+ — and an
    unresolved version — issue the synchronous REST ``DELETE:/vcenter/vm/{vm}``
    unchanged.
    """
    major = about_version.split(".", 1)[0].strip() if about_version else ""
    return major.isdigit() and int(major) < 9


async def _read_vm_snapshots_best_effort(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm_moid: str,
) -> list[dict[str, Any]]:
    """Best-effort snapshot enumeration for the destroy blast radius (#3198).

    Reads ``VirtualMachine.snapshot`` via the vim ``RetrievePropertiesEx``
    (the pinned REST spec serves no snapshot resource, #2970) and flattens
    the tree to ``[{snapshot, name, ...}]`` via :func:`_flatten_snapshot_tree`.
    A transport fault, or an absent/empty snapshot tree, yields ``[]`` — the
    enumeration is a reviewer convenience on the blast radius, never
    load-bearing for the park, so it degrades to "no snapshots enumerated"
    rather than sinking the whole preview.
    """
    try:
        tree_result = await connector._post_vmomi_json(
            target,
            _VMOMI_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=_build_single_prop_retrieve_params(
                _VIRTUAL_MACHINE_MO_TYPE, vm_moid, _PROP_SNAPSHOT
            ),
        )
    except httpx.HTTPError:
        return []
    snapshot_info = _extract_single_prop(tree_result, _PROP_SNAPSHOT)
    root_list = snapshot_info.get("rootSnapshotList") if isinstance(snapshot_info, dict) else None
    return _flatten_snapshot_tree(root_list)


async def vm_destroy_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Permanently destroy (delete) a powered-off VM and all of its disks.

    Op-id: ``vmware.composite.vm.destroy``. The FIRST governed destructive
    delete (#3198) — ``safety_level="destructive"``, so it rides the hardest
    gate MEHO has (decision ``docs/decisions/governed-delete-operations.md``):
    mandatory human approval (no agent path, no standing grant, no
    self-approval even under break-glass), a mandatory preview-hash binding,
    and a mandatory blast-radius statement (built by
    :func:`._write_preview._vm_destroy_preview`).

    **Fail-closed on a running VM.** vSphere faults a destroy on a VM that is
    not powered off, so the composite live-re-reads the power state and
    refuses with ``status="not_powered_off"`` unless the VM is
    ``POWERED_OFF``. It issues **no implicit power-off** — powering the VM
    down is a separate, deliberate decision the operator makes through the
    governed ``vmware.composite.vm.power`` op first. The re-read happens at
    dispatch time (post-approval), so a VM powered on between park and
    approval is still refused.

    **Dual arm** (mirrors :func:`vm_create_composite`): a resolvable pre-9.0
    ``about.version`` routes the destroy through the vim
    ``VirtualMachine.Destroy_Task`` (task-polled via the governed
    :func:`_write_vmomi_sub_op` seam, like ``vm.disk.grow`` #2893); 9.0+ (and
    an unresolved version) issues the synchronous REST
    ``DELETE:/vcenter/vm/{vm}`` (:func:`_write_sub_op`). A task fault raises
    (the dispatcher wraps it ``connector_error``); a poll timeout returns
    ``status="timeout"`` with the task id.
    """
    vm_moid = params["vm"]

    # Fail-closed power-state re-read (post-approval): a running VM faults the
    # destroy at the vendor, and the composite never powers it off implicitly.
    info = await _read_vm_info(
        connector=connector, target=target, operator=operator, vm_moid=vm_moid
    )
    if info is None:
        return {
            "status": "not_found",
            "vm_id": None,
            "power_state": None,
            "arm": None,
            "task_id": None,
            "guidance": f"vm {vm_moid!r} not found or unreadable; nothing destroyed",
        }
    power_state = info.get("power_state")
    if power_state != "POWERED_OFF":
        return {
            "status": "not_powered_off",
            "vm_id": vm_moid,
            "power_state": power_state,
            "arm": None,
            "task_id": None,
            "guidance": (
                f"refusing to destroy vm {vm_moid!r} in power state {power_state!r} "
                "(vSphere faults a destroy on a VM that is not powered off); power "
                "it off first via vmware.composite.vm.power — the destroy issues no "
                "implicit power-off"
            ),
        }

    about_version = await connector._about_version(target, operator)
    if _vim_destroy_required(about_version):
        gate, task_payload = await _write_vmomi_sub_op(
            connector,
            target,
            operator,
            op_id=_OP_DESTROY_VM_TASK,
            vmomi_path=f"/{_VIRTUAL_MACHINE_MO_TYPE}/{vm_moid}/Destroy_Task",
            body={},
            params={"vm": vm_moid},
        )
        if gate is not None:
            return gate
        outcome = await poll_vim_task(
            connector,
            target,
            operator,
            task=_unwrap_value(task_payload),
            timeout_seconds=_VM_DESTROY_TASK_TIMEOUT_SECONDS,
        )
        if outcome.state == TASK_STATE_ERROR:
            raise RuntimeError(
                f"vm.destroy: Destroy_Task on vm {vm_moid!r} faulted: "
                f"{outcome.error_message or '<no fault reported>'}"
            )
        if outcome.timed_out:
            return {
                "status": "timeout",
                "vm_id": vm_moid,
                "power_state": power_state,
                "arm": "vim",
                "task_id": outcome.task,
                "guidance": (
                    f"Destroy_Task {outcome.task} did not reach a terminal state "
                    f"within {int(_VM_DESTROY_TASK_TIMEOUT_SECONDS)}s; poll the task — "
                    "the destroy may still complete in the background"
                ),
            }
        return {
            "status": "destroyed",
            "vm_id": vm_moid,
            "power_state": power_state,
            "arm": "vim",
            "task_id": outcome.task,
            "guidance": None,
        }

    gate, _payload = await _write_sub_op(
        connector, target, operator, _OP_DELETE_VM, {"vm": vm_moid}
    )
    if gate is not None:
        return gate
    return {
        "status": "destroyed",
        "vm_id": vm_moid,
        "power_state": power_state,
        "arm": "rest",
        "task_id": None,
        "guidance": None,
    }


# ===========================================================================
# vm.migrate
# ===========================================================================


def _pick_drs_target_host(recs: Any, vm_moid: str) -> str | None:
    """Walk ``ClusterComputeResource.drsRecommendation`` for ``vm_moid``'s target.

    The property is a ``ClusterDrsRecommendation[]``; each recommendation
    carries a ``migrationList`` of ``ClusterDrsMigration`` rows whose
    ``vm`` / ``destination`` MoRefs name the VM and its recommended target
    host (spec-verified against the pinned ``vi-json.yaml``).
    """
    if not isinstance(recs, list):
        return None
    for rec in recs:
        if not isinstance(rec, dict):
            continue
        for migration in rec.get("migrationList") or []:
            if not isinstance(migration, dict):
                continue
            vm_ref = migration.get("vm")
            if not isinstance(vm_ref, dict) or vm_ref.get("value") != vm_moid:
                continue
            destination = migration.get("destination")
            candidate = destination.get("value") if isinstance(destination, dict) else None
            if isinstance(candidate, str):
                return candidate
    return None


async def vm_migrate_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Migrate a VM via DRS recommendation or explicit ``target_host``.

    Op-id: ``vmware.composite.vm.migrate``. ``target_host`` overrides
    the DRS lookup. No-recommendation path returns
    ``status='no_recommendation'`` so the caller can re-dispatch. The
    DRS lookup is a vim ``RetrievePropertiesEx`` read of the cluster's
    ``drsRecommendation`` property -- the pinned vcenter.yaml serves no
    DRS REST resource at all (#2970).

    Also the recursion target of :func:`host_evacuate_composite`: invoked
    through ``dispatch_child`` there, the dispatcher resolves this composite
    and injects ``connector`` so its own relocate write runs on the direct
    session under the governance seam, exactly as a top-level dispatch does.
    """
    vm_moid = params["vm"]
    cluster_moid = params["cluster"]
    explicit_target = params.get("target_host")
    target_host: str | None = None
    source = "none"

    if isinstance(explicit_target, str):
        target_host = explicit_target
        source = "operator"
    else:
        recs_payload = await connector._post_vmomi_json(
            target,
            _VMOMI_RETRIEVE_PROPERTIES_PATH,
            operator=operator,
            json=_build_single_prop_retrieve_params(
                _CLUSTER_COMPUTE_RESOURCE_MO_TYPE, cluster_moid, _PROP_DRS_RECOMMENDATION
            ),
        )
        recommendations = _extract_single_prop(recs_payload, _PROP_DRS_RECOMMENDATION)
        target_host = _pick_drs_target_host(recommendations, vm_moid)
        if target_host is not None:
            source = "drs"

    if target_host is None:
        return {
            "status": "no_recommendation",
            "target_host": None,
            "source": "none",
            "guidance": (
                "DRS produced no recommendation for the VM; pass "
                "``target_host`` explicitly to bypass the DRS lookup"
            ),
        }

    gate, _ = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_RELOCATE_VM,
        {"vm": vm_moid, "placement": {"host": target_host}},
    )
    if gate is not None:
        return gate
    return {
        "status": "migrated",
        "target_host": target_host,
        "source": source,
        "guidance": None,
    }


# ===========================================================================
# vm.power.bulk
# ===========================================================================


async def vm_power_bulk_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Apply a power action to every VM matching a filter; aggregate results.

    Op-id: ``vmware.composite.vm.power.bulk``. ``fail_fast=True``
    aborts on first failure; default tolerates per-VM failures. A governance
    verdict is identical across the fan-out (same op_id + target), so a
    gated/denied per-VM power short-circuits the whole composite with the
    seam's result rather than half-executing the batch.
    """
    filter_dict: dict[str, Any] = dict(params.get("filter") or {})
    action = params["action"]
    fail_fast = bool(params.get("fail_fast", False))
    power_op_id = _power_vm_op_id(action)

    vms = await _resolve_vm_list(
        connector=connector, target=target, operator=operator, filter_dict=filter_dict
    )

    results: list[dict[str, Any]] = []
    ok_count = 0
    err_count = 0
    aborted = False
    for vm_entry in vms:
        vm_moid = vm_entry.get("vm")
        if not isinstance(vm_moid, str):
            continue
        try:
            gate, _ = await _write_sub_op(connector, target, operator, power_op_id, {"vm": vm_moid})
        except httpx.HTTPError as exc:
            results.append({"vm": vm_moid, "status": "error", "error": str(exc)})
            err_count += 1
            if fail_fast:
                aborted = True
                break
            continue
        if gate is not None:
            return gate
        results.append({"vm": vm_moid, "status": "ok", "error": None})
        ok_count += 1
    return {
        "results": results,
        "summary": {"ok": ok_count, "error": err_count},
        "aborted_on_failure": aborted,
    }


# ===========================================================================
# vm.power (single VM)
# ===========================================================================


def _parse_vsphere_error(exc: httpx.HTTPError) -> tuple[int | None, str | None]:
    """Best-effort ``(http_status, vsphere_error_type)`` from a sub-op fault.

    vCenter error bodies carry a machine ``error_type`` (e.g.
    ``SERVICE_UNAVAILABLE`` when Tools is down) alongside the HTTP status.
    Only :class:`httpx.HTTPStatusError` carries a response; transport-level
    faults (connect/read) surface ``(None, None)`` and are reported as a
    generic error. Body parsing is defensive -- a non-JSON or
    unexpectedly-shaped body yields a ``None`` error_type, never a raise.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        return None, None
    status = exc.response.status_code
    try:
        body = exc.response.json()
    except (ValueError, httpx.HTTPError):
        return status, None
    error_type = body.get("error_type") if isinstance(body, dict) else None
    return status, error_type if isinstance(error_type, str) else None


def _vsphere_fault_detail(exc: httpx.HTTPError) -> str:
    """Human-readable ``HTTP <status> (<error_type>): <message>`` from a sub-op fault.

    The diagnosable form of an upstream fault (#3071): the HTTP status, the
    machine ``error_type`` (e.g. ``INVALID_ARGUMENT`` / ``NOT_FOUND``), and the
    first localized vAPI message (``messages[].default_message``) — so a
    composite can fold a 4xx/5xx into its structured failure arm instead of
    letting a bare :exc:`httpx.HTTPStatusError` escape as an opaque
    ``connector_error`` with no status, url, or vendor message (the regression
    #3071 fixes, sibling of #1649/#1804). A transport-level fault (connect /
    read, no response) degrades to ``transport fault (<ExcType>)`` — the
    exception *type* name is folded in because several transport faults
    (notably :exc:`httpx.ReadTimeout`) stringify to the empty string, which
    otherwise left the surfaced detail an empty ``transport fault:`` tail
    (#3076); a non-empty ``str(exc)`` is appended after it. The helper owns
    the whole rendered detail for both fault classes, so callers interpolate
    it directly without a redundant ``: {exc}`` suffix. Body parsing is
    defensive — a non-JSON or unexpectedly-shaped body drops the missing parts,
    never raises.
    """
    if not isinstance(exc, httpx.HTTPStatusError):
        base = f"transport fault ({type(exc).__name__})"
        text = str(exc)
        return f"{base}: {text}" if text else base
    detail = f"HTTP {exc.response.status_code}"
    try:
        body = exc.response.json()
    except (ValueError, httpx.HTTPError):
        body = None
    if isinstance(body, dict):
        error_type = body.get("error_type")
        if isinstance(error_type, str) and error_type:
            detail += f" ({error_type})"
        for message in body.get("messages") or []:
            if isinstance(message, dict):
                default = message.get("default_message")
                if isinstance(default, str) and default:
                    detail += f": {default}"
                    break
    return detail


def _guest_tools_unavailable(status: int | None, error_type: str | None) -> bool:
    """Whether a guest-power fault means VMware Tools is not running.

    The vAPI guest-power operation returns ``ServiceUnavailable`` -- HTTP
    503, ``error_type == "SERVICE_UNAVAILABLE"`` -- specifically when Tools
    is not running, which is the state the operator needs surfaced (a soft
    shutdown cannot proceed). Matching either signal keeps the classifier
    robust to a deployment that returns the typed body without the 503, or
    the 503 without a parseable body.
    """
    return status == 503 or error_type == "SERVICE_UNAVAILABLE"


async def vm_power_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Apply one power verb to a single VM; approval-gated, typed failures.

    Op-id: ``vmware.composite.vm.power``. Unlike the fan-out
    :func:`vm_power_bulk_composite`, this acts on one operator-named VM moid
    -- the ergonomics a one-VM incident action (a hung appliance) wants.
    Five verbs: ``on`` / ``off`` / ``reset`` hit the hard ``/power``
    endpoint; ``guest_shutdown`` / ``guest_reboot`` hit the Tools-mediated
    ``/guest/power`` endpoint for a clean in-guest transition.

    The single write is routed through the #2254 governance seam
    (:func:`_write_sub_op`); a parked/denied gate returns the
    :class:`OperationResult` verbatim and no power op fires. A soft verb
    against a VM whose Tools are down fails *typed* -- ``status =
    "tools_unavailable"`` with the Tools state echoed -- rather than hanging
    or surfacing an opaque transport error, so the operator learns why the
    clean shutdown could not run and can fall back to a hard ``off``.
    """
    vm_moid = params["vm"]
    verb = params["verb"]
    op_id = _SINGLE_POWER_VERB_OP_IDS[verb]
    is_guest_verb = verb in _GUEST_POWER_VERBS

    try:
        gate, _ = await _write_sub_op(connector, target, operator, op_id, {"vm": vm_moid})
    except httpx.HTTPError as exc:
        status, error_type = _parse_vsphere_error(exc)
        if is_guest_verb and _guest_tools_unavailable(status, error_type):
            return {
                "vm": vm_moid,
                "verb": verb,
                "status": "tools_unavailable",
                "error": str(exc),
                "error_type": error_type,
                "guest_tools": "unavailable",
            }
        return {
            "vm": vm_moid,
            "verb": verb,
            "status": "error",
            "error": str(exc),
            "error_type": error_type,
            "guest_tools": "unavailable" if is_guest_verb else None,
        }
    if gate is not None:
        return gate
    return {
        "vm": vm_moid,
        "verb": verb,
        "status": "ok",
        "error": None,
        "error_type": None,
        "guest_tools": "ok" if is_guest_verb else None,
    }


# ===========================================================================
# host.evacuate (recursive composite)
# ===========================================================================


async def _host_maintenance_vim_step(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    host_moid: str,
    op_id: str,
) -> tuple[OperationResult | None, str | None]:
    """Run one governed vim maintenance transition on a host, polled to terminal.

    The pinned vcenter.yaml serves no host-maintenance REST path (#2970);
    enter / exit are the vim ``HostSystem.EnterMaintenanceMode_Task`` /
    ``ExitMaintenanceMode_Task`` methods, whose request types carry a
    required int ``timeout`` (``<= 0`` = no vim-side bound -- the poll's
    wall clock is the bound here). Returns ``(gate, None)`` when the
    governance seam parks/denies the write, ``(None, reason)`` when the
    returned Task faults or the poll deadline elapses, ``(None, None)`` on
    success. Shared by :func:`host_evacuate_composite` (enter) and
    :func:`cluster_patch_composite` (enter + exit).
    """
    method_name = op_id.rsplit("/", 1)[-1]
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=op_id,
        vmomi_path=f"/{_HOST_SYSTEM_MO_TYPE}/{host_moid}/{method_name}",
        body={"timeout": _MAINTENANCE_VIM_TIMEOUT},
        params={"host": host_moid},
    )
    if gate is not None:
        return gate, None
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_MAINTENANCE_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        return None, (
            f"{method_name} on host {host_moid!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return None, (
            f"{method_name} task {outcome.task} on host {host_moid!r} did not reach a "
            f"terminal state within {int(_MAINTENANCE_TASK_TIMEOUT_SECONDS)}s"
        )
    return None, None


def _classify_vm_migrate_outcome(
    migrate_result: OperationResult,
) -> tuple[bool, str]:
    """Return ``(succeeded, error_text)`` for a recursive vm.migrate result."""
    if migrate_result.status != "ok":
        return False, migrate_result.error or "unknown"
    inner = migrate_result.result
    if isinstance(inner, dict) and inner.get("status") == "migrated":
        return True, ""
    inner_status = inner.get("status") if isinstance(inner, dict) else None
    return False, str(inner_status or "unknown")


async def host_evacuate_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
    dispatch_child: DispatchChild,
) -> dict[str, Any] | OperationResult:
    """Migrate every VM off a host (via recursive vm.migrate) then enter maintenance.

    Op-id: ``vmware.composite.host.evacuate``. First production composite
    that calls another composite via ``dispatch_child`` -- that recursion
    into ``vmware.composite.vm.migrate`` stays on the ``dispatch_child`` path
    (a registrar-guaranteed ``source_kind="composite"`` row, #2248), while
    the host's own VM listing read and the maintenance-enter write run
    directly on the injected ``connector`` session.
    """
    host_moid = params["host"]
    tolerate_partial = bool(params.get("tolerate_partial_failure", False))

    vms = await _resolve_vm_list(
        connector=connector, target=target, operator=operator, filter_dict={"hosts": [host_moid]}
    )

    migrated: list[str] = []
    failed: list[dict[str, str]] = []
    for vm_entry in vms:
        vm_moid = vm_entry.get("vm")
        if not isinstance(vm_moid, str):
            continue
        # Resolve the cluster per-VM rather than once from ``vms[0]``. The
        # vCenter listing row reports each VM's containing cluster; VMs on
        # the same host can belong to different clusters when the host
        # straddles a federation (and the listing payload may simply omit
        # the field for a row mid-flight). Treat a missing cluster as a
        # per-VM failure so the recursive migrate doesn't fire against an
        # empty target — that would short-circuit to ``no_recommendation``
        # without surfacing the underlying data gap.
        vm_cluster = vm_entry.get("cluster")
        if not isinstance(vm_cluster, str) or not vm_cluster:
            failed.append({"vm": vm_moid, "error": "missing_cluster"})
            if not tolerate_partial:
                return {
                    "status": "aborted",
                    "host": host_moid,
                    "migrated_vms": migrated,
                    "failed_vms": failed,
                    "maintenance_entered": False,
                }
            continue
        migrate_result = await dispatch_child(
            connector_id=_CONNECTOR_ID,
            op_id=_OP_COMPOSITE_VM_MIGRATE,
            params={"vm": vm_moid, "cluster": vm_cluster},
        )
        succeeded, err_text = _classify_vm_migrate_outcome(migrate_result)
        if succeeded:
            migrated.append(vm_moid)
            continue
        failed.append({"vm": vm_moid, "error": err_text})
        if not tolerate_partial:
            return {
                "status": "aborted",
                "host": host_moid,
                "migrated_vms": migrated,
                "failed_vms": failed,
                "maintenance_entered": False,
            }

    gate, reason = await _host_maintenance_vim_step(
        connector=connector,
        target=target,
        operator=operator,
        host_moid=host_moid,
        op_id=_OP_ENTER_MAINTENANCE_TASK,
    )
    if gate is not None:
        return gate
    if reason is not None:
        # The maintenance-enter is the load-bearing final step; a task
        # fault / poll timeout raises so the dispatcher wraps it
        # ``connector_error`` (mirrors the pre-#2970 propagate-on-HTTPError
        # semantics of the synchronous REST fiction).
        raise RuntimeError(f"host.evacuate: {reason}")
    return {
        "status": "partial" if failed else "evacuated",
        "host": host_moid,
        "migrated_vms": migrated,
        "failed_vms": failed,
        "maintenance_entered": True,
    }


# ===========================================================================
# host.detach_from_vds
# ===========================================================================


async def _repoint_vm_nics_to_fallback(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_moid: str,
    fallback_network: str,
) -> tuple[OperationResult | None, str | None]:
    """Repoint every NIC of one VM to *fallback_network* (standard portgroup).

    The pinned spec serves no VM-level ``PATCH:/vcenter/vm/{vm}/network``
    (#2970); NIC backing changes are per-adapter --
    ``Vcenter.Vm.Hardware.Ethernet_list`` enumerates the adapters, then
    each is updated via ``PATCH:/vcenter/vm/{vm}/hardware/ethernet/{nic}``
    with a ``STANDARD_PORTGROUP`` backing spec. Returns ``(gate, None)``
    on a parked/denied write, ``(None, error)`` on a transport fault
    (folded into the per-VM failure row), ``(None, None)`` on success.
    """
    listing = await _read_sub_op(connector, target, operator, _OP_LIST_VM_NICS, {"vm": vm_moid})
    nic_rows = _unwrap_value(listing)
    if not isinstance(nic_rows, list):
        return None, f"expected list from {_OP_LIST_VM_NICS!r}, got {type(nic_rows).__name__}"
    for row in nic_rows:
        nic_id = row.get("nic") if isinstance(row, dict) else None
        if not isinstance(nic_id, str):
            continue
        gate, _ = await _write_sub_op(
            connector,
            target,
            operator,
            _OP_UPDATE_VM_NIC,
            {
                "vm": vm_moid,
                "nic": nic_id,
                "backing": {
                    "type": _NIC_BACKING_STANDARD_PORTGROUP,
                    "network": fallback_network,
                },
            },
        )
        if gate is not None:
            return gate, None
    return None, None


async def host_detach_from_vds_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Migrate host VM NICs off a DVS to a standard switch, then remove host from DVS.

    Op-id: ``vmware.composite.host.detach_from_vds``. Refuses the DVS
    detach when any NIC migration failed -- vSphere would reject the
    detach anyway. The detach itself is the vim
    ``DistributedVirtualSwitch.ReconfigureDvs_Task`` (host member spec
    with ``operation="remove"``): the pinned vcenter.yaml serves no DVS
    write path at all (#2970). The spec's required ``configVersion`` is
    read off ``config.configVersion`` first; the returned Task is polled
    to a terminal state (fault raises; poll timeout returns
    ``status='timeout'`` with the task id).
    """
    host_moid = params["host"]
    dvs_moid = params["dvs"]
    fallback_network = params["fallback_network"]

    await _read_sub_op(
        connector,
        target,
        operator,
        _OP_LIST_NETWORK,
        {"filter.types": [_NETWORK_TYPE_DISTRIBUTED_PORTGROUP], "filter.hosts": [host_moid]},
    )
    vms = await _resolve_vm_list(
        connector=connector, target=target, operator=operator, filter_dict={"hosts": [host_moid]}
    )

    vms_migrated: list[str] = []
    migration_failures: list[dict[str, str]] = []
    for vm_entry in vms:
        vm_moid = vm_entry.get("vm")
        if not isinstance(vm_moid, str):
            continue
        try:
            gate, failure = await _repoint_vm_nics_to_fallback(
                connector=connector,
                target=target,
                operator=operator,
                vm_moid=vm_moid,
                fallback_network=fallback_network,
            )
        except httpx.HTTPError as exc:
            migration_failures.append({"vm": vm_moid, "error": str(exc)})
            continue
        if gate is not None:
            return gate
        if failure is not None:
            migration_failures.append({"vm": vm_moid, "error": failure})
            continue
        vms_migrated.append(vm_moid)

    if migration_failures:
        return {
            "status": "incomplete",
            "host": host_moid,
            "vm_migration_failures": migration_failures,
            "vms_migrated": vms_migrated,
        }

    # ``DVSConfigSpec.configVersion`` must echo the switch's current
    # ``DVSConfigInfo.configVersion`` for the reconfigure to be accepted.
    config_result = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=_build_single_prop_retrieve_params(_DVS_MO_TYPE, dvs_moid, _PROP_DVS_CONFIG_VERSION),
    )
    config_version = _extract_single_prop(config_result, _PROP_DVS_CONFIG_VERSION)
    if not isinstance(config_version, str):
        raise RuntimeError(
            f"host.detach_from_vds: could not read config.configVersion off dvs "
            f"{dvs_moid!r} (payload={config_result!r})"
        )

    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIGURE_DVS_TASK,
        vmomi_path=f"/{_DVS_MO_TYPE}/{dvs_moid}/ReconfigureDvs_Task",
        body={
            "spec": {
                _VMOMI_TYPE_NAME_KEY: _DVS_CONFIG_SPEC_TYPE,
                "configVersion": config_version,
                "host": [
                    {
                        _VMOMI_TYPE_NAME_KEY: _DVS_HOST_MEMBER_CONFIG_SPEC_TYPE,
                        "operation": "remove",
                        "host": _moref(_HOST_SYSTEM_MO_TYPE, host_moid),
                    }
                ],
            }
        },
        params={"dvs": dvs_moid, "host": host_moid},
    )
    if gate is not None:
        return gate
    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_DVS_RECONFIGURE_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == TASK_STATE_ERROR:
        raise RuntimeError(
            f"host.detach_from_vds: ReconfigureDvs_Task on dvs {dvs_moid!r} (remove host "
            f"{host_moid!r}) faulted: {outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return {
            "status": "timeout",
            "host": host_moid,
            "vm_migration_failures": [],
            "vms_migrated": vms_migrated,
            "guidance": (
                f"ReconfigureDvs_Task {outcome.task} did not reach a terminal state within "
                f"{int(_DVS_RECONFIGURE_TASK_TIMEOUT_SECONDS)}s; poll the task -- the "
                "detach may still complete in the background"
            ),
        }
    return {
        "status": "detached",
        "host": host_moid,
        "vm_migration_failures": [],
        "vms_migrated": vms_migrated,
    }


# ===========================================================================
# cluster.patch
# ===========================================================================


async def _apply_host_software(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    host_moid: str,
) -> tuple[OperationResult | None, str | None]:
    """Fire the per-host vLCM apply and poll its cis task to a terminal state.

    ``POST:/esx/settings/hosts/{host}/software?action=apply&vmw-task=true``
    (the pinned spec's only per-host remediation path -- #2970; the
    fictional ``POST:/vcenter/host/{host}?action=patch`` never existed).
    An empty ``Esx.Settings.Hosts.Software.ApplySpec`` (every field
    optional) remediates to the latest desired-state commit. The 202 body
    is the cis task id, polled via ``GET:/cis/tasks/{task}`` before the
    host leaves maintenance -- exiting mid-remediation would defeat the
    sequential rolling-patch contract.
    """
    gate, apply_payload = await _write_sub_op(
        connector, target, operator, _OP_HOST_SOFTWARE_APPLY, {"host": host_moid, "spec": {}}
    )
    if gate is not None:
        return gate, None
    task_id = _extract_cis_task_id(apply_payload)
    if task_id is None:
        return None, (
            f"software apply on {host_moid!r} returned no cis task id (payload={apply_payload!r})"
        )
    reason = await _poll_cis_task(
        connector=connector,
        target=target,
        operator=operator,
        task_id=task_id,
        timeout_seconds=_HOST_APPLY_TASK_TIMEOUT_SECONDS,
    )
    if reason is not None:
        return None, f"software apply on {host_moid!r} failed: {reason}"
    return None, None


async def _patch_one_host(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    host_moid: str,
) -> tuple[OperationResult | None, str | None]:
    """Sequential maintenance-enter + vLCM apply + maintenance-exit on one host.

    Maintenance transitions are vim ``*_Task`` methods polled to terminal
    (:func:`_host_maintenance_vim_step`); the patch step is the cis-task-
    polled vLCM apply (:func:`_apply_host_software`). Returns
    ``(gate, None)`` when a step's governance seam parks/denies the write
    (the caller returns the :class:`OperationResult` verbatim),
    ``(None, error_reason)`` when a step fails (transport fault, task
    fault, or poll timeout), or ``(None, None)`` on full success.
    """
    steps: tuple[tuple[str, Any], ...] = (
        ("maintenance_enter", _OP_ENTER_MAINTENANCE_TASK),
        ("patch", None),
        ("maintenance_exit", _OP_EXIT_MAINTENANCE_TASK),
    )
    for step, maintenance_op_id in steps:
        try:
            if maintenance_op_id is not None:
                gate, reason = await _host_maintenance_vim_step(
                    connector=connector,
                    target=target,
                    operator=operator,
                    host_moid=host_moid,
                    op_id=maintenance_op_id,
                )
            else:
                gate, reason = await _apply_host_software(
                    connector=connector,
                    target=target,
                    operator=operator,
                    host_moid=host_moid,
                )
        except httpx.HTTPError as exc:
            return None, f"{step} on {host_moid!r} failed: {exc}"
        if gate is not None:
            return gate, None
        if reason is not None:
            return None, f"{step} on {host_moid!r} failed: {reason}"
    return None, None


async def cluster_patch_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Sequentially patch every host in a cluster: maintenance + patch + exit.

    Op-id: ``vmware.composite.cluster.patch``. Sequential by design --
    concurrent host patches would force every cluster VM to vMotion
    at once.
    """
    cluster_moid = params["cluster"]

    entries = await _resolve_cluster_hosts(
        connector=connector, target=target, operator=operator, cluster_moid=cluster_moid
    )
    host_moids: list[str] = []
    for entry in entries:
        host_moid = entry.get("host")
        if isinstance(host_moid, str):
            host_moids.append(host_moid)

    patched: list[str] = []
    for i, host_moid in enumerate(host_moids):
        gate, failure_reason = await _patch_one_host(
            connector=connector,
            target=target,
            operator=operator,
            host_moid=host_moid,
        )
        if gate is not None:
            return gate
        if failure_reason is not None:
            return {
                "status": "stopped",
                "cluster": cluster_moid,
                "patched_hosts": patched,
                "failed_host": host_moid,
                "remaining_hosts": host_moids[i + 1 :],
                "failure_reason": failure_reason,
            }
        patched.append(host_moid)

    return {
        "status": "completed",
        "cluster": cluster_moid,
        "patched_hosts": patched,
        "failed_host": None,
        "remaining_hosts": [],
        "failure_reason": None,
    }


# ===========================================================================
# vm.disk.grow (VI-JSON write substrate — keystone 2, #2893)
# ===========================================================================
#
# The first *mutating* VI-JSON composite. Growing a virtual disk has no
# REST path — the pinned 9.0 spec's ``Disk.UpdateSpec`` carries only
# ``backing`` (no capacity field, spec-verified), so ``ReconfigVM_Task``
# is the sole route. The write rides the same governed direct-session seam
# the REST writes do (:func:`_write_vmomi_sub_op` → the #2254 gate), and
# the returned ``*_Task`` MoRef is driven to a terminal state via the
# shared :func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task`
# helper before success is reported. Tasks D (#2894 clone) and E (#2895
# DRS-rule) ride the same two seams.


def _coerce_int(value: Any) -> int | None:
    """Coerce a JSON number / numeric-string to int; reject bools + junk.

    ``VirtualDisk.capacityInBytes`` is ``xsd:long``; some intermediaries
    render a 64-bit value as a JSON string. ``True`` is an ``int`` subclass
    and must not read as ``1``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _build_vm_devices_retrieve_params(vm_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` body reading a VM's device list.

    A single ``PropertyFilterSpec`` scoped directly to the VirtualMachine
    object requesting ``config.hardware.device`` -- the list of
    ``VirtualDevice`` objects (disks, controllers, NICs, …), each carrying
    its VI-JSON ``_typeName`` discriminator so the disk-grow handler can
    pick the ``VirtualDisk`` out. The singleton ``propertyCollector`` moId
    rides the path, so the body is only the method args (the shape the
    typed reads send), ``_typeName``-annotated via the shared trio helper
    (#3103).
    """
    return retrieve_properties_body(
        _VIRTUAL_MACHINE_MO_TYPE, [vm_moid], [_PROP_CONFIG_HARDWARE_DEVICE]
    )


def _extract_vm_devices(retrieve_result: Any) -> list[Any]:
    """Pull the ``config.hardware.device`` list off a RetrievePropertiesEx result.

    Live VI-JSON boxes the array-valued ``val`` as ``ArrayOfVirtualDevice``
    (``{"_typeName": ..., "_value": [...]}``) -- unwrap before the list
    check, tolerating the bare-list shape too (#3106).
    """
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    if not isinstance(objects, list):
        return []
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_CONFIG_HARDWARE_DEVICE:
                val = unwrap_vim_value(prop.get("val"))
                return val if isinstance(val, list) else []
    return []


def _find_virtual_disk(devices: list[Any], disk_id: str) -> dict[str, Any] | None:
    """Return the ``VirtualDisk`` device whose key matches *disk_id*, else ``None``.

    The REST disk id (resource type ``com.vmware.vcenter.vm.hardware.Disk``)
    is the string form of the vim ``VirtualDevice.key``, so the disk-grow
    ``disk`` param selects the device by ``str(key)`` match. The
    ``_typeName == "VirtualDisk"`` guard skips the controllers / NICs /
    CD-ROMs sharing the same ``config.hardware.device`` list.
    """
    for dev in devices:
        if not isinstance(dev, dict):
            continue
        if dev.get(_VMOMI_TYPE_NAME_KEY) != _VIRTUAL_DISK_TYPE:
            continue
        if str(dev.get("key")) == disk_id:
            return dev
    return None


async def _resolve_disk_info(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
    disk: str,
) -> dict[str, Any]:
    """Resolve the REST ``Disk.Info`` (label + current capacity bytes) for a disk.

    Read-only single ``GET:/vcenter/vm/{vm}/hardware/disk/{disk}`` on the
    connector session, shared by the disk-grow park-time preview
    (:mod:`._write_preview`). ``Vcenter.Vm.Hardware.Disk.Info`` carries
    ``label`` + ``capacity`` (bytes) -- the current-capacity half of the
    from→to delta the approver decides on. Raises :exc:`httpx.HTTPError` on
    a transport fault; the preview builder lets it propagate into the #1628
    ``preview_unavailable`` marker (the delta is unknowable, so the park is
    honest about it).
    """
    info = await _read_sub_op(
        connector, target, operator, _OP_GET_VM_DISK, {"vm": vm, "disk": disk}
    )
    payload = _unwrap_value(info)
    return payload if isinstance(payload, dict) else {}


async def _resolve_vm_name(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    vm: str,
) -> str | None:
    """Best-effort VM display name via ``GET:/vcenter/vm/{vm}``.

    Cosmetic for the disk-grow preview -- a transport fault nulls the name
    rather than sinking the preview, since the current→requested delta (not
    the name) is the decision. The disk read is the load-bearing one.
    """
    try:
        info = await _read_sub_op(connector, target, operator, _OP_GET_VM, {"vm": vm})
    except httpx.HTTPError:
        return None
    payload = _unwrap_value(info)
    name = payload.get("name") if isinstance(payload, dict) else None
    return name if isinstance(name, str) else None


async def vm_disk_grow_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Grow a VM's virtual disk to ``capacity_bytes`` via ``ReconfigVM_Task``.

    Op-id: ``vmware.composite.vm.disk.grow``. The proving op for the
    mutating VI-JSON substrate (#2893): grow has no REST path
    (``Disk.UpdateSpec`` has only ``backing``), so the capacity change goes
    through vim ``VirtualMachine.ReconfigVM_Task`` — a single-device
    ``VirtualDeviceConfigSpec`` with ``operation="edit"`` and the target
    ``VirtualDisk``'s ``capacityInBytes`` raised (spec-verified against
    ``vi-json.yaml``).

    Flow:

    1. Read the VM's ``config.hardware.device`` (vmomi ``RetrievePropertiesEx``,
       a *read* — no governance gate) to obtain the full ``VirtualDisk``
       device (the spec requires the edited device "fully specified") + its
       current ``capacityInBytes``.
    2. **Grow-only by contract**: a request ``<=`` the current capacity is
       refused with ``status="invalid_shrink"`` *before* any write —
       vSphere rejects a shrink anyway; fail early and legibly.
    3. Issue the single ``ReconfigVM_Task`` edit through the governed vmomi
       write seam (:func:`_write_vmomi_sub_op` → the #2254 gate); a
       parked/denied gate returns the :class:`OperationResult` verbatim and
       no reconfigure fires.
    4. Poll the returned ``*_Task`` MoRef to a terminal state via the shared
       :func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task`
       helper before reporting success. A task fault raises (the dispatcher
       wraps it ``connector_error``, mirroring ``vm.clone``); a poll timeout
       returns ``status="timeout"`` with the task id for the operator.
    """
    vm_moid = params["vm"]
    disk_id = params["disk"]
    requested = int(params["capacity_bytes"])

    devices_result = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=_build_vm_devices_retrieve_params(vm_moid),
    )
    device = _find_virtual_disk(_extract_vm_devices(devices_result), disk_id)
    current = _coerce_int(device.get("capacityInBytes")) if device is not None else None
    if device is None or current is None:
        return {
            "status": "disk_not_found",
            "vm": vm_moid,
            "disk": disk_id,
            "task": None,
            "from_capacity_bytes": current,
            "to_capacity_bytes": requested,
            "delta_bytes": None,
            "guidance": (
                f"no VirtualDisk with key {disk_id!r} (or no readable capacity) on "
                f"vm {vm_moid!r}; list the VM's disks to confirm the disk id"
            ),
        }

    delta = requested - current
    if requested <= current:
        return {
            "status": "invalid_shrink",
            "vm": vm_moid,
            "disk": disk_id,
            "task": None,
            "from_capacity_bytes": current,
            "to_capacity_bytes": requested,
            "delta_bytes": delta,
            "guidance": (
                "vm.disk.grow is grow-only: requested capacity "
                f"{requested} <= current {current} (delta {delta}). vSphere rejects "
                "a disk shrink; re-issue with a capacity greater than the current size"
            ),
        }

    # The edited device is the server-read ``VirtualDisk`` echoed back
    # ("fully specified" per the spec), so it already carries its own
    # ``_typeName`` from the response; the enclosing ConfigSpec and
    # ``VirtualDeviceConfigSpec`` entry are tagged here (#3103).
    reconfig_spec = {
        "spec": {
            _VMOMI_TYPE_NAME_KEY: _VM_CONFIG_SPEC_TYPE,
            "deviceChange": [
                {
                    _VMOMI_TYPE_NAME_KEY: _VIRTUAL_DEVICE_CONFIG_SPEC_TYPE,
                    "operation": "edit",
                    "device": {**device, "capacityInBytes": requested},
                }
            ],
        }
    }
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIG_VM_TASK,
        vmomi_path=f"/VirtualMachine/{vm_moid}/ReconfigVM_Task",
        body=reconfig_spec,
        params={"vm": vm_moid, "disk": disk_id, "capacity_bytes": requested},
    )
    if gate is not None:
        return gate

    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_DISK_GROW_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == "error":
        raise RuntimeError(
            f"vm.disk.grow: ReconfigVM_Task on vm {vm_moid!r} disk {disk_id!r} faulted: "
            f"{outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return {
            "status": "timeout",
            "vm": vm_moid,
            "disk": disk_id,
            "task": outcome.task,
            "from_capacity_bytes": current,
            "to_capacity_bytes": requested,
            "delta_bytes": delta,
            "guidance": (
                f"ReconfigVM_Task {outcome.task} did not reach a terminal state within "
                f"{int(_DISK_GROW_TASK_TIMEOUT_SECONDS)}s; poll the task or re-read the disk "
                "capacity — the grow may still complete in the background"
            ),
        }
    return {
        "status": "grown",
        "vm": vm_moid,
        "disk": disk_id,
        "task": outcome.task,
        "from_capacity_bytes": current,
        "to_capacity_bytes": requested,
        "delta_bytes": delta,
        "guidance": None,
    }


# ===========================================================================
# vm.clone_from_template (folder-template CloneVM_Task — Task D, #2894)
# ===========================================================================
#
# Clones a *folder VM template* (a marked-as-template VM in a VM folder) via
# vim ``VirtualMachine.CloneVM_Task``. The existing ``vm.clone`` composite is
# content-library-only (``POST:/vcenter/vm-template/library-items?action=deploy``)
# and a folder template has no clone path on the REST surface at all;
# ``CloneVM_Task`` is the canonical folder-template deploy API (what
# govc/terraform use) and uniquely supports inline guest customization at
# clone time. Rides the #2893 substrate: the mutating CloneVM_Task flows
# through the governed vmomi write seam (:func:`_write_vmomi_sub_op` → the
# #2254 gate) and the returned ``*_Task`` MoRef is driven to a terminal state
# via the shared :func:`~...vim_task.poll_vim_task` helper.


def _moref(mo_type: str, moid: str) -> dict[str, str]:
    """An annotated vim ``ManagedObjectReference`` (shared helper, #3103)."""
    return vim_moref(mo_type, moid)


def _build_config_template_retrieve_params(vm_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` body reading a VM's ``config.template``.

    A single ``PropertyFilterSpec`` scoped to the VirtualMachine requesting
    ``config.template`` -- the boolean that distinguishes a marked-as-template
    VM from a regular one. The singleton ``propertyCollector`` moId rides the
    path, so the body is only the method args (the shape the typed reads +
    the disk-grow config read send), ``_typeName``-annotated via the shared
    trio helper (#3103).
    """
    return retrieve_properties_body(_VIRTUAL_MACHINE_MO_TYPE, [vm_moid], [_PROP_CONFIG_TEMPLATE])


def _extract_config_template(retrieve_result: Any, vm_moid: str) -> bool | None:
    """Pull the ``config.template`` bool off a RetrievePropertiesEx result.

    Returns the boolean when present, else ``None`` (the caller treats an
    absent / non-bool value as "cannot confirm a template" and refuses the
    clone). Matches the object by moid when the ``obj`` MoRef is present.
    """
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    if not isinstance(objects, list):
        return None
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        obj_moid = obj.get("obj", {}).get("value") if isinstance(obj.get("obj"), dict) else None
        if obj_moid is not None and obj_moid != vm_moid:
            continue
        for prop in obj.get("propSet", []) or []:
            if isinstance(prop, dict) and prop.get("name") == _PROP_CONFIG_TEMPLATE:
                val = unwrap_vim_value(prop.get("val"))
                return val if isinstance(val, bool) else None
    return None


def _extract_task_vm_moid(task_result: Any) -> str | None:
    """Pull the new VM moid out of a SUCCEEDED ``*_Task`` ``result`` MoRef.

    ``TaskInfo.result`` for ``CloneVM_Task`` (#2894) and ``CreateVM_Task``
    (#3099) is the new VirtualMachine ``ManagedObjectReference``
    (``{"type": "VirtualMachine", "value": "vm-99"}``); a bare moid string
    is tolerated. ``None`` when the shape is neither -- the task still
    succeeded, the moid is just unreadable.
    """
    if isinstance(task_result, dict):
        value = task_result.get("value")
        return value if isinstance(value, str) else None
    return task_result if isinstance(task_result, str) else None


async def _resolve_template_moid(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    name: str,
) -> tuple[str | None, str | None, list[str]]:
    """Resolve a source-template display name to a unique VM moid.

    Reads ``GET:/vcenter/vm?filter.names=<name>`` (a read sub-op, un-gated)
    and returns ``(moid, error_status, candidates)``:

    * ``(moid, None, [])`` -- exactly one match.
    * ``(None, "template_not_found", [])`` -- no match.
    * ``(None, "ambiguous_template", [moids])`` -- more than one match; the
      operator re-issues against an unambiguous moid.

    Name resolution (not a moid param) is the flow the #2894 body prescribes,
    mirroring the ``vmware.vm.info`` typed read; the ``config.template``
    assert that follows is what proves the resolved VM is actually a template.
    """
    listing = await _read_sub_op(
        connector, target, operator, _OP_LIST_VMS, {"filter.names": [name]}
    )
    rows = _unwrap_value(listing)
    if not isinstance(rows, list):
        return None, "template_not_found", []
    moids = [row["vm"] for row in rows if isinstance(row, dict) and isinstance(row.get("vm"), str)]
    if not moids:
        return None, "template_not_found", []
    if len(moids) > 1:
        return None, "ambiguous_template", moids
    return moids[0], None, []


async def _resolve_customization_spec(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    spec_name: str,
    manager_moid: str,
) -> dict[str, Any]:
    """Resolve a stored GOSC spec by name to its inline vim ``CustomizationSpec``.

    vim ``CloneSpec.customization`` takes a full ``CustomizationSpec`` inline,
    not a by-name reference, so a stored spec (e.g. one created by #2892's
    ``guest.customization_spec.create``) is resolved to its object form via
    ``CustomizationSpecManager.GetCustomizationSpec`` (a *read* -- un-gated,
    routed straight through ``_post_vmomi_json``) and its ``.spec`` field is
    embedded in the clone. Composing here means the clone yields a customized
    VM without a separate ``vm.customize`` dispatch. Raises when the spec name
    does not resolve to a usable ``CustomizationSpecItem`` -- the operator
    named a customization that vCenter cannot supply, so the clone must not
    proceed with a silently-uncustomized VM.
    """
    item = await connector._post_vmomi_json(
        target,
        f"/CustomizationSpecManager/{manager_moid}/GetCustomizationSpec",
        operator=operator,
        json={"name": spec_name},
    )
    payload = _unwrap_value(item)
    spec = payload.get("spec") if isinstance(payload, dict) else None
    if not isinstance(spec, dict):
        raise RuntimeError(
            f"vm.clone_from_template: customization spec {spec_name!r} did not resolve to a "
            f"CustomizationSpecItem with a spec (got {type(spec).__name__}); confirm the GOSC "
            "spec name exists on this vCenter"
        )
    return spec


def _build_clone_body(
    *,
    new_vm_name: str,
    folder_moid: str,
    pool_moid: str,
    datastore_moid: str,
    host_moid: str | None,
    power_on: bool,
    customization: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the ``CloneVM_Task`` request body from the placement params.

    Shape (spec-verified against ``vi-json.yaml``): ``CloneVMRequestType`` =
    ``{folder: Folder-MoRef, name, spec: VirtualMachineCloneSpec}`` where the
    ``CloneSpec`` carries a ``VirtualMachineRelocateSpec`` ``location``
    (``pool`` + ``datastore`` MoRefs, optional ``host`` pin), ``template:
    false`` (clone to a VM, never a template) and ``powerOn``. ``customization``
    (an inline ``CustomizationSpec``) is added only when a GOSC spec was
    requested -- it is the server-resolved ``GetCustomizationSpec`` object
    echoed back, so it round-trips its own ``_typeName`` tags; the
    hand-assembled DataObjects here carry theirs explicitly (#3103).
    """
    location: dict[str, Any] = {
        _VMOMI_TYPE_NAME_KEY: _VM_RELOCATE_SPEC_TYPE,
        "pool": _moref(_RESOURCE_POOL_MO_TYPE, pool_moid),
        "datastore": _moref(_DATASTORE_MO_TYPE, datastore_moid),
    }
    if host_moid is not None:
        location["host"] = _moref(_HOST_SYSTEM_MO_TYPE, host_moid)
    clone_spec: dict[str, Any] = {
        _VMOMI_TYPE_NAME_KEY: _VM_CLONE_SPEC_TYPE,
        "location": location,
        "template": False,
        "powerOn": power_on,
    }
    if customization is not None:
        clone_spec["customization"] = customization
    return {"folder": _moref(_FOLDER_MO_TYPE, folder_moid), "name": new_vm_name, "spec": clone_spec}


def _clone_result(
    status: str,
    *,
    source_template: str,
    new_vm_name: str,
    folder: str,
    source_template_id: str | None = None,
    new_vm_id: str | None = None,
    task: str | None = None,
    customization_spec_name: str | None = None,
    candidates: list[str] | None = None,
    guidance: str | None = None,
) -> dict[str, Any]:
    """Build the uniform ``vm.clone_from_template`` response envelope."""
    return {
        "status": status,
        "source_template": source_template,
        "source_template_id": source_template_id,
        "new_vm_name": new_vm_name,
        "new_vm_id": new_vm_id,
        "folder": folder,
        "task": task,
        "customization_spec_name": customization_spec_name,
        "candidates": candidates,
        "guidance": guidance,
    }


async def vm_clone_from_template_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Clone a folder VM template via ``CloneVM_Task``, with optional GOSC.

    Op-id: ``vmware.composite.vm.clone_from_template``. The folder-template
    counterpart to ``vm.clone`` (content-library only). Flow:

    1. Resolve ``source_template`` (a display name) to a unique VM moid via
       ``GET:/vcenter/vm?filter.names=`` -- refuse on no / multiple matches.
    2. Assert the resolved VM is a template (``config.template`` via a vmomi
       ``RetrievePropertiesEx`` *read*) -- refuse a non-template source with
       ``status='not_a_template'`` before any clone.
    3. When ``customization_spec_name`` is set, resolve the stored GOSC spec
       to its inline ``CustomizationSpec`` (``CustomizationSpecManager.
       GetCustomizationSpec``) so the clone customizes in one dispatch.
    4. Issue ``CloneVM_Task`` through the governed vmomi write seam
       (:func:`_write_vmomi_sub_op` → the #2254 gate); a parked/denied gate
       returns the :class:`OperationResult` verbatim and no clone fires.
    5. Poll the returned Task to terminal via
       :func:`~...vim_task.poll_vim_task`. A fault raises (dispatcher wraps
       ``connector_error``, mirroring ``vm.disk.grow``); a timeout returns
       ``status='timeout'`` with the task id; success returns
       ``status='cloned'`` with the new VM moid from ``TaskInfo.result``.
    """
    source_template = params["source_template"]
    new_vm_name = params["new_vm_name"]
    folder_moid = params["folder"]
    pool_moid = params["resource_pool"]
    datastore_moid = params["datastore"]
    host_moid = params.get("host")
    power_on = bool(params.get("power_on", False))
    customization_spec_name = params.get("customization_spec_name")
    manager_moid = params.get(
        "customization_spec_manager_moid", _DEFAULT_CUSTOMIZATION_SPEC_MANAGER_MOID
    )

    template_moid, resolve_error, candidates = await _resolve_template_moid(
        connector, target, operator, name=source_template
    )
    if resolve_error is not None:
        return _clone_result(
            resolve_error,
            source_template=source_template,
            new_vm_name=new_vm_name,
            folder=folder_moid,
            candidates=candidates or None,
            guidance=(
                f"source_template {source_template!r} matched "
                f"{'no VM' if resolve_error == 'template_not_found' else 'more than one VM'}; "
                "re-issue against an unambiguous marked-as-template VM"
            ),
        )
    # ``resolve_error is None`` implies a unique moid resolved (the two are
    # correlated in ``_resolve_template_moid``'s return contract).
    assert template_moid is not None

    template_check = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=_build_config_template_retrieve_params(template_moid),
    )
    if _extract_config_template(template_check, template_moid) is not True:
        return _clone_result(
            "not_a_template",
            source_template=source_template,
            source_template_id=template_moid,
            new_vm_name=new_vm_name,
            folder=folder_moid,
            guidance=(
                f"{source_template!r} ({template_moid}) is not a marked-as-template VM "
                "(config.template is not true); mark it as a template or clone it with "
                "vmware.composite.vm.clone (content-library) instead"
            ),
        )

    customization = None
    if customization_spec_name is not None:
        customization = await _resolve_customization_spec(
            connector,
            target,
            operator,
            spec_name=customization_spec_name,
            manager_moid=manager_moid,
        )

    clone_body = _build_clone_body(
        new_vm_name=new_vm_name,
        folder_moid=folder_moid,
        pool_moid=pool_moid,
        datastore_moid=datastore_moid,
        host_moid=host_moid,
        power_on=power_on,
        customization=customization,
    )
    # Identity-only gate params: they name the blast radius (source, target,
    # placement, spec *name*) for the durable ApprovalRequest without ever
    # serialising the resolved CustomizationSpec's secret-bearing fields
    # (GOSC secret hygiene, #1503).
    gate_params = {
        "source_template": source_template,
        "source_template_id": template_moid,
        "new_vm_name": new_vm_name,
        "folder": folder_moid,
        "resource_pool": pool_moid,
        "datastore": datastore_moid,
        "host": host_moid,
        "power_on": power_on,
        "customization_spec_name": customization_spec_name,
    }
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_CLONE_VM_TASK,
        vmomi_path=f"/VirtualMachine/{template_moid}/CloneVM_Task",
        body=clone_body,
        params=gate_params,
    )
    if gate is not None:
        return gate

    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_CLONE_FROM_TEMPLATE_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == "error":
        raise RuntimeError(
            f"vm.clone_from_template: CloneVM_Task cloning {source_template!r} ({template_moid}) "
            f"to {new_vm_name!r} faulted: {outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return _clone_result(
            "timeout",
            source_template=source_template,
            source_template_id=template_moid,
            new_vm_name=new_vm_name,
            folder=folder_moid,
            task=outcome.task,
            customization_spec_name=customization_spec_name,
            guidance=(
                f"CloneVM_Task {outcome.task} did not reach a terminal state within "
                f"{int(_CLONE_FROM_TEMPLATE_TASK_TIMEOUT_SECONDS)}s; poll the task or list the "
                f"folder — the clone {new_vm_name!r} may still complete in the background"
            ),
        )
    return _clone_result(
        "cloned",
        source_template=source_template,
        source_template_id=template_moid,
        new_vm_name=new_vm_name,
        new_vm_id=_extract_task_vm_moid(outcome.result),
        folder=folder_moid,
        task=outcome.task,
        customization_spec_name=customization_spec_name,
    )


# ===========================================================================
# cluster.drs_rule.create (vim ClusterComputeResource reconfigure — #2895)
# ===========================================================================
#
# Add a DRS affinity / anti-affinity rule by *explicit VM list*. No REST
# path exists (the /vcenter/cluster module is list/get/evc-mode only; the
# tag-based compute-policies surface is tag-scoped, not an explicit VM list
# — spec-verified), so the rule add rides vim
# ``ClusterComputeResource.ReconfigureComputeResource_Task`` with a
# ``ClusterConfigSpecEx.rulesSpec`` delta (``modify=true`` — touches nothing
# else). Rides the #2893 substrate: the governed ``_write_vmomi_sub_op``
# seam + the ``poll_vim_task`` helper (the reconfigure returns a ``*_Task``).


def _build_cluster_rules_retrieve_params(cluster_moid: str) -> dict[str, Any]:
    """Build the ``RetrievePropertiesEx`` body reading a cluster's DRS rules.

    A single ``PropertyFilterSpec`` scoped to the ClusterComputeResource
    object requesting ``configurationEx.rule`` — the array of existing
    ``ClusterRuleInfo`` rules, each carrying its ``name``. The idempotence /
    name-collision check reads this before any write so a duplicate rule
    name returns a structured status instead of a raw vim ``DuplicateName``
    fault. The singleton ``propertyCollector`` moId rides the path, so the
    body is only the method args (the shape the typed reads send),
    ``_typeName``-annotated via the shared trio helper (#3103).
    """
    return retrieve_properties_body(
        _CLUSTER_COMPUTE_RESOURCE_MO_TYPE, [cluster_moid], [_PROP_CONFIGURATION_EX_RULE]
    )


def _extract_cluster_rule_names(retrieve_result: Any) -> set[str]:
    """Pull the existing DRS rule names off a ``RetrievePropertiesEx`` result."""
    payload = _unwrap_value(retrieve_result)
    objects = payload.get("objects", []) if isinstance(payload, dict) else payload
    names: set[str] = set()
    if not isinstance(objects, list):
        return names
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for prop in obj.get("propSet", []) or []:
            if not isinstance(prop, dict) or prop.get("name") != _PROP_CONFIGURATION_EX_RULE:
                continue
            rules = unwrap_vim_value(prop.get("val"))
            for rule in rules if isinstance(rules, list) else []:
                if isinstance(rule, dict) and isinstance(rule.get("name"), str):
                    names.add(rule["name"])
    return names


async def _resolve_cluster_name(
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    *,
    cluster: str,
) -> str | None:
    """Best-effort cluster display name via ``GET:/vcenter/cluster/{cluster}``.

    Cosmetic for the drs_rule preview — a transport fault nulls the name
    rather than sinking the preview, since the cluster moid + resolved VM
    set (not the name) is the decision. Mirrors :func:`_resolve_vm_name`.
    """
    try:
        info = await _read_sub_op(
            connector, target, operator, _OP_GET_CLUSTER, {"cluster": cluster}
        )
    except httpx.HTTPError:
        return None
    payload = _unwrap_value(info)
    name = payload.get("name") if isinstance(payload, dict) else None
    return name if isinstance(name, str) else None


async def _resolve_drs_rule_vms(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    cluster_moid: str,
    vm_names: list[str],
) -> list[dict[str, Any]]:
    """Resolve the rule's VM names to ``[{vm, name}]`` rows, scoped to the cluster.

    Read-only single ``GET:/vcenter/vm`` (via the shared
    :func:`_resolve_vm_list`) filtered by both ``names`` and ``clusters`` — a
    DRS rule is cluster-local, so scoping the resolution to the cluster
    disambiguates same-named VMs in other clusters and drops any name that
    does not name a VM in this cluster. Shared with the park-time preview
    builder so the reviewer sees the same resolved set the approved write
    references.
    """
    rows = await _resolve_vm_list(
        connector=connector,
        target=target,
        operator=operator,
        filter_dict={"names": vm_names, "clusters": [cluster_moid]},
    )
    resolved: list[dict[str, Any]] = []
    for row in rows:
        moid = row.get("vm")
        if isinstance(moid, str):
            name = row.get("name")
            resolved.append({"vm": moid, "name": name if isinstance(name, str) else None})
    return resolved


async def cluster_drs_rule_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Add a DRS affinity / anti-affinity rule via ReconfigureComputeResource_Task.

    Op-id: ``vmware.composite.cluster.drs_rule.create``. No REST path exists
    for a classic DRS rule by explicit VM list, so the add rides vim
    ``ClusterComputeResource.ReconfigureComputeResource_Task`` with a
    single-rule ``ClusterConfigSpecEx.rulesSpec`` delta (``modify=true``).

    Flow:

    1. Resolve the rule's VM *names* to MoRefs, scoped to the cluster (a
       *read*, un-gated). Fewer than two resolve → ``status="insufficient_vms"``
       before any write (an affinity / anti-affinity rule needs >=2 VMs).
    2. Read the cluster's existing rules (``configurationEx.rule``, a *read*)
       for the idempotence / name-collision check: a duplicate name returns
       ``status="rule_exists"`` — a structured status, not a raw vim
       ``DuplicateName`` fault.
    3. Issue the single ``ReconfigureComputeResource_Task`` add through the
       governed vmomi write seam (:func:`_write_vmomi_sub_op` → the #2254
       gate); a parked/denied gate returns the :class:`OperationResult`
       verbatim and no reconfigure fires.
    4. Poll the returned ``*_Task`` MoRef to a terminal state via the shared
       :func:`~meho_backplane.connectors.vmware_rest.vim_task.poll_vim_task`
       helper before reporting ``status="created"``. A task fault raises (the
       dispatcher wraps it ``connector_error``); a poll timeout returns
       ``status="timeout"`` with the task id.
    """
    cluster_moid = params["cluster"]
    rule_name = params["rule_name"]
    rule_type = params["rule_type"]
    enabled = bool(params.get("enabled", True))
    vm_names = [n for n in (params.get("vms") or []) if isinstance(n, str)]
    rule_info_type = _DRS_RULE_TYPE_TYPE_NAMES[rule_type]

    resolved = await _resolve_drs_rule_vms(
        connector=connector,
        target=target,
        operator=operator,
        cluster_moid=cluster_moid,
        vm_names=vm_names,
    )
    if len(resolved) < _DRS_RULE_MIN_VMS:
        return {
            "status": "insufficient_vms",
            "cluster": cluster_moid,
            "rule_name": rule_name,
            "rule_type": rule_type,
            "enabled": enabled,
            "task": None,
            "resolved_vms": resolved,
            "guidance": (
                f"a DRS {rule_type} rule needs at least {_DRS_RULE_MIN_VMS} VMs in the cluster; "
                f"{len(resolved)} of {len(vm_names)} requested name(s) resolved to a VM in "
                f"cluster {cluster_moid!r}"
            ),
        }

    existing = await connector._post_vmomi_json(
        target,
        _VMOMI_RETRIEVE_PROPERTIES_PATH,
        operator=operator,
        json=_build_cluster_rules_retrieve_params(cluster_moid),
    )
    if rule_name in _extract_cluster_rule_names(existing):
        return {
            "status": "rule_exists",
            "cluster": cluster_moid,
            "rule_name": rule_name,
            "rule_type": rule_type,
            "enabled": enabled,
            "task": None,
            "resolved_vms": resolved,
            "guidance": (
                f"a DRS rule named {rule_name!r} already exists on cluster {cluster_moid!r}; "
                "rule names are the idempotence key — pick a new name or remove the existing rule"
            ),
        }

    reconfig_spec = {
        "spec": {
            _VMOMI_TYPE_NAME_KEY: _CLUSTER_CONFIG_SPEC_EX_TYPE,
            "rulesSpec": [
                {
                    _VMOMI_TYPE_NAME_KEY: _CLUSTER_RULE_SPEC_TYPE,
                    "operation": "add",
                    "info": {
                        _VMOMI_TYPE_NAME_KEY: rule_info_type,
                        "name": rule_name,
                        "enabled": enabled,
                        "vm": [vim_moref(_VIRTUAL_MACHINE_MO_TYPE, row["vm"]) for row in resolved],
                    },
                }
            ],
        },
        "modify": True,
    }
    gate, task_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_RECONFIGURE_COMPUTE_RESOURCE_TASK,
        vmomi_path=f"/ClusterComputeResource/{cluster_moid}/ReconfigureComputeResource_Task",
        body=reconfig_spec,
        params={
            "cluster": cluster_moid,
            "rule_name": rule_name,
            "rule_type": rule_type,
            "vms": [row["vm"] for row in resolved],
        },
    )
    if gate is not None:
        return gate

    outcome = await poll_vim_task(
        connector,
        target,
        operator,
        task=_unwrap_value(task_payload),
        timeout_seconds=_DRS_RULE_TASK_TIMEOUT_SECONDS,
    )
    if outcome.state == "error":
        raise RuntimeError(
            f"cluster.drs_rule.create: ReconfigureComputeResource_Task on cluster "
            f"{cluster_moid!r} faulted: {outcome.error_message or '<no fault reported>'}"
        )
    if outcome.timed_out:
        return {
            "status": "timeout",
            "cluster": cluster_moid,
            "rule_name": rule_name,
            "rule_type": rule_type,
            "enabled": enabled,
            "task": outcome.task,
            "resolved_vms": resolved,
            "guidance": (
                f"ReconfigureComputeResource_Task {outcome.task} did not reach a terminal state "
                f"within {int(_DRS_RULE_TASK_TIMEOUT_SECONDS)}s; poll the task or re-read the "
                "cluster's DRS rules — the rule add may still complete in the background"
            ),
        }
    return {
        "status": "created",
        "cluster": cluster_moid,
        "rule_name": rule_name,
        "rule_type": rule_type,
        "enabled": enabled,
        "task": outcome.task,
        "resolved_vms": resolved,
        "guidance": None,
    }


# ===========================================================================
# folder.create (vim Folder.CreateFolder — synchronous, #2895)
# ===========================================================================
#
# Create a VM folder under a named parent. ``/vcenter/folder`` is GET-only
# (spec-verified), so the create rides vim ``Folder.CreateFolder`` — which
# is **synchronous**: it returns the new Folder ``ManagedObjectReference``
# directly (NOT a ``*_Task``), so unlike drs_rule / disk-grow there is no
# poll. Rides the #2893 governed vmomi write seam (``_write_vmomi_sub_op``).


def _moref_value(moref: Any) -> str | None:
    """Return the ``value`` field of a vim ``ManagedObjectReference``, else ``None``.

    A synchronous vim method that returns an object reference (e.g.
    ``Folder.CreateFolder`` → the new Folder MoRef) serialises as
    ``{"type": "Folder", "value": "group-v123"}``. A bare moid string is
    tolerated so a caller that already unwrapped the MoRef passes through.
    """
    if isinstance(moref, dict):
        value = moref.get("value")
        return value if isinstance(value, str) else None
    return moref if isinstance(moref, str) else None


async def _resolve_parent_vm_folder(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    parent_name: str,
) -> tuple[str | None, str | None]:
    """Resolve a VM-folder parent by display name to its moid.

    Read-only ``GET:/vcenter/folder`` filtered by ``names`` + the
    ``VIRTUAL_MACHINE`` folder type (the same listing ``vm.create`` resolves
    its placement folder from). Returns ``(moid, None)`` on a unique match,
    ``(None, "parent_not_found")`` on no match, or ``(None,
    "ambiguous_parent")`` when the name resolves to more than one VM folder —
    a structured status the caller surfaces instead of guessing a parent.
    """
    listing = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_LIST_FOLDERS,
        {"filter.names": [parent_name], "filter.type": "VIRTUAL_MACHINE"},
    )
    entries = _unwrap_value(listing)
    rows = [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    if not rows:
        return None, "parent_not_found"
    if len(rows) > 1:
        return None, "ambiguous_parent"
    moid = rows[0].get("folder")
    if not isinstance(moid, str):
        return None, "parent_not_found"
    return moid, None


async def folder_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a VM folder under a named parent via the synchronous vim CreateFolder.

    Op-id: ``vmware.composite.folder.create``. ``/vcenter/folder`` is
    GET-only (spec-verified), so the create rides vim ``Folder.CreateFolder``,
    which is **synchronous** — it returns the new Folder
    ``ManagedObjectReference`` directly, not a ``*_Task``, so (unlike
    drs_rule / disk-grow) the returned MoRef is **not** polled.

    Flow: resolve the parent folder *name* to a moid (a *read*; no match →
    ``status="parent_not_found"``, >1 match → ``status="ambiguous_parent"``),
    then issue the single ``CreateFolder`` through the governed vmomi write
    seam (:func:`_write_vmomi_sub_op` → the #2254 gate). A parked/denied gate
    returns the :class:`OperationResult` verbatim and no folder is created;
    otherwise the new folder MoRef is unwrapped into ``status="created"``.
    """
    parent_name = params["parent_folder"]
    new_name = params["folder_name"]

    parent_moid, resolve_error = await _resolve_parent_vm_folder(
        connector=connector, target=target, operator=operator, parent_name=parent_name
    )
    if parent_moid is None:
        return {
            "status": resolve_error,
            "parent_folder": parent_name,
            "parent_folder_id": None,
            "new_folder_name": new_name,
            "folder": None,
            "guidance": (
                f"parent VM folder name {parent_name!r} "
                + (
                    "resolved to more than one folder — pass a unique parent name"
                    if resolve_error == "ambiguous_parent"
                    else "did not resolve to a VM folder"
                )
            ),
        }

    gate, folder_payload = await _write_vmomi_sub_op(
        connector,
        target,
        operator,
        op_id=_OP_CREATE_FOLDER,
        vmomi_path=f"/Folder/{parent_moid}/CreateFolder",
        body={"name": new_name},
        params={"parent_folder": parent_moid, "folder_name": new_name},
    )
    if gate is not None:
        return gate

    new_folder_moid = _moref_value(folder_payload)
    return {
        "status": "created",
        "parent_folder": parent_name,
        "parent_folder_id": parent_moid,
        "new_folder_name": new_name,
        "folder": new_folder_moid,
        "guidance": None,
    }


# ===========================================================================
# Hardware write composites (#2891): vm.resize / vm.nic.repoint /
# vm.device.cdrom -- pure vSphere Automation REST post-clone reconfigure.
# ===========================================================================


async def _read_vm_info(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_moid: str,
) -> dict[str, Any] | None:
    """Read a VM's config via ``GET:/vcenter/vm/{vm}`` (name, power_state, cpu, memory).

    Shared seam (#1608): the ``vm.resize`` handler + preview read current
    sizing here, and the ``vm.nic.repoint`` / ``vm.device.cdrom`` previews
    read the VM's display ``name`` from it. One read-only GET on the
    connector session; returns the unwrapped ``Vm.Info`` dict, or ``None``
    when the payload is not a dict.
    """
    payload = await _read_sub_op(connector, target, operator, _OP_GET_VM, {"vm": vm_moid})
    info = _unwrap_value(payload)
    return info if isinstance(info, dict) else None


async def _read_ethernet_nic(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_moid: str,
    nic_id: str,
) -> dict[str, Any] | None:
    """Read one vNIC via ``GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}`` (mac + backing).

    Shared between the ``vm.nic.repoint`` handler and its preview builder
    (#1608). Returns the unwrapped ``Ethernet.Info`` dict or ``None``.
    """
    payload = await _read_sub_op(
        connector, target, operator, _OP_GET_VM_NIC, {"vm": vm_moid, "nic": nic_id}
    )
    info = _unwrap_value(payload)
    return info if isinstance(info, dict) else None


async def _read_cdrom(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    vm_moid: str,
    cdrom_id: str,
) -> dict[str, Any] | None:
    """Read one CD-ROM via ``GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}`` (backing + state).

    Shared between the ``vm.device.cdrom`` handler and its preview builder
    (#1608). Returns the unwrapped ``Cdrom.Info`` dict or ``None``.
    """
    payload = await _read_sub_op(
        connector, target, operator, _OP_GET_VM_CDROM, {"vm": vm_moid, "cdrom": cdrom_id}
    )
    info = _unwrap_value(payload)
    return info if isinstance(info, dict) else None


async def _resolve_distributed_portgroup(
    *,
    connector: VmwareRestConnector,
    target: Any,
    operator: Operator,
    portgroup_name: str,
) -> tuple[str | None, str, list[dict[str, Any]]]:
    """Resolve a distributed-portgroup display name to its network moid.

    Reads ``GET:/vcenter/network`` filtered to ``DISTRIBUTED_PORTGROUP`` +
    the name (the #1602 fix -- there is no dedicated portgroup list
    resource). Returns ``(network_moid, "ok", [])`` on a unique match,
    ``(None, "not_found", [])`` on no match, or ``(None, "ambiguous",
    candidates)`` when the name is not unique. Shared between the
    ``vm.nic.repoint`` handler (dispatch time) and its preview builder
    (#1608), so the reviewer sees the same portgroup the approved dispatch
    will bind.
    """
    listing = await _read_sub_op(
        connector,
        target,
        operator,
        _OP_LIST_NETWORK,
        {
            "filter.types": [_NETWORK_TYPE_DISTRIBUTED_PORTGROUP],
            "filter.names": [portgroup_name],
        },
    )
    rows = _unwrap_value(listing)
    matches = (
        [row for row in rows if isinstance(row, dict) and row.get("name") == portgroup_name]
        if isinstance(rows, list)
        else []
    )
    if not matches:
        return None, "not_found", []
    if len(matches) > 1:
        return None, "ambiguous", matches
    network_moid = matches[0].get("network")
    if not isinstance(network_moid, str):
        return None, "not_found", matches
    return network_moid, "ok", []


def _resize_requires_power_off(
    *,
    current: dict[str, Any],
    cpu_count: int | None,
    cores_per_socket: int | None,
    memory_mib: int | None,
    cpu_hot_add: bool,
    memory_hot_add: bool,
) -> str | None:
    """Return a reason when a *powered-on* VM cannot take the resize live, else ``None``.

    vCenter refuses (HTTP 400) a live CPU/memory change that is not a
    hot-add: a count/size *increase* needs the matching hot-add flag; a
    *decrease* or any ``cores_per_socket`` change is never live. Surfacing
    this as a typed ``requires_power_off`` status (rather than letting the
    PATCH 400) is the composite's contract. Consulted only when the VM is
    powered on.
    """
    cur_count = current.get("cpu_count")
    cur_cores = current.get("cores_per_socket")
    cur_mem = current.get("memory_MiB")
    if (
        cpu_count is not None
        and cpu_count != cur_count
        and (not cpu_hot_add or (isinstance(cur_count, int) and cpu_count < cur_count))
    ):
        return "CPU count change needs hot-add (increase only) on a powered-on VM; power off first"
    if (
        cores_per_socket is not None
        and isinstance(cur_cores, int)
        and cores_per_socket != cur_cores
    ):
        return "cores_per_socket cannot change on a powered-on VM; power off first"
    if (
        memory_mib is not None
        and memory_mib != cur_mem
        and (not memory_hot_add or (isinstance(cur_mem, int) and memory_mib < cur_mem))
    ):
        return "memory change needs hot-add (increase only) on a powered-on VM; power off first"
    return None


async def vm_resize_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Reconfigure a VM's CPU count / cores-per-socket and/or memory.

    Op-id: ``vmware.composite.vm.resize``. Reads current sizing + hot-add
    flags via ``GET:/vcenter/vm/{vm}``, then PATCHes ``hardware/cpu``
    and/or ``hardware/memory``. A change a powered-on VM cannot take live
    returns ``status='requires_power_off'`` (never a raw 400); a request
    already matching current returns ``no_change``; a CPU PATCH that lands
    followed by a memory PATCH that faults returns ``partial``.
    """
    vm_moid = params["vm"]
    req_cpu_count = params.get("cpu_count")
    req_cores = params.get("cores_per_socket")
    req_mem = params.get("memory_mib")

    info = await _read_vm_info(
        connector=connector, target=target, operator=operator, vm_moid=vm_moid
    )
    if info is None:
        raise RuntimeError(f"vm.resize: GET:/vcenter/vm/{vm_moid} returned no VM info dict")
    cpu_raw = info.get("cpu")
    mem_raw = info.get("memory")
    cpu_info = cpu_raw if isinstance(cpu_raw, dict) else {}
    mem_info = mem_raw if isinstance(mem_raw, dict) else {}
    power_state = info.get("power_state")
    from_sizing = {
        "cpu_count": cpu_info.get("count"),
        "cores_per_socket": cpu_info.get("cores_per_socket"),
        "memory_MiB": mem_info.get("size_MiB"),
    }
    base = {
        "vm": vm_moid,
        "name": info.get("name"),
        "power_state": power_state,
        "from": from_sizing,
        "to": {"cpu_count": req_cpu_count, "cores_per_socket": req_cores, "memory_MiB": req_mem},
    }

    cpu_changes = (req_cpu_count is not None and req_cpu_count != from_sizing["cpu_count"]) or (
        req_cores is not None and req_cores != from_sizing["cores_per_socket"]
    )
    mem_changes = req_mem is not None and req_mem != from_sizing["memory_MiB"]
    if not cpu_changes and not mem_changes:
        return {
            **base,
            "status": "no_change",
            "applied": {"cpu": False, "memory": False},
            "guidance": None,
        }

    if power_state == "POWERED_ON":
        reason = _resize_requires_power_off(
            current=from_sizing,
            cpu_count=req_cpu_count,
            cores_per_socket=req_cores,
            memory_mib=req_mem,
            cpu_hot_add=bool(cpu_info.get("hot_add_enabled")),
            memory_hot_add=bool(mem_info.get("hot_add_enabled")),
        )
        if reason is not None:
            return {
                **base,
                "status": "requires_power_off",
                "applied": {"cpu": False, "memory": False},
                "guidance": reason,
            }

    applied_cpu = False
    if cpu_changes:
        cpu_spec: dict[str, Any] = {}
        if req_cpu_count is not None:
            cpu_spec["count"] = req_cpu_count
        if req_cores is not None:
            cpu_spec["cores_per_socket"] = req_cores
        gate, _ = await _write_sub_op(
            connector, target, operator, _OP_UPDATE_VM_CPU, {"vm": vm_moid, **cpu_spec}
        )
        if gate is not None:
            return gate
        applied_cpu = True

    if mem_changes:
        try:
            gate, _ = await _write_sub_op(
                connector,
                target,
                operator,
                _OP_UPDATE_VM_MEMORY,
                {"vm": vm_moid, "size_MiB": req_mem},
            )
        except httpx.HTTPError as exc:
            if applied_cpu:
                return {
                    **base,
                    "status": "partial",
                    "applied": {"cpu": True, "memory": False},
                    "guidance": f"CPU applied; memory update failed: {exc}",
                }
            raise
        if gate is not None:
            return gate
        return {
            **base,
            "status": "resized",
            "applied": {"cpu": applied_cpu, "memory": True},
            "guidance": None,
        }

    return {
        **base,
        "status": "resized",
        "applied": {"cpu": applied_cpu, "memory": False},
        "guidance": None,
    }


async def vm_nic_repoint_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Repoint an existing vNIC to a different distributed portgroup.

    Op-id: ``vmware.composite.vm.nic.repoint``. Reads the NIC's current
    backing + MAC via ``GET:/vcenter/vm/{vm}/hardware/ethernet/{nic}``,
    resolves the target portgroup by name via ``GET:/vcenter/network``
    filtered to ``DISTRIBUTED_PORTGROUP`` (the #1602 fix -- no dedicated
    portgroup list resource), then PATCHes the NIC backing. A name that
    resolves to zero / many portgroups refuses the repoint
    (``not_found`` / ``ambiguous``) with no PATCH issued.
    """
    vm_moid = params["vm"]
    nic_id = params["nic"]
    portgroup_name = params["portgroup_name"]

    nic_info = await _read_ethernet_nic(
        connector=connector, target=target, operator=operator, vm_moid=vm_moid, nic_id=nic_id
    )
    mac_address = nic_info.get("mac_address") if nic_info else None
    current_backing = nic_info.get("backing") if nic_info else None

    network_moid, resolution, candidates = await _resolve_distributed_portgroup(
        connector=connector, target=target, operator=operator, portgroup_name=portgroup_name
    )
    base = {
        "vm": vm_moid,
        "nic": nic_id,
        "mac_address": mac_address,
        "current_backing": current_backing,
        "requested_backing": {"portgroup_id": network_moid, "portgroup_name": portgroup_name},
    }
    if resolution == "not_found":
        return {
            **base,
            "status": "not_found",
            "candidates": [],
            "guidance": f"no distributed portgroup named {portgroup_name!r}",
        }
    if resolution == "ambiguous":
        return {
            **base,
            "status": "ambiguous",
            "candidates": candidates,
            "guidance": "multiple distributed portgroups share the name; pass a unique name",
        }

    gate, _ = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_UPDATE_VM_NIC,
        {
            "vm": vm_moid,
            "nic": nic_id,
            "backing": {"type": _NETWORK_TYPE_DISTRIBUTED_PORTGROUP, "network": network_moid},
        },
    )
    if gate is not None:
        return gate
    return {**base, "status": "repointed", "candidates": [], "guidance": None}


async def vm_device_cdrom_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Remove / update / disconnect a VM CD-ROM device.

    Op-id: ``vmware.composite.vm.device.cdrom``. Reads the device's
    current backing + state via ``GET:/vcenter/vm/{vm}/hardware/cdrom/{cdrom}``
    (the host-local ISO path the approver needs to see), then dispatches
    the requested ``action``: ``remove`` (DELETE the device), ``update``
    (PATCH its backing -- requires ``backing``), or ``disconnect`` (POST
    ``?action=disconnect``). ``update`` without a ``backing`` object
    returns ``invalid_request`` with no write issued.
    """
    vm_moid = params["vm"]
    cdrom_id = params["cdrom"]
    action = params["action"]

    cdrom_info = await _read_cdrom(
        connector=connector, target=target, operator=operator, vm_moid=vm_moid, cdrom_id=cdrom_id
    )
    base = {
        "vm": vm_moid,
        "cdrom": cdrom_id,
        "action": action,
        "current_backing": cdrom_info.get("backing") if cdrom_info else None,
        "state": cdrom_info.get("state") if cdrom_info else None,
        "requested_backing": None,
        "guidance": None,
    }

    if action == "remove":
        gate, _ = await _write_sub_op(
            connector, target, operator, _OP_DELETE_VM_CDROM, {"vm": vm_moid, "cdrom": cdrom_id}
        )
        if gate is not None:
            return gate
        return {**base, "status": "removed"}

    if action == "disconnect":
        gate, _ = await _write_sub_op(
            connector, target, operator, _OP_DISCONNECT_VM_CDROM, {"vm": vm_moid, "cdrom": cdrom_id}
        )
        if gate is not None:
            return gate
        return {**base, "status": "disconnected"}

    backing = params.get("backing")
    if not isinstance(backing, dict):
        return {
            **base,
            "status": "invalid_request",
            "guidance": "action='update' needs a 'backing' object, e.g. {'type': 'CLIENT_DEVICE'}",
        }
    gate, _ = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_UPDATE_VM_CDROM,
        {"vm": vm_moid, "cdrom": cdrom_id, "backing": backing},
    )
    if gate is not None:
        return gate
    return {**base, "status": "updated", "requested_backing": backing}


# ===========================================================================
# guest.customization_spec.create + vm.customize (GOSC) (#2892)
# ===========================================================================
#
# GOSC is how a cloned VM gets its network identity (hostname, per-NIC
# static IP + gateway + DNS) and, on Windows, its sysprep identity on
# first boot. Two composites: create a reusable named spec, then apply a
# saved spec to a VM. Both plain REST -- no vim fallback.
#
# Secret hygiene (#1503) is load-bearing: the create params carry Windows
# admin / product-key / domain-join credentials. The handler consumes
# them into the sysprep body but they never reach a reviewer surface --
# the op is pinned ``credential_write`` (broadcast collapses to
# aggregate-only), the park-time preview echoes identity only
# (:mod:`._write_preview`), and the durable audit row stores a params
# hash. The handler builders below are the ONLY code that touches the
# secret values.


def _put_if_str(target: dict[str, Any], key: str, value: Any) -> None:
    """Set ``target[key] = value`` only when *value* is a non-empty string.

    Keeps the vCenter customization body free of empty / ``None`` fields
    (the API rejects some empty-string members) and holds the builders'
    branching down.
    """
    if isinstance(value, str) and value:
        target[key] = value


def _build_hostname_generator(hostname: str) -> dict[str, Any]:
    """Build a FIXED ``HostnameGenerator`` for *hostname*.

    vCenter models the guest host name as a generator
    (``{type: FIXED|PREFIX|VIRTUAL_MACHINE, fixed_name, prefix}``); the
    provisioning subset always pins an explicit name, so ``FIXED`` with
    ``fixed_name`` is the only shape built.
    """
    return {"type": "FIXED", "fixed_name": hostname}


def _build_interface(nic: dict[str, Any]) -> dict[str, Any]:
    """Map one agent-facing NIC dict to a vCenter ``AdapterMapping``.

    A NIC with an ``ip_address`` becomes a STATIC ``ipv4`` block
    (``{type, ip_address, prefix?, gateways?}``); a NIC without one
    becomes a DHCP adapter. Shape: ``{"adapter": {"ipv4": {...}}}``.
    """
    ip_address = nic.get("ip_address")
    if isinstance(ip_address, str) and ip_address:
        ipv4: dict[str, Any] = {"type": "STATIC", "ip_address": ip_address}
        prefix = nic.get("prefix")
        if isinstance(prefix, int):
            ipv4["prefix"] = prefix
        gateways = nic.get("gateways")
        if isinstance(gateways, list) and gateways:
            ipv4["gateways"] = [g for g in gateways if isinstance(g, str)]
    else:
        ipv4 = {"type": "DHCP"}
    return {"adapter": {"ipv4": ipv4}}


def _build_linux_config(params: dict[str, Any]) -> dict[str, Any]:
    """Build ``configuration_spec.linux_config`` from the agent params.

    ``hostname`` and ``domain`` are REQUIRED by the pinned
    ``Vcenter.Guest.LinuxConfiguration`` schema (vcenter.yaml:126320), so
    ``domain`` is always emitted (empty string when unset) -- omitting it
    fails even a minimal Linux create. ``time_zone`` (a Linux tz-name
    string here) is optional and dropped when absent.
    """
    linux_config: dict[str, Any] = {
        "hostname": _build_hostname_generator(params["hostname"]),
        "domain": params.get("domain") or "",
    }
    _put_if_str(linux_config, "time_zone", params.get("time_zone"))
    return linux_config


def _build_windows_sysprep(params: dict[str, Any]) -> dict[str, Any]:
    """Build the ``windows_config.sysprep`` body, consuming the secret fields.

    The credential members (``gui_unattended.password``,
    ``user_data.product_key``, ``domain.domain_password``) are read here
    and here only -- they never leave for a reviewer / preview /
    broadcast surface (#1503).

    ``UserData`` (``full_name`` / ``organization`` / ``product_key``,
    vcenter.yaml:126067) and ``GuiUnattended`` (``auto_logon`` /
    ``auto_logon_count`` / ``time_zone``, vcenter.yaml:126181) fields are
    REQUIRED by the pinned schema, so they are always emitted (empty
    string / derived defaults) -- their omission fails every Windows
    create. ``time_zone`` here is the REST integer MS index (not the Linux
    tz-name string). Domain join lives under ``domain`` ->
    ``Vcenter.Guest.Domain`` (vcenter.yaml:126104), NOT the pyvmomi
    ``identification`` key.
    """
    auto_logon = bool(params.get("windows_auto_logon", False))
    user_data: dict[str, Any] = {
        "computer_name": _build_hostname_generator(params["hostname"]),
        "full_name": params.get("windows_full_name") or "",
        "organization": params.get("windows_organization") or "",
        "product_key": params.get("windows_product_key") or "",
    }

    gui_unattended: dict[str, Any] = {
        "auto_logon": auto_logon,
        "auto_logon_count": 1 if auto_logon else 0,
        "time_zone": int(params.get("windows_time_zone", _DEFAULT_WINDOWS_TIME_ZONE)),
    }
    _put_if_str(gui_unattended, "password", params.get("windows_admin_password"))

    sysprep: dict[str, Any] = {"user_data": user_data, "gui_unattended": gui_unattended}

    join_domain = params.get("windows_join_domain")
    if isinstance(join_domain, str) and join_domain:
        domain: dict[str, Any] = {"type": "DOMAIN", "domain": join_domain}
        _put_if_str(domain, "domain_username", params.get("windows_domain_admin_username"))
        _put_if_str(domain, "domain_password", params.get("windows_domain_admin_password"))
        sysprep["domain"] = domain
    return sysprep


def _build_customization_create_body(params: dict[str, Any]) -> dict[str, Any]:
    """Assemble the ``POST:/vcenter/guest/customization-specs`` request body.

    Maps the agent-facing GOSC subset onto the vCenter ``CreateSpec``
    (``{name, description, spec}``) whose ``spec`` field is a
    ``CustomizationSpec`` (``{configuration_spec, interfaces,
    global_dns_settings}``). The ``CreateSpec`` fields sit at the **top
    level** of the ``/api`` request body -- the modern surface takes the
    ``CreateSpec`` directly (#2973), so the only ``spec`` key here is the
    legitimate ``CustomizationSpec`` field, not a ``/rest``-style envelope.
    """
    if params["os_type"] == "linux":
        configuration_spec = {"linux_config": _build_linux_config(params)}
    else:
        configuration_spec = {
            "windows_config": {"reboot": "REBOOT", "sysprep": _build_windows_sysprep(params)},
        }
    interfaces = [
        _build_interface(nic) for nic in params.get("interfaces") or [] if isinstance(nic, dict)
    ]
    customization_spec: dict[str, Any] = {
        "configuration_spec": configuration_spec,
        "interfaces": interfaces,
        "global_dns_settings": {
            "dns_servers": [s for s in params.get("dns_servers") or [] if isinstance(s, str)],
            "dns_suffix_list": [
                s for s in params.get("dns_suffix_list") or [] if isinstance(s, str)
            ],
        },
    }
    # ``description`` is REQUIRED on the CreateSpec (vcenter.yaml:126873), so
    # it is always emitted (empty string when unset) -- omitting it fails
    # even a minimal create.
    create_spec: dict[str, Any] = {
        "name": params["spec_name"],
        "description": params.get("description") or "",
        "spec": customization_spec,
    }
    return create_spec


async def guest_customization_spec_create_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Create a reusable named guest customization (GOSC) spec.

    Op-id: ``vmware.composite.guest.customization_spec.create``. Single
    write sub-op (``POST:/vcenter/guest/customization-specs``) routed
    through the #2254 governance seam. The Windows credential params are
    consumed into the sysprep body by
    :func:`_build_customization_create_body` and never surface on a
    reviewer / preview / broadcast / audit surface (#1503) -- the op is
    pinned ``credential_write``.

    On a parked / denied gate the seam's :class:`OperationResult` returns
    verbatim; a transport / vCenter fault propagates as the dispatcher's
    ``connector_error``.
    """
    spec_name = params["spec_name"]
    os_type = params["os_type"]
    body = _build_customization_create_body(params)
    gate, _ = await _write_sub_op(connector, target, operator, _OP_CREATE_CUSTOMIZATION_SPEC, body)
    if gate is not None:
        return gate
    return {"status": "created", "spec_name": spec_name, "os_type": os_type}


def _vm_customize_identity(row: dict[str, Any]) -> dict[str, Any]:
    """Identity projection of a VM listing row for the ``ambiguous`` candidate list."""
    return {key: row[key] for key in ("vm", "name", "power_state") if row.get(key) is not None}


def _vm_customize_result(
    *,
    status: str,
    vm: str | None,
    name: str,
    spec_name: str,
    power_state: str | None,
    applies_on: str | None,
    guidance: str | None,
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the canonical ``vm.customize`` response envelope."""
    return {
        "status": status,
        "vm": vm,
        "name": name,
        "spec_name": spec_name,
        "power_state": power_state,
        "applies_on": applies_on,
        "candidates": candidates or [],
        "guidance": guidance,
    }


async def vm_customize_composite(
    *,
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    connector: VmwareRestConnector,
) -> dict[str, Any] | OperationResult:
    """Apply a saved customization spec to a VM (resolved by name); optional power-on.

    Op-id: ``vmware.composite.vm.customize``. Resolves the VM by display
    name, refuses a powered-on VM with a structured
    ``precondition_failed`` (vCenter only accepts a pending customization
    on a powered-off VM), then ``PUT:/vcenter/vm/{vm}/guest/customization``
    with the named spec. When ``power_on=True`` the VM is powered on
    afterward so the customization applies on that boot.

    The single spec reference carries no secret (the secret material
    lives in the saved spec, created separately); the write sub-ops route
    through the #2254 governance seam.
    """
    vm_name = params["name"]
    spec_name = params["spec_name"]
    power_on = bool(params.get("power_on", False))

    vms = await _resolve_vm_list(
        connector=connector, target=target, operator=operator, filter_dict={"names": [vm_name]}
    )
    if not vms:
        return _vm_customize_result(
            status="not_found",
            vm=None,
            name=vm_name,
            spec_name=spec_name,
            power_state=None,
            applies_on=None,
            guidance=f"no VM named {vm_name!r} resolved",
        )
    if len(vms) > 1:
        return _vm_customize_result(
            status="ambiguous",
            vm=None,
            name=vm_name,
            spec_name=spec_name,
            power_state=None,
            applies_on=None,
            guidance="multiple VMs share the requested name -- rename or clean up duplicates",
            candidates=[_vm_customize_identity(row) for row in vms],
        )
    vm_row = vms[0]
    vm_moid = vm_row.get("vm")
    power_state = vm_row.get("power_state") if isinstance(vm_row.get("power_state"), str) else None
    if not isinstance(vm_moid, str):
        return _vm_customize_result(
            status="not_found",
            vm=None,
            name=vm_name,
            spec_name=spec_name,
            power_state=None,
            applies_on=None,
            guidance="matched VM listing row missing ``vm`` key",
        )
    if power_state == "POWERED_ON":
        return _vm_customize_result(
            status="precondition_failed",
            vm=vm_moid,
            name=vm_name,
            spec_name=spec_name,
            power_state=power_state,
            applies_on=None,
            guidance="VM is powered on; power it off before setting a pending guest customization",
        )

    gate, _ = await _write_sub_op(
        connector,
        target,
        operator,
        _OP_SET_VM_CUSTOMIZATION,
        {"vm": vm_moid, "name": spec_name},
    )
    if gate is not None:
        return gate

    if power_on:
        gate, _ = await _write_sub_op(
            connector, target, operator, _power_vm_op_id("start"), {"vm": vm_moid}
        )
        if gate is not None:
            return gate
        return _vm_customize_result(
            status="powered_on",
            vm=vm_moid,
            name=vm_name,
            spec_name=spec_name,
            power_state=power_state,
            applies_on="next_power_on",
            guidance=None,
        )
    return _vm_customize_result(
        status="customization_set",
        vm=vm_moid,
        name=vm_name,
        spec_name=spec_name,
        power_state=power_state,
        applies_on="next_power_on",
        guidance=None,
    )

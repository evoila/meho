# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The VM hardware writes: disk grow and attach, resize, NIC repoint and the CD drive.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._vm_llm_instructions import (
    VM_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    vm_device_cdrom_composite,
    vm_disk_attach_composite,
    vm_disk_grow_composite,
    vm_nic_repoint_composite,
    vm_resize_composite,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _vm_device_cdrom_preview,
    _vm_disk_attach_preview,
    _vm_disk_grow_preview,
    _vm_nic_repoint_preview,
    _vm_resize_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_DEVICE_CDROM_PARAMETER_SCHEMA,
    VM_DEVICE_CDROM_RESPONSE_SCHEMA,
    VM_DISK_ATTACH_PARAMETER_SCHEMA,
    VM_DISK_ATTACH_RESPONSE_SCHEMA,
    VM_DISK_GROW_PARAMETER_SCHEMA,
    VM_DISK_GROW_RESPONSE_SCHEMA,
    VM_NIC_REPOINT_PARAMETER_SCHEMA,
    VM_NIC_REPOINT_RESPONSE_SCHEMA,
    VM_RESIZE_PARAMETER_SCHEMA,
    VM_RESIZE_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.vm.disk.grow",
        handler=vm_disk_grow_composite,
        summary="Grow a VM's virtual disk to a larger capacity via ReconfigVM_Task.",
        description=(
            "Makes one virtual disk of a VM larger. A smaller or equal size is "
            "refused before any change. The guest must still extend its file "
            "system. Like 'govc vm.disk.change -size'."
        ),
        parameter_schema=VM_DISK_GROW_PARAMETER_SCHEMA,
        response_schema=VM_DISK_GROW_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "disk", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.disk.grow"],
        preview=_vm_disk_grow_preview,
        governed_subops=_write._VIM_SUB_OPS_VM_DISK_GROW,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.disk.attach",
        handler=vm_disk_attach_composite,
        summary="Attach an existing VMDK to a VM at an explicit SCSI controller/unit address.",
        description=(
            "Attaches an existing VMDK file to a VM at a given SCSI controller and "
            "unit, for example the shared disk of a cluster. It checks the path, "
            "the controller and that the unit is free first. It never creates a "
            "disk."
        ),
        parameter_schema=VM_DISK_ATTACH_PARAMETER_SCHEMA,
        response_schema=VM_DISK_ATTACH_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "disk", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.disk.attach"],
        preview=_vm_disk_attach_preview,
        governed_subops=_write._VIM_SUB_OPS_VM_DISK_ATTACH,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.resize",
        handler=vm_resize_composite,
        summary="Reconfigure a VM's CPU count / cores-per-socket and/or memory.",
        description=(
            "Changes a VM's vCPU count, cores per socket and/or memory. A change a "
            "running VM cannot take returns requires_power_off; a request that "
            "matches today returns no_change."
        ),
        parameter_schema=VM_RESIZE_PARAMETER_SCHEMA,
        response_schema=VM_RESIZE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.resize"],
        preview=_vm_resize_preview,
        governed_subops=_write._SUB_OPS_VM_RESIZE,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.nic.repoint",
        handler=vm_nic_repoint_composite,
        summary="Repoint a vNIC onto a distributed or standard portgroup.",
        description=(
            "Moves one VM network adapter to another distributed or standard port "
            "group, found by name or by network moid. A name that matches no "
            "network or several changes nothing. Like 'govc vm.network.change'."
        ),
        parameter_schema=VM_NIC_REPOINT_PARAMETER_SCHEMA,
        response_schema=VM_NIC_REPOINT_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "networking"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.nic.repoint"],
        preview=_vm_nic_repoint_preview,
        governed_subops=_write._SUB_OPS_VM_NIC_REPOINT,
        classifier_other=True,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.device.cdrom",
        handler=vm_device_cdrom_composite,
        summary="Remove / update / disconnect a VM CD-ROM device.",
        description=(
            "Removes a VM's CD-ROM drive, changes its backing (for example from a "
            "host-local ISO to CLIENT_DEVICE) or disconnects it. The current "
            "backing is read first, so the approver sees it."
        ),
        parameter_schema=VM_DEVICE_CDROM_PARAMETER_SCHEMA,
        response_schema=VM_DEVICE_CDROM_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "hardware"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.device.cdrom"],
        preview=_vm_device_cdrom_preview,
        governed_subops=_write._SUB_OPS_VM_DEVICE_CDROM,
        classifier_other=True,
    ),
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The VM lifecycle writes: create, clone, deploy, import, destroy, power,
migrate and the snapshot revert.

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
    vm_clone_composite,
    vm_clone_from_template_composite,
    vm_create_composite,
    vm_deploy_from_library_composite,
    vm_destroy_composite,
    vm_import_from_library_composite,
    vm_migrate_composite,
    vm_power_bulk_composite,
    vm_power_composite,
    vm_snapshot_revert_composite,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _vm_clone_from_template_preview,
    _vm_clone_preview,
    _vm_create_preview,
    _vm_deploy_from_library_preview,
    _vm_destroy_preview,
    _vm_migrate_preview,
    _vm_power_bulk_preview,
    _vm_power_preview,
    _vm_snapshot_revert_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    VM_CLONE_FROM_TEMPLATE_PARAMETER_SCHEMA,
    VM_CLONE_FROM_TEMPLATE_RESPONSE_SCHEMA,
    VM_CLONE_PARAMETER_SCHEMA,
    VM_CLONE_RESPONSE_SCHEMA,
    VM_CREATE_PARAMETER_SCHEMA,
    VM_CREATE_RESPONSE_SCHEMA,
    VM_DEPLOY_FROM_LIBRARY_PARAMETER_SCHEMA,
    VM_DEPLOY_FROM_LIBRARY_RESPONSE_SCHEMA,
    VM_DESTROY_PARAMETER_SCHEMA,
    VM_DESTROY_RESPONSE_SCHEMA,
    VM_IMPORT_FROM_LIBRARY_PARAMETER_SCHEMA,
    VM_IMPORT_FROM_LIBRARY_RESPONSE_SCHEMA,
    VM_MIGRATE_PARAMETER_SCHEMA,
    VM_MIGRATE_RESPONSE_SCHEMA,
    VM_POWER_BULK_PARAMETER_SCHEMA,
    VM_POWER_BULK_RESPONSE_SCHEMA,
    VM_POWER_PARAMETER_SCHEMA,
    VM_POWER_RESPONSE_SCHEMA,
    VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA,
    VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.vm.create",
        handler=vm_create_composite,
        summary="Create a VM with NIC attach, optional VHV enable + optional power-on.",
        description=(
            "Creates a new VM in a folder, adds its NICs and optional data disks, "
            "and can turn on nested virtualization and power it on. If a step after "
            "the create fails, MEHO tries to remove the VM again; rollback.outcome "
            "says whether it did. Like 'govc vm.create'."
        ),
        parameter_schema=VM_CREATE_PARAMETER_SCHEMA,
        response_schema=VM_CREATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.create"],
        preview=_vm_create_preview,
        governed_subops=_write._SUB_OPS_VM_CREATE + _write._VIM_SUB_OPS_VM_CREATE,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.clone",
        handler=vm_clone_composite,
        summary="Clone a VM from a content-library template (synchronous deploy).",
        description=(
            "Makes a new VM from a VM template item in a content library. The "
            "deploy is synchronous, so the result carries the new VM id. Like 'govc "
            "vm.clone'."
        ),
        parameter_schema=VM_CLONE_PARAMETER_SCHEMA,
        response_schema=VM_CLONE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.clone"],
        preview=_vm_clone_preview,
        governed_subops=_write._SUB_OPS_VM_CLONE,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.deploy_from_library",
        handler=vm_deploy_from_library_composite,
        summary="Deploy an OVF/OVA content-library item to a new VM (retires govc library.deploy).",
        description=(
            "Deploys an OVF/OVA item from a content library as a new VM or vApp, "
            "found by item id or by name. Name lookups that match no item or "
            "several are refused before anything changes. Like 'govc "
            "library.deploy'."
        ),
        parameter_schema=VM_DEPLOY_FROM_LIBRARY_PARAMETER_SCHEMA,
        response_schema=VM_DEPLOY_FROM_LIBRARY_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "ovf"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.deploy_from_library"],
        preview=_vm_deploy_from_library_preview,
        governed_subops=_write._SUB_OPS_VM_DEPLOY_FROM_LIBRARY
        + _write._VIM_SUB_OPS_VM_DEPLOY_FROM_LIBRARY,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.import_from_library",
        handler=vm_import_from_library_composite,
        summary="Import an OVF/OVA content-library item to a new VM via a typed HttpNfcLease.",
        description=(
            "Imports a large OVF/OVA item from a content library as a new VM: MEHO "
            "streams the disks to vCenter itself, so a long copy does not time out. "
            "Works on vCenter 8 and 9. A failed import is cancelled."
        ),
        parameter_schema=VM_IMPORT_FROM_LIBRARY_PARAMETER_SCHEMA,
        response_schema=VM_IMPORT_FROM_LIBRARY_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "ovf"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.import_from_library"],
        governed_subops=_write._SUB_OPS_VM_IMPORT_FROM_LIBRARY
        + _write._VIM_SUB_OPS_VM_IMPORT_FROM_LIBRARY,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.snapshot.revert",
        handler=vm_snapshot_revert_composite,
        summary="Revert a VM to a snapshot, by name or id; reject on name ambiguity.",
        description=(
            "Puts a VM back to a snapshot, given by name or by id. Everything since "
            "that snapshot is lost. If two snapshots share the name, nothing "
            "changes; pass the id. Like 'govc snapshot.revert'."
        ),
        parameter_schema=VM_SNAPSHOT_REVERT_PARAMETER_SCHEMA,
        response_schema=VM_SNAPSHOT_REVERT_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "snapshot"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.snapshot.revert"],
        preview=_vm_snapshot_revert_preview,
        governed_subops=_write._VIM_SUB_OPS_VM_SNAPSHOT_REVERT,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.destroy",
        handler=vm_destroy_composite,
        summary="Permanently destroy (delete) a powered-off VM and all its disks.",
        description=(
            "Deletes a powered-off VM and all its disks for good. A VM that is not "
            "powered off is refused. Destructive: a second person must always "
            "approve it, after seeing the VM, its disks, NICs and snapshots. The "
            "only exception is a standing delete grant on an instance in sandbox "
            "mode. Like 'govc vm.destroy'."
        ),
        parameter_schema=VM_DESTROY_PARAMETER_SCHEMA,
        response_schema=VM_DESTROY_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.destroy"],
        preview=_vm_destroy_preview,
        governed_subops=_write._SUB_OPS_VM_DESTROY + _write._VIM_SUB_OPS_VM_DESTROY,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.migrate",
        handler=vm_migrate_composite,
        summary="Migrate a VM via DRS recommendation or explicit target host.",
        description=(
            "Moves a VM to another host, where DRS recommends or to a host you "
            "name. Without a DRS recommendation and without target_host it changes "
            "nothing. Like 'govc vm.migrate'."
        ),
        parameter_schema=VM_MIGRATE_PARAMETER_SCHEMA,
        response_schema=VM_MIGRATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "drs"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.migrate"],
        preview=_vm_migrate_preview,
        governed_subops=_write._SUB_OPS_VM_MIGRATE + _write._VIM_SUB_OPS_VM_MIGRATE,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.power.bulk",
        handler=vm_power_bulk_composite,
        summary="Apply a power action to every VM matching a filter; aggregate results.",
        description=(
            "Runs one power action (start, stop, suspend, reset) on every VM that "
            "matches a filter and reports each VM's result. One failure does not "
            "stop the rest unless fail_fast is set."
        ),
        parameter_schema=VM_POWER_BULK_PARAMETER_SCHEMA,
        response_schema=VM_POWER_BULK_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "bulk"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.power.bulk"],
        preview=_vm_power_bulk_preview,
        governed_subops=_write._SUB_OPS_VM_POWER_BULK,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.power",
        handler=vm_power_composite,
        summary="Apply one power verb to a single VM, including a Tools soft shutdown.",
        description=(
            "Changes the power state of one VM: on, off, reset, or a clean guest "
            "shutdown or reboot through VMware Tools. A guest verb without running "
            "Tools returns tools_unavailable."
        ),
        parameter_schema=VM_POWER_PARAMETER_SCHEMA,
        response_schema=VM_POWER_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "power"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.power"],
        preview=_vm_power_preview,
        governed_subops=_write._SUB_OPS_VM_POWER,
    ),
    CompositeSpec(
        op_id="vmware.composite.vm.clone_from_template",
        handler=vm_clone_from_template_composite,
        summary="Clone a folder VM template via CloneVM_Task, with optional inline GOSC.",
        description=(
            "Clones a template VM that lives in a VM folder into a new VM, with "
            "optional host, power-on and a saved guest customization spec applied "
            "in the same step. Refuses a source that is not a template."
        ),
        parameter_schema=VM_CLONE_FROM_TEMPLATE_PARAMETER_SCHEMA,
        response_schema=VM_CLONE_FROM_TEMPLATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "lifecycle", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.vm.clone_from_template"],
        preview=_vm_clone_from_template_preview,
        governed_subops=_write._VIM_SUB_OPS_VM_CLONE_FROM_TEMPLATE,
    ),
)

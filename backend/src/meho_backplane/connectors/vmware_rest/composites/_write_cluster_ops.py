# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Cluster, DRS rule, resource pool and folder writes.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites._host_cluster_llm_instructions import (
    HOST_CLUSTER_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._vm_llm_instructions import (
    VM_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._write import (
    cluster_drs_rule_create_composite,
    cluster_drs_vm_host_rule_create_composite,
    cluster_patch_composite,
    folder_create_composite,
    resource_pool_create_composite,
    resource_pool_delete_composite,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _cluster_drs_rule_create_preview,
    _cluster_drs_vm_host_rule_create_preview,
    _cluster_patch_preview,
    _folder_create_preview,
    _resource_pool_create_preview,
    _resource_pool_delete_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA,
    CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA,
    CLUSTER_DRS_VM_HOST_RULE_CREATE_PARAMETER_SCHEMA,
    CLUSTER_DRS_VM_HOST_RULE_CREATE_RESPONSE_SCHEMA,
    CLUSTER_PATCH_PARAMETER_SCHEMA,
    CLUSTER_PATCH_RESPONSE_SCHEMA,
    FOLDER_CREATE_PARAMETER_SCHEMA,
    FOLDER_CREATE_RESPONSE_SCHEMA,
    RESOURCE_POOL_CREATE_PARAMETER_SCHEMA,
    RESOURCE_POOL_CREATE_RESPONSE_SCHEMA,
    RESOURCE_POOL_DELETE_PARAMETER_SCHEMA,
    RESOURCE_POOL_DELETE_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.cluster.patch",
        handler=cluster_patch_composite,
        summary="Update every host of a cluster in turn: maintenance mode, apply image, exit.",
        description=(
            "Updates the hosts of a cluster one at a time: maintenance mode on, apply the host's "
            "vSphere Lifecycle Manager image, maintenance mode off. The first failure stops the "
            "run and lists the hosts not yet done."
        ),
        parameter_schema=CLUSTER_PATCH_PARAMETER_SCHEMA,
        response_schema=CLUSTER_PATCH_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "patch", "long-running"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.cluster.patch"],
        preview=_cluster_patch_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.cluster.drs_rule.create",
        handler=cluster_drs_rule_create_composite,
        summary="Add a DRS rule that keeps listed VMs together or apart.",
        description=(
            "Adds a DRS affinity rule (keep the VMs on one host) or anti-affinity rule (keep them "
            "on different hosts) for VMs of a cluster, given by name. A rule name that already "
            "exists changes nothing. Like 'govc cluster.rule.create'."
        ),
        parameter_schema=CLUSTER_DRS_RULE_CREATE_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_RULE_CREATE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.cluster.drs_rule.create"],
        preview=_cluster_drs_rule_create_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.cluster.drs_vm_host_rule.create",
        handler=cluster_drs_vm_host_rule_create_composite,
        summary="Add a DRS rule that runs a VM group on, or away from, a host group.",
        description=(
            "Creates a VM group, a host group and a DRS rule that keeps those VMs on those hosts, "
            "or away from them, in one cluster change. With use_existing_groups it uses two "
            "groups that exist and creates only the rule. A name already taken changes nothing."
        ),
        parameter_schema=CLUSTER_DRS_VM_HOST_RULE_CREATE_PARAMETER_SCHEMA,
        response_schema=CLUSTER_DRS_VM_HOST_RULE_CREATE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "drs", "vi-json"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS[
            "vmware.composite.cluster.drs_vm_host_rule.create"
        ],
        preview=_cluster_drs_vm_host_rule_create_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.resource_pool.create",
        handler=resource_pool_create_composite,
        summary="Create a resource pool under a parent pool or a cluster's root pool.",
        description=(
            "Creates a resource pool inside a parent pool, or at the top of a cluster, with "
            "optional CPU and memory reservations, limits and shares. Returns the new pool moid "
            "and checks that it lists under the parent. Like 'govc pool.create'."
        ),
        parameter_schema=RESOURCE_POOL_CREATE_PARAMETER_SCHEMA,
        response_schema=RESOURCE_POOL_CREATE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "resource-pool"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.resource_pool.create"],
        preview=_resource_pool_create_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.resource_pool.delete",
        handler=resource_pool_delete_composite,
        summary="Delete a resource pool; refuse a non-empty pool unless force.",
        description=(
            "Deletes a resource pool. vCenter moves its child pools and VMs up to the parent "
            "pool; they are not deleted. A pool that is not empty is refused unless force is "
            "true. Like 'govc pool.destroy'."
        ),
        parameter_schema=RESOURCE_POOL_DELETE_PARAMETER_SCHEMA,
        response_schema=RESOURCE_POOL_DELETE_RESPONSE_SCHEMA,
        group_key="cluster",
        tags=["composite", "write", "cluster", "resource-pool"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=HOST_CLUSTER_LLM_INSTRUCTIONS["vmware.composite.resource_pool.delete"],
        preview=_resource_pool_delete_preview,
    ),
    CompositeSpec(
        op_id="vmware.composite.folder.create",
        handler=folder_create_composite,
        summary="Create a VM folder under a parent folder (synchronous vim CreateFolder).",
        description=(
            "Creates a VM folder under a parent VM folder, given by name or by "
            "moid. A parent name that matches no folder or several is refused "
            "before anything changes. Like 'govc folder.create'."
        ),
        parameter_schema=FOLDER_CREATE_PARAMETER_SCHEMA,
        response_schema=FOLDER_CREATE_RESPONSE_SCHEMA,
        group_key="vm",
        tags=["composite", "write", "vm", "inventory", "vi-json"],
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions=VM_LLM_INSTRUCTIONS["vmware.composite.folder.create"],
        preview=_folder_create_preview,
    ),
)

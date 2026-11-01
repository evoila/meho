# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Storage policy: create, delete and list.

The registration rows of these composites. ``_register.py`` collects
``COMPOSITES`` from every module of this package at start-up.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vmware_rest.composites import _storage_policy
from meho_backplane.connectors.vmware_rest.composites._spec import (
    CompositeSpec,
)
from meho_backplane.connectors.vmware_rest.composites._storage_network_llm_instructions import (
    STORAGE_NETWORK_LLM_INSTRUCTIONS,
)
from meho_backplane.connectors.vmware_rest.composites._storage_policy import (
    storage_policy_create_composite,
    storage_policy_delete_composite,
    storage_policy_list_composite,
)
from meho_backplane.connectors.vmware_rest.composites._write_preview import (
    _storage_policy_create_preview,
    _storage_policy_delete_preview,
)
from meho_backplane.connectors.vmware_rest.composites.schemas import (
    STORAGE_POLICY_CREATE_PARAMETER_SCHEMA,
    STORAGE_POLICY_CREATE_RESPONSE_SCHEMA,
    STORAGE_POLICY_DELETE_PARAMETER_SCHEMA,
    STORAGE_POLICY_DELETE_RESPONSE_SCHEMA,
    STORAGE_POLICY_LIST_PARAMETER_SCHEMA,
    STORAGE_POLICY_LIST_RESPONSE_SCHEMA,
)

__all__ = ["COMPOSITES"]

COMPOSITES: Final[tuple[CompositeSpec, ...]] = (
    CompositeSpec(
        op_id="vmware.composite.storage_policy.list",
        handler=storage_policy_list_composite,
        summary="List the VM storage policies of vCenter.",
        description=(
            "Reads the VM storage policies of vCenter (GET /vcenter/storage/policies). "
            "policy_ids keeps only the given ids. A long list comes back as a link to the "
            "stored result. Use it to check storage_policy.create and storage_policy.delete. "
            "Read-only."
        ),
        parameter_schema=STORAGE_POLICY_LIST_PARAMETER_SCHEMA,
        response_schema=STORAGE_POLICY_LIST_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "read-only", "storage", "policy", "spbm"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.storage_policy.list"],
    ),
    CompositeSpec(
        op_id="vmware.composite.storage_policy.create",
        handler=storage_policy_create_composite,
        summary="Create a tag-based VM storage policy for NFS datastores.",
        description=(
            "Creates a tag category and a tag, puts the tag on the named datastores, then "
            "creates a VM storage policy that asks for that tag (PBM API). NFS datastores need "
            "this: they have no default policy. Objects that already exist with the same name "
            "are reused."
        ),
        parameter_schema=STORAGE_POLICY_CREATE_PARAMETER_SCHEMA,
        response_schema=STORAGE_POLICY_CREATE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "write", "storage", "policy", "spbm", "tagging", "nfs"],
        safety_level="caution",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.storage_policy.create"],
        preview=_storage_policy_create_preview,
        governed_subops=_storage_policy._SUB_OPS_STORAGE_POLICY_CREATE,
    ),
    CompositeSpec(
        op_id="vmware.composite.storage_policy.delete",
        handler=storage_policy_delete_composite,
        summary="Delete a VM storage policy by id and check that it is gone.",
        description=(
            "Deletes one VM storage policy (PBM API), then reads the policy list again to check "
            "that it is gone. A policy that is still in use is refused (delete_failed). The "
            "tag and tag category stay. A second person must always approve it."
        ),
        parameter_schema=STORAGE_POLICY_DELETE_PARAMETER_SCHEMA,
        response_schema=STORAGE_POLICY_DELETE_RESPONSE_SCHEMA,
        group_key="storage",
        tags=["composite", "write", "storage", "policy", "spbm", "destroy", "destructive"],
        safety_level="destructive",
        requires_approval=True,
        llm_instructions=STORAGE_NETWORK_LLM_INSTRUCTIONS["vmware.composite.storage_policy.delete"],
        preview=_storage_policy_delete_preview,
        governed_subops=_storage_policy._SUB_OPS_STORAGE_POLICY_DELETE,
    ),
)

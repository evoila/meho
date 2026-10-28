# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the HCX inventory reads for a migration (the bodies live in ``typed_reads``).

``hcx.inventory.list`` (in ``read_ops``) returns the resource containers.
These three reads go one level down: ``hcx.inventory.compute.list`` returns
the vCenter instances with their datacenters and clusters,
``hcx.inventory.datastore.list`` the datastores of a cluster, and
``hcx.inventory.network.list`` the networks of a site. The paths come from
VMware's own Terraform provider for HCX. All are ``safe`` and need no
approval.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx.op_types import (
    GROUP_INVENTORY,
    NO_PARAMS_SCHEMA,
    HcxTypedOp,
    list_response_schema,
    string_schema,
)

__all__ = ["HCX_INVENTORY_OPS"]

_INVENTORY_TAGS: Final[tuple[str, ...]] = ("read-only", "hcx", "inventory", "migration")

_ENTITY_FIELDS: Final[dict[str, Any]] = {
    "entity_id": string_schema("Vendor id of the object."),
    "name": string_schema("Name of the object."),
    "entityType": string_schema("Vendor type of the object."),
}

_COMPUTE_LIST = HcxTypedOp(
    op_id="hcx.inventory.compute.list",
    handler_attr="inventory_compute_list",
    summary="List the vCenter instances HCX knows with their datacenters and clusters.",
    description=(
        "Returns the compute inventory as a flat list: one row per vCenter instance, "
        "datacenter and cluster, with the id of its parent and its depth in the tree. "
        "Use it to find the cluster ids for the datastore read and for a migration."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema=list_response_schema(
        "One row per vCenter instance, datacenter or cluster.",
        {
            **_ENTITY_FIELDS,
            "vcenter_instanceId": string_schema("Id of the vCenter instance the row belongs to."),
            "parent_id": string_schema("entity_id of the parent row; empty for a vCenter row."),
            "depth": {
                "type": "integer",
                "description": "0 for a vCenter instance, 1 for a datacenter, 2 for a cluster.",
            },
        },
    ),
    group_key=GROUP_INVENTORY,
    tags=_INVENTORY_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when planning a migration, to see the vCenter instances, datacenters and "
            "clusters HCX can move workloads from and to."
        ),
        "output_shape": (
            "{items: [{entity_id, name, entityType, vcenter_instanceId, parent_id, depth}]}: "
            "one flat row per object, parents before children. A long list is behind a "
            "result handle; read it with result_query."
        ),
        "next_step": (
            "Call hcx.inventory.datastore.list with the vcenter_instanceId and the entity_id "
            "of a cluster for its datastores, and hcx.inventory.network.list for the "
            "networks of the site."
        ),
    },
)

_DATASTORE_LIST = HcxTypedOp(
    op_id="hcx.inventory.datastore.list",
    handler_attr="inventory_datastore_list",
    summary="List the datastores of one or more clusters of a vCenter instance HCX knows.",
    description=(
        "Returns one row per datastore that the given clusters of a vCenter instance can "
        "use: id, name and type. Use it to pick the target datastore of a migration."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "vcenter_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Id of the vCenter instance (vcenter_instanceId in hcx.inventory.compute.list)."
                ),
            },
            "cluster_ids": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
                "description": (
                    "Ids of the clusters (entity_id of the depth-2 rows in "
                    "hcx.inventory.compute.list) whose datastores to list."
                ),
            },
        },
        "required": ["vcenter_id", "cluster_ids"],
        "additionalProperties": False,
        "examples": [{"vcenter_id": "<vcenter-id>", "cluster_ids": ["<cluster-id>"]}],
    },
    response_schema=list_response_schema(
        "One row per datastore.",
        {
            "id": string_schema("Vendor id of the datastore."),
            "name": string_schema("Name of the datastore."),
            "entity_type": string_schema("Vendor type of the datastore."),
        },
    ),
    group_key=GROUP_INVENTORY,
    tags=_INVENTORY_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when planning a migration, after hcx.inventory.compute.list, to see "
            "which datastores the target cluster can use."
        ),
        "output_shape": (
            "{items: [{id, name, entity_type}]}: one flat row per datastore. A long list "
            "is behind a result handle; read it with result_query."
        ),
        "next_step": (
            "Use id as the target datastore of a migration. An empty list means the "
            "clusters have no datastore HCX can use; check the cluster ids."
        ),
    },
)

_NETWORK_LIST = HcxTypedOp(
    op_id="hcx.inventory.network.list",
    handler_attr="inventory_network_list",
    summary="List the networks of one site HCX knows, for a migration or a network extension.",
    description=(
        "Returns one row per network (port group or segment) of one site: id, name and "
        "type. Use it to pick the target network of a migration or a network to extend."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "endpoint_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Id of the site (endpointId in hcx.inventory.list or "
                    "hcx.site_pairing.list; local_endpoint_id in hcx.about)."
                ),
            },
        },
        "required": ["endpoint_id"],
        "additionalProperties": False,
        "examples": [{"endpoint_id": "<endpoint-id>"}],
    },
    response_schema=list_response_schema("One row per network.", _ENTITY_FIELDS),
    group_key=GROUP_INVENTORY,
    tags=_INVENTORY_TAGS,
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when planning a migration or a network extension, to see the networks "
            "of the local or of a paired site."
        ),
        "output_shape": (
            "{items: [{entity_id, name, entityType}]}: one flat row per network. A long "
            "list is behind a result handle; read it with result_query."
        ),
        "next_step": (
            "Use entity_id as the target network of a migration. For the paired site, "
            "take endpointId from hcx.site_pairing.list."
        ),
    },
)

HCX_INVENTORY_OPS: Final[tuple[HcxTypedOp, ...]] = (_COMPUTE_LIST, _DATASTORE_LIST, _NETWORK_LIST)

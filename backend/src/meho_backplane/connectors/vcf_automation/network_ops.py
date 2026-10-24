# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA ops for a tenant project's own network (tenant plane).

* ``vcfa.tenant.fabric_network.list`` -- safe: ``GET /iaas/api/fabric-networks-vsphere``
* ``vcfa.tenant.fabric_compute.list`` -- safe: ``GET /iaas/api/fabric-computes``
* ``vcfa.tenant.region.list`` -- safe: ``GET /iaas/api/regions``
* ``vcfa.tenant.zone.list`` -- safe: ``GET /iaas/api/zones``
* ``vcfa.tenant.fabric_network.update`` -- caution, approval: ``GET`` then ``PATCH``
  ``/iaas/api/fabric-networks-vsphere/{id}``
* ``vcfa.tenant.network_profile.create`` -- caution, approval: ``GET`` then ``POST``
  ``/iaas/api/network-profiles``

The region and zone reads give the ids the network profile and the project
quota (``vcfa.tenant.project.create``) need. Handlers live in
:mod:`._network`. Field names come from the pinned ``vra-iaas.json``
(``FabricNetworkVsphere``, ``FabricCompute``, ``Region``, ``Zone``,
``NetworkProfileSpecification``). Same :class:`~.typed_ops.VcfaTypedOp` shape
and import-time plane check as the other VCFA ops.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._op_schemas import (
    field,
    guidance,
    iaas_page,
    list_of,
    obj,
)
from meho_backplane.connectors.vcf_automation._paths import (
    TENANT_FABRIC_COMPUTES_PATH,
    TENANT_FABRIC_NETWORK_PATH,
    TENANT_FABRIC_NETWORKS_PATH,
    TENANT_NETWORK_PROFILES_PATH,
    TENANT_REGIONS_PATH,
    TENANT_ZONES_PATH,
)
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = ["VCFA_NETWORK_OPS"]

_NEW_OBJECT_DELAY = (
    "VCF Automation reads new vCenter objects on its own schedule (2 to 12 minutes "
    "were measured). If a new port group or cluster is missing, wait a few minutes "
    "and read again."
)

# ---------------------------------------------------------------------------
# Shared schema fragments
# ---------------------------------------------------------------------------

_PAGING: dict[str, Any] = {
    "$top": {"type": "integer", "minimum": 1, "description": "Optional page size."},
    "$skip": {"type": "integer", "minimum": 0, "description": "Optional number of items to skip."},
}
_FILTER: dict[str, Any] = {
    "$filter": {
        "type": "string",
        "minLength": 1,
        "description": "Optional OData filter, for example \"name eq '<name>'\".",
    },
}

_TAG_FIELDS: dict[str, Any] = {
    "key": field("string", "The tag key."),
    "value": field("string", "The tag value; may be empty."),
}
_TAG_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "minLength": 1, "description": "The tag key, e.g. 'project'."},
        "value": {"type": "string", "description": "The tag value, e.g. '<project-name>'."},
    },
    "required": ["key"],
    "additionalProperties": False,
}


def _tags_param(description: str) -> dict[str, Any]:
    return {"type": "array", "items": _TAG_ITEM, "description": description}


_COMMON_FIELDS: dict[str, Any] = {
    "id": field("string", "The id in VCF Automation."),
    "name": field("string", "The name, as read from vCenter."),
    "externalId": field("string", "The vCenter id (managed object reference)."),
    "externalRegionId": field("string", "The vCenter datacenter id of the region."),
    "tags": list_of("The tags.", _TAG_FIELDS),
    "orgId": field("string", "The id of the org."),
}

_FABRIC_NETWORK_FIELDS: dict[str, Any] = {
    **_COMMON_FIELDS,
    "cidr": field("string", "The IPv4 network in CIDR form (address/prefix length)."),
    "defaultGateway": field("string", "The IPv4 default gateway."),
    "dnsServerAddresses": {
        "type": "array",
        "description": "The DNS server addresses.",
        "items": {"type": "string"},
    },
    "domain": field("string", "The DNS domain."),
    "isDefault": field("boolean", "True when this is the default network of the zone."),
    "cloudAccountIds": {
        "type": "array",
        "description": "The ids of the cloud accounts (vCenters) that have this network.",
        "items": {"type": "string"},
    },
}

_FABRIC_COMPUTE_FIELDS: dict[str, Any] = {
    **_COMMON_FIELDS,
    "type": field("string", "The kind, for example Cluster or Host."),
    "externalZoneId": field("string", "The vCenter id of the zone."),
    "powerState": field("string", "The power state."),
    "lifecycleState": field("string", "The lifecycle state."),
}

_REGION_FIELDS: dict[str, Any] = {
    "id": field("string", "The region id; a network profile needs it as region_id."),
    "name": field("string", "The region name (in vSphere, the datacenter)."),
    "externalRegionId": field("string", "The vCenter datacenter id."),
    "cloudAccountId": field("string", "The id of the cloud account (vCenter)."),
    "orgId": field("string", "The id of the org."),
}

_ZONE_FIELDS: dict[str, Any] = {
    "id": field("string", "The cloud zone id; a project quota needs it as zone_id."),
    "name": field("string", "The cloud zone name."),
    "description": field("string", "The cloud zone description."),
    "externalRegionId": field("string", "The vCenter datacenter id of the zone's region."),
    "cloudAccountId": field("string", "The id of the cloud account (vCenter)."),
    "placementPolicy": field("string", "How the zone picks a host or cluster."),
    "tags": list_of("The zone tags.", _TAG_FIELDS),
}


def _list_op(
    *,
    op_id: str,
    handler_attr: str,
    path: str,
    what: str,
    fields: dict[str, Any],
    filterable: bool,
    description: str,
    output: str,
    next_step: str,
) -> VcfaTypedOp:
    properties = {**(_FILTER if filterable else {}), **_PAGING}
    return VcfaTypedOp(
        op_id=op_id,
        handler_attr=handler_attr,
        plane="tenant",
        path=path,
        summary=f"List the {what} VCF Automation knows in the tenant org (tenant plane).",
        description=description,
        parameter_schema={
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
            "examples": [{"$filter": "name eq '<name>'"} if filterable else {"$top": 25}],
        },
        response_schema=iaas_page(what, fields),
        group_key="vcfa-tenant-reads",
        tags=("read-only", "vcfa", "tenant", "network"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions=guidance(
            when_to_call=f"Call on the tenant plane to list the {what}.",
            output_shape=f"{{content: [{output}], numberOfElements, totalElements}}.",
            next_step=next_step,
        ),
    )


_FABRIC_NETWORK_LIST = _list_op(
    op_id="vcfa.tenant.fabric_network.list",
    handler_attr="tenant_fabric_network_list",
    path=TENANT_FABRIC_NETWORKS_PATH,
    what="vSphere networks",
    fields=_FABRIC_NETWORK_FIELDS,
    filterable=True,
    description=(
        "Lists the vSphere networks (port groups) VCF Automation has read from vCenter, "
        "with GET /iaas/api/fabric-networks-vsphere. " + _NEW_OBJECT_DELAY
    ),
    output="FabricNetworkVsphere: id, name, cidr, defaultGateway, dnsServerAddresses, domain, tags",
    next_step=(
        "Set the address settings and a tag with vcfa.tenant.fabric_network.update, then "
        "put the network in a profile with vcfa.tenant.network_profile.create. " + _NEW_OBJECT_DELAY
    ),
)

_FABRIC_COMPUTE_LIST = _list_op(
    op_id="vcfa.tenant.fabric_compute.list",
    handler_attr="tenant_fabric_compute_list",
    path=TENANT_FABRIC_COMPUTES_PATH,
    what="vSphere computes",
    fields=_FABRIC_COMPUTE_FIELDS,
    filterable=True,
    description=(
        "Lists the clusters and hosts VCF Automation has read from vCenter, with GET "
        "/iaas/api/fabric-computes. " + _NEW_OBJECT_DELAY
    ),
    output="FabricCompute: id, name, type, externalRegionId, externalZoneId, tags",
    next_step="Find the cloud zone that holds a compute with vcfa.tenant.zone.list.",
)

_REGION_LIST = _list_op(
    op_id="vcfa.tenant.region.list",
    handler_attr="tenant_region_list",
    path=TENANT_REGIONS_PATH,
    what="regions",
    fields=_REGION_FIELDS,
    filterable=False,
    description=(
        "Lists the tenant org's regions (in vSphere, datacenters) with GET "
        "/iaas/api/regions. A network profile needs a region id."
    ),
    output="Region: id, name, externalRegionId, cloudAccountId",
    next_step="Pass a region id as region_id to vcfa.tenant.network_profile.create.",
)

_ZONE_LIST = _list_op(
    op_id="vcfa.tenant.zone.list",
    handler_attr="tenant_zone_list",
    path=TENANT_ZONES_PATH,
    what="cloud zones",
    fields=_ZONE_FIELDS,
    filterable=False,
    description=(
        "Lists the tenant org's cloud zones with GET /iaas/api/zones. A project quota "
        "names a cloud zone by its id."
    ),
    output="Zone: id, name, externalRegionId, cloudAccountId, placementPolicy, tags",
    next_step="Pass a zone id as zones[].zone_id to vcfa.tenant.project.create.",
)

_FABRIC_NETWORK_UPDATE = VcfaTypedOp(
    op_id="vcfa.tenant.fabric_network.update",
    handler_attr="tenant_fabric_network_update",
    plane="tenant",
    path=TENANT_FABRIC_NETWORK_PATH,
    summary="Set the address settings and tags of one vSphere network (tenant plane).",
    description=(
        "Reads the network, then sends PATCH /iaas/api/fabric-networks-vsphere/{id} with "
        "only the values that differ. add_tags is added to the tags the network has; no "
        "tag is removed. Safe to run twice: same values answer 'unchanged'."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": "The network id (from vcfa.tenant.fabric_network.list).",
            },
            "cidr": {
                "type": "string",
                "minLength": 1,
                "description": "The IPv4 network in CIDR form (address/prefix length).",
            },
            "default_gateway": {
                "type": "string",
                "minLength": 1,
                "description": "The IPv4 default gateway.",
            },
            "dns_server_addresses": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "description": "One DNS server."},
                "description": "The DNS server addresses. Replaces the list the network has.",
            },
            "domain": {"type": "string", "minLength": 1, "description": "The DNS domain."},
            "add_tags": _tags_param(
                "Tags to add, for example {key: 'project', value: '<project-name>'}. "
                "A project rule can then keep its VMs on this network."
            ),
        },
        "required": ["id"],
        "additionalProperties": False,
        "examples": [
            {
                "id": "<network-id>",
                "cidr": "<network-cidr>",
                "default_gateway": "<gateway-address>",
                "dns_server_addresses": ["<dns-server-address>"],
                "domain": "<dns-domain>",
                "add_tags": [{"key": "project", "value": "<project-name>"}],
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "What the op did: updated, unchanged or invalid_request.",
                enum=["updated", "unchanged", "invalid_request"],
            ),
            "fabric_network": {
                **obj(
                    "The network after the change; null on invalid_request.",
                    {
                        key: _FABRIC_NETWORK_FIELDS[key]
                        for key in (
                            "id",
                            "name",
                            "cidr",
                            "defaultGateway",
                            "dnsServerAddresses",
                            "domain",
                            "tags",
                        )
                    },
                ),
                "type": ["object", "null"],
            },
            "changed": {
                "type": "array",
                "description": "The IaaS fields that were sent, for example ['cidr', 'tags'].",
                "items": {"type": "string"},
            },
            "guidance": field(["string", "null"], "A short note on what to do next, or null."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-tenant-writes",
    tags=("write", "vcfa", "tenant", "network"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to give a vSphere network its address settings and a tag before a "
            "network profile and a project use it."
        ),
        preconditions=(
            "The network is listed by vcfa.tenant.fabric_network.list. "
            + _NEW_OBJECT_DELAY
            + " A person must approve the call."
        ),
        output_shape=(
            "{status: updated | unchanged | invalid_request, fabric_network: {id, name, "
            "cidr, defaultGateway, dnsServerAddresses, domain, tags}, changed, guidance}."
        ),
        next_step="vcfa.tenant.network_profile.create with this network id.",
    ),
)

_NETWORK_PROFILE_CREATE = VcfaTypedOp(
    op_id="vcfa.tenant.network_profile.create",
    handler_attr="tenant_network_profile_create",
    plane="tenant",
    path=TENANT_NETWORK_PROFILES_PATH,
    summary="Create a network profile in the tenant org (tenant plane); safe to run twice.",
    description=(
        "Creates a network profile with POST /iaas/api/network-profiles for one region, "
        "with the vSphere networks it may use. A profile with the same name answers "
        "'unchanged' and nothing is written."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "The profile name.",
            },
            "region_id": {
                "type": "string",
                "minLength": 1,
                "description": "The region id (from vcfa.tenant.region.list).",
            },
            "fabric_network_ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "description": "A network id."},
                "description": "The network ids (from vcfa.tenant.fabric_network.list).",
            },
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional profile description.",
            },
            "tags": _tags_param("Optional tags of the profile."),
        },
        "required": ["name", "region_id"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<profile-name>",
                "region_id": "<region-id>",
                "fabric_network_ids": ["<network-id>"],
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "What the op did: created, unchanged or invalid_request.",
                enum=["created", "unchanged", "invalid_request"],
            ),
            "network_profile": {
                **obj(
                    "The profile.",
                    {
                        "id": field(["string", "null"], "The profile id."),
                        "name": field("string", "The profile name."),
                        "externalRegionId": field(
                            ["string", "null"], "The vCenter datacenter id of its region."
                        ),
                    },
                ),
                "type": ["object", "null"],
            },
            "guidance": field(["string", "null"], "A short note on what to do next, or null."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-tenant-writes",
    tags=("write", "vcfa", "tenant", "network"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to make a network profile that offers a project's own network.",
        preconditions=(
            "The region and network ids come from vcfa.tenant.region.list and "
            "vcfa.tenant.fabric_network.list. A person must approve the call."
        ),
        output_shape=(
            "{status: created | unchanged, network_profile: {id, name, externalRegionId}, "
            "guidance}. 'unchanged' means a profile with this name exists; it was not changed."
        ),
        next_step=(
            "vcfa.tenant.project.create with network_constraints that match the "
            "network's tag, for example 'project:<project-name>'."
        ),
    ),
)

#: The network ops, reads first then the changes in build order.
VCFA_NETWORK_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _FABRIC_NETWORK_LIST,
    _FABRIC_COMPUTE_LIST,
    _REGION_LIST,
    _ZONE_LIST,
    _FABRIC_NETWORK_UPDATE,
    _NETWORK_PROFILE_CREATE,
)


validate_typed_ops(VCFA_NETWORK_OPS)

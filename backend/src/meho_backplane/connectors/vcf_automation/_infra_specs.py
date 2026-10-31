# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Specs of the cloud account, cloud zone and profile ops (see :mod:`.infra_ops`)."""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._infra_op_base import (
    COMMON_FIELDS,
    FILTER,
    PAGING,
    STATUS_NOTE,
    TAG_FIELDS,
    S,
    change,
    change_answer,
    id_param,
    list_answer,
    read,
    tags_param,
    text_param,
)
from meho_backplane.connectors.vcf_automation._infra_paths import (
    CLOUD_ACCOUNT_PATH,
    CLOUD_ACCOUNTS_PATH,
    CLOUD_ACCOUNTS_VSPHERE_PATH,
    FLAVOR_PROFILES_PATH,
    NETWORK_PROFILES_PATH,
    STORAGE_PROFILES_PATH,
    ZONES_PATH,
)
from meho_backplane.connectors.vcf_automation._op_schemas import field, list_of, obj
from meho_backplane.connectors.vcf_automation.provisioning_ops import _PASSWORD_SECRET_PROPS
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp

__all__ = [
    "cloud_account_create_op",
    "cloud_account_delete_op",
    "cloud_account_list_op",
    "flavor_profile_list_op",
    "flavor_profile_set_op",
    "network_profile_list_op",
    "storage_profile_list_op",
    "zone_list_op",
    "zone_set_op",
]

# ---------------------------------------------------------------------------
# Cloud accounts
# ---------------------------------------------------------------------------

_REGION_FIELDS: Final[dict[str, Any]] = {
    "id": field(S, "The region id; a cloud zone or a profile needs it as region_id."),
    "externalRegionId": field(S, "The vCenter datacenter id."),
    "name": field(S, "The region name."),
}
_ACCOUNT_FIELDS: Final[dict[str, Any]] = {
    "id": field("string", "The cloud account id."),
    "name": field("string", "The cloud account name."),
    "type": field(S, "The account type, for example vsphere or nsxt."),
    "description": field(S, "The description."),
    "hostName": field(S, "The host name of the vCenter or NSX Manager."),
    "healthy": field(["boolean", "null"], "False when VCF Automation cannot reach the system."),
    "inMaintenanceMode": field(["boolean", "null"], "True while the account is in maintenance."),
    "regions": list_of("The regions enabled for deployments.", _REGION_FIELDS),
    "tags": list_of("The tags.", TAG_FIELDS),
    "createdAt": field(S, "When the account was added."),
    "updatedAt": field(S, "When the account last changed."),
}


def cloud_account_list_op(prefix: str) -> VcfaTypedOp:
    return read(
        prefix,
        name="cloud_account.list",
        path=CLOUD_ACCOUNTS_PATH,
        summary="List the cloud accounts (vCenter, NSX and others) with their regions and state.",
        description=(
            "Reads GET /iaas/api/cloud-accounts: type, host name, enabled regions, healthy "
            "and maintenance state. Credentials are never in the answer."
        ),
        properties={**FILTER, **PAGING},
        example={"$filter": "name eq '<cloud-account-name>'"},
        response=list_answer("cloud accounts", _ACCOUNT_FIELDS),
        when="Call to see which vCenter and NSX systems VCF Automation uses, and if they work.",
        shape=(
            "{items: [{id, name, type, hostName, healthy, inMaintenanceMode, regions: [{id, "
            "externalRegionId, name}], tags}], totalElements}."
        ),
        next_step=(
            f"Use a region id with {prefix}.zone.set or {prefix}.flavor_profile.set; remove "
            f"an account with {prefix}.cloud_account.delete."
        ),
        tag="cloud-account",
    )


_REGION_ITEM: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One vCenter datacenter to use for deployments.",
    "properties": {
        "external_region_id": text_param(
            "The datacenter id in vCenter, for example 'Datacenter:<moref>'."
        ),
        "name": text_param("The datacenter name."),
    },
    "required": ["external_region_id", "name"],
    "additionalProperties": False,
}
_CLOUD_ACCOUNT_CREATE_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": ["vsphere", "nsx-t"],
            "description": "vsphere for a vCenter, nsx-t for an NSX Manager.",
        },
        "name": text_param("The cloud account name."),
        "description": text_param("Optional description.", max_length=1024),
        "host_name": text_param("The host name or address of the vCenter or NSX Manager."),
        "username": text_param("The user VCF Automation logs in with."),
        **_PASSWORD_SECRET_PROPS,
        "accept_self_signed_certificate": {
            "type": "boolean",
            "description": "True to trust the system's certificate as it is (default false).",
        },
        "regions": {
            "type": "array",
            "minItems": 1,
            "items": _REGION_ITEM,
            "description": "vCenter only: the datacenters to use (required for vsphere).",
        },
        "create_default_zones": {
            "type": "boolean",
            "description": "vCenter only: make one cloud zone per region.",
        },
        "dcid": text_param("Optional data collector id, when one is used."),
        "associated_cloud_account_ids": {
            "type": "array",
            "items": text_param("A cloud account id."),
            "description": "Optional ids of accounts to link (vCenter with NSX).",
        },
        "manager_mode": {
            "type": "boolean",
            "description": "NSX only: use the old Manager API.",
        },
        "is_global_manager": {
            "type": "boolean",
            "description": "NSX only: true for an NSX Global Manager.",
        },
        "tags": tags_param("Optional tags of the account."),
    },
    "required": ["type", "name", "host_name", "username", "password_secret_ref"],
    "oneOf": [
        {
            "properties": {"type": {"const": "vsphere", "description": "A vCenter."}},
            "required": ["regions"],
        },
        {"properties": {"type": {"const": "nsx-t", "description": "An NSX Manager."}}},
    ],
    "additionalProperties": False,
    "examples": [
        {
            "type": "vsphere",
            "name": "<cloud-account-name>",
            "host_name": "<vcenter-host>",
            "username": "<username>",
            "password_secret_ref": "<vault-path>",
            "regions": [{"external_region_id": "<datacenter-id>", "name": "<datacenter-name>"}],
        }
    ],
}


def cloud_account_create_op(prefix: str) -> VcfaTypedOp:
    return change(
        prefix,
        name="cloud_account.create",
        path=CLOUD_ACCOUNTS_VSPHERE_PATH,
        summary="Add a vCenter or NSX cloud account; the password comes from Vault.",
        description=(
            "Sends POST /iaas/api/cloud-accounts-vsphere or -nsx-t. The password is read "
            "from Vault at password_secret_ref when the call runs. An account with the "
            "same name answers 'unchanged'."
        ),
        schema=_CLOUD_ACCOUNT_CREATE_SCHEMA,
        response=change_answer(
            "cloud_account",
            "cloud account",
            {
                "id": field(S, "The account id; null until it is added."),
                "name": field("string", "The account name."),
                "type": field(S, "The account type."),
            },
            ["submitted", "unchanged", "invalid_request"],
            changed=False,
            extra={
                "request": {
                    **obj(
                        "The vendor request that adds the account.",
                        {
                            "id": field(S, "The request id."),
                            "status": field(S, "The request state."),
                            "progress": field(["integer", "null"], "Progress in percent."),
                            "message": field(S, "The vendor message."),
                        },
                    ),
                    "type": ["object", "null"],
                }
            },
        ),
        safety="dangerous",
        when="Call to connect VCF Automation to a vCenter or an NSX Manager.",
        preconditions=(
            "The password is stored in Vault at password_secret_ref (field 'password' unless "
            "password_secret_key says else); never pass it. For vCenter, the datacenter ids "
            "are known. A person must approve the call."
        ),
        shape=(
            "{status: submitted | unchanged | invalid_request, cloud_account: {id, name, "
            "type}, request: {id, status, progress, message}, guidance}. " + STATUS_NOTE
        ),
        next_step=f"Read {prefix}.cloud_account.list until the account shows up healthy.",
        tag="cloud-account",
        credential=True,
    )


def cloud_account_delete_op(prefix: str) -> VcfaTypedOp:
    return change(
        prefix,
        name="cloud_account.delete",
        path=CLOUD_ACCOUNT_PATH,
        summary="Remove a cloud account; its cloud zones and profiles stop working.",
        description=(
            "Sends DELETE /iaas/api/cloud-accounts/{id}. Needs a preview first: the preview "
            "lists the cloud zones and profiles of the account. An id that does not exist "
            "answers 'unchanged'."
        ),
        schema={
            "type": "object",
            "properties": {
                "id": id_param(f"The cloud account id (from {prefix}.cloud_account.list).")
            },
            "required": ["id"],
            "additionalProperties": False,
            "examples": [{"id": "<cloud-account-id>"}],
        },
        response={
            "type": "object",
            "description": "What the remove did.",
            "properties": {
                "status": field(
                    "string", "What happened.", enum=["submitted", "deleted", "unchanged"]
                ),
                "request": {
                    **obj(
                        "The vendor request that removes the account.",
                        {
                            "id": field(S, "The request id."),
                            "status": field(S, "The request state."),
                            "progress": field(["integer", "null"], "Progress in percent."),
                            "message": field(S, "The vendor message."),
                        },
                    ),
                    "type": ["object", "null"],
                },
                "guidance": field(S, "What to do next."),
            },
            "required": ["status", "guidance"],
            "additionalProperties": True,
        },
        safety="destructive",
        when="Call to stop using a vCenter or NSX system in VCF Automation.",
        preconditions=(
            "Call preview_operation with the same id first and read blast_radius: the cloud "
            "zones and profiles there stop working. Then call with the preview_hash. Only a "
            "person can run it, and a second person must approve."
        ),
        shape=(
            "{status: submitted | deleted | unchanged, request: {id, status, progress, "
            "message}, guidance}. 'unchanged' means no account has this id."
        ),
        next_step=f"Read {prefix}.cloud_account.list until the account is gone.",
        tag="cloud-account",
    )


# ---------------------------------------------------------------------------
# Cloud zones
# ---------------------------------------------------------------------------

_ZONE_FIELDS: Final[dict[str, Any]] = {
    **COMMON_FIELDS,
    "regionId": field(S, "The region id of the zone."),
    "placementPolicy": field(S, "How the zone picks a host or cluster."),
    "folder": field(S, "The vCenter folder for new machines."),
    "tagsToMatch": list_of(
        "The compute tags: the clusters or hosts with them are used.", TAG_FIELDS
    ),
    "tags": list_of("The tags the zone gives its machines.", TAG_FIELDS),
    "updatedAt": field(S, "When the zone last changed."),
}


def zone_list_op(prefix: str) -> VcfaTypedOp:
    return read(
        prefix,
        name="zone.list",
        path=ZONES_PATH,
        summary="List the cloud zones with region, placement policy, compute tags and tags.",
        description=(
            "Reads GET /iaas/api/zones as flat rows. A cloud zone is the part of a region "
            "(clusters or hosts) a project may deploy to. A project quota names a zone by id."
        ),
        properties=dict(PAGING),
        example={"$top": 50},
        response=list_answer("cloud zones", _ZONE_FIELDS),
        when="Call to find a cloud zone id, or to see where a project can deploy.",
        shape=(
            "{items: [{id, name, regionId, externalRegionId, cloudAccountId, placementPolicy, "
            "folder, tagsToMatch, tags}], totalElements}."
        ),
        next_step=f"Change a zone with {prefix}.zone.set.",
        tag="cloud-zone",
    )


def zone_set_op(prefix: str, zone_list: str) -> VcfaTypedOp:
    schema = {
        "type": "object",
        "properties": {
            "id": id_param(
                f"The zone id, to change a zone (from {zone_list}). Omit to create one."
            ),
            "name": text_param("The zone name; needed to create one."),
            "region_id": id_param(
                f"The region id (from {prefix}.cloud_account.list); needed to create."
            ),
            "description": {"type": "string", "maxLength": 1024, "description": "The description."},
            "placement_policy": {
                "type": "string",
                "enum": ["DEFAULT", "SPREAD", "BINPACK", "SPREAD_MEMORY"],
                "description": "How the zone picks a host or cluster for a new machine.",
            },
            "folder": {"type": "string", "maxLength": 1024, "description": "The vCenter folder."},
            "compute_ids": {
                "type": "array",
                "items": text_param("A compute id."),
                "description": "Optional clusters or hosts to put in the zone, by id.",
            },
            "tags_to_match": tags_param("The compute tags; replaces the list the zone has."),
            "tags": tags_param("The tags for the zone's machines; replaces the list."),
        },
        "anyOf": [{"required": ["id"]}, {"required": ["name", "region_id"]}],
        "additionalProperties": False,
        "examples": [
            {"name": "<zone-name>", "region_id": "<region-id>", "placement_policy": "DEFAULT"},
            {"id": "<zone-id>", "tags_to_match": [{"key": "<tag-key>", "value": "<tag-value>"}]},
        ],
    }
    return change(
        prefix,
        name="zone.set",
        path=ZONES_PATH,
        summary="Create a cloud zone, or change one; only the given fields change.",
        description=(
            "Without id: POST /iaas/api/zones (a zone with the same name in the region answers "
            "'unchanged'). With id: GET, then PATCH /iaas/api/zones/{id} with what differs."
        ),
        schema=schema,
        response=change_answer(
            "zone",
            "cloud zone",
            _ZONE_FIELDS,
            ["created", "updated", "unchanged", "invalid_request"],
        ),
        safety="dangerous",
        when="Call to make a new cloud zone, or to change its placement, folder, computes or tags.",
        preconditions=(
            "A change of compute tags or computes moves where new machines go. A person must "
            "approve the call."
        ),
        shape=(
            "{status: created | updated | unchanged | invalid_request, zone: {id, name, "
            "regionId, placementPolicy, folder, tagsToMatch, tags}, changed, guidance}. "
            + STATUS_NOTE
        ),
        next_step=f"Read {zone_list} to check the zone.",
        tag="cloud-zone",
    )


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

_FLAVOR_FIELDS: Final[dict[str, Any]] = {
    "key": field("string", "The flavor name templates use, for example 'small'."),
    "name": field(S, "The instance type name (public clouds)."),
    "cpuCount": field(["integer", "null"], "The CPU count (vSphere)."),
    "memoryInMB": field(["integer", "null"], "The memory in MB (vSphere)."),
}
_FLAVOR_PROFILE_FIELDS: Final[dict[str, Any]] = {
    **COMMON_FIELDS,
    "regionId": field(S, "The region id of the profile."),
    "flavors": list_of("The flavors of the profile.", _FLAVOR_FIELDS),
}


def flavor_profile_list_op(prefix: str) -> VcfaTypedOp:
    return read(
        prefix,
        name="flavor_profile.list",
        path=FLAVOR_PROFILES_PATH,
        summary="List the flavor profiles per region with their flavors (CPU and memory).",
        description=(
            "Reads GET /iaas/api/flavor-profiles. A flavor maps a name a template uses, for "
            "example 'small', to a size in one region."
        ),
        properties={},
        example={},
        response=list_answer("flavor profiles", _FLAVOR_PROFILE_FIELDS),
        when="Call to see which machine sizes each region offers.",
        shape=(
            "{items: [{id, name, regionId, externalRegionId, flavors: [{key, cpuCount, "
            "memoryInMB, name}]}], totalElements}."
        ),
        next_step=f"Add or change a flavor with {prefix}.flavor_profile.set.",
        tag="flavor-profile",
    )


def flavor_profile_set_op(prefix: str) -> VcfaTypedOp:
    flavor = {
        "type": "object",
        "description": "One flavor: CPU and memory for vSphere, or a cloud instance name.",
        "properties": {
            "cpu_count": {"type": "integer", "minimum": 1, "description": "The CPU count."},
            "memory_mb": {"type": "integer", "minimum": 1, "description": "The memory in MB."},
            "name": text_param("The instance type name, for public clouds."),
        },
        "additionalProperties": False,
    }
    schema = {
        "type": "object",
        "properties": {
            "id": id_param(f"The profile id, to change one (from {prefix}.flavor_profile.list)."),
            "name": text_param("The profile name; needed to create one."),
            "region_id": id_param(
                f"The region id (from {prefix}.cloud_account.list); needed to create."
            ),
            "description": {"type": "string", "maxLength": 1024, "description": "The description."},
            "flavors": {
                "type": "object",
                "description": "Flavors to add or replace, by flavor name; needed to create.",
                "additionalProperties": flavor,
            },
            "remove_flavors": {
                "type": "array",
                "items": text_param("A flavor name."),
                "description": "Flavor names to remove from the profile.",
            },
        },
        "anyOf": [{"required": ["id"]}, {"required": ["name", "region_id", "flavors"]}],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<profile-name>",
                "region_id": "<region-id>",
                "flavors": {"<flavor-name>": {"cpu_count": 2, "memory_mb": 4096}},
            }
        ],
    }
    return change(
        prefix,
        name="flavor_profile.set",
        path=FLAVOR_PROFILES_PATH,
        summary="Create a flavor profile, or add, change or remove flavors of one.",
        description=(
            "Without id: POST /iaas/api/flavor-profiles. With id: GET, then PATCH "
            "/iaas/api/flavor-profiles/{id} with the whole new flavor list. Unchanged values "
            "write nothing."
        ),
        schema=schema,
        response=change_answer(
            "flavor_profile",
            "flavor profile",
            {
                "id": field(S, "The profile id."),
                "name": field(S, "The profile name."),
                "externalRegionId": field(S, "The vCenter datacenter id."),
                "flavors": list_of("The flavors.", _FLAVOR_FIELDS),
            },
            ["created", "updated", "unchanged", "invalid_request"],
        ),
        safety="dangerous",
        when="Call to offer a new machine size in a region, or to change or remove one.",
        preconditions=(
            "A removed or smaller flavor changes what templates that name it deploy. A "
            "person must approve the call."
        ),
        shape=(
            "{status: created | updated | unchanged | invalid_request, flavor_profile: {id, "
            "name, externalRegionId, flavors}, changed, guidance}. " + STATUS_NOTE
        ),
        next_step=f"Read {prefix}.flavor_profile.list to check the flavors.",
        tag="flavor-profile",
    )


def network_profile_list_op(prefix: str) -> VcfaTypedOp:
    ip_range = {
        "id": field("string", "The IP range id."),
        "name": field(S, "The IP range name."),
        "startIPAddress": field(S, "The first address."),
        "endIPAddress": field(S, "The last address."),
        "ipVersion": field(S, "IPv4 or IPv6."),
        "fabricNetworkId": field(S, "The network the range belongs to."),
    }
    fields = {
        **COMMON_FIELDS,
        "isolationType": field(S, "NONE, SUBNET or SECURITY_GROUP."),
        "fabricNetworkIds": {
            "type": "array",
            "description": "The ids of the networks in the profile.",
            "items": {"type": "string"},
        },
        "ipRanges": list_of("The IP ranges of those networks.", ip_range),
        "tags": list_of("The tags.", TAG_FIELDS),
    }
    return read(
        prefix,
        name="network_profile.list",
        path=NETWORK_PROFILES_PATH,
        summary="List the network profiles with their networks and IP ranges.",
        description=(
            "Reads GET /iaas/api/network-profiles and GET /iaas/api/network-ip-ranges, and "
            "puts each IP range under the profile that holds its network."
        ),
        properties={**FILTER, **PAGING},
        example={"$filter": "name eq '<profile-name>'"},
        response=list_answer("network profiles", fields),
        when="Call to see which networks and address ranges a region gives new machines.",
        shape=(
            "{items: [{id, name, externalRegionId, isolationType, fabricNetworkIds, ipRanges: "
            "[{id, name, startIPAddress, endIPAddress, fabricNetworkId}], tags}], totalElements}."
        ),
        next_step="Match fabricNetworkIds against the networks a cloud template asks for.",
        tag="network-profile",
    )


def storage_profile_list_op(prefix: str) -> VcfaTypedOp:
    fields = {
        **COMMON_FIELDS,
        "defaultItem": field(["boolean", "null"], "True for the default profile of the region."),
        "supportsEncryption": field(["boolean", "null"], "True when disks can be encrypted."),
        "diskProperties": field(
            "object", "The disk settings, for example the datastore or policy."
        ),
        "tags": list_of("The tags a template asks for to pick this profile.", TAG_FIELDS),
    }
    return read(
        prefix,
        name="storage_profile.list",
        path=STORAGE_PROFILES_PATH,
        summary="List the storage profiles per region (default, encryption, disk settings, tags).",
        description=(
            "Reads GET /iaas/api/storage-profiles. A storage profile says where and how the "
            "disks of new machines are made in one region."
        ),
        properties={},
        example={},
        response=list_answer("storage profiles", fields),
        when="Call to see where new disks go in each region.",
        shape=(
            "{items: [{id, name, externalRegionId, cloudAccountId, defaultItem, "
            "supportsEncryption, diskProperties, tags}], totalElements}."
        ),
        next_step=f"Check the cloud account of a profile with {prefix}.cloud_account.list.",
        tag="storage-profile",
    )

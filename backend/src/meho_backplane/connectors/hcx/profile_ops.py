# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the HCX profile operations: ``hcx.profile.list`` and ``hcx.profile.set``.

``hcx.profile.list`` is ``safe``. ``hcx.profile.set`` creates or changes a
compute profile or a network profile; it is ``dangerous`` and waits for a
person's approval. The bodies live in
:mod:`meho_backplane.connectors.hcx.profile_writes`. Both operations are in
the group ``hcx-profiles``; its text is :data:`HCX_PROFILE_WHEN_TO_USE_BY_GROUP`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx.op_types import HcxTypedOp, string_schema

__all__ = ["GROUP_PROFILES", "HCX_PROFILE_OPS", "HCX_PROFILE_WHEN_TO_USE_BY_GROUP"]

GROUP_PROFILES: Final = "hcx-profiles"

#: Group text: which operations the group holds, in plain words.
HCX_PROFILE_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    GROUP_PROFILES: (
        "Use for the HCX compute profiles and network profiles that every service "
        "mesh is built from: list both kinds with their state (hcx.profile.list), "
        "and create or change one compute or network profile (hcx.profile.set), "
        "a change that waits for a person's approval."
    ),
}

#: The HCX services a compute profile can turn on. HCX 9.0 removed Disaster
#: Recovery and WAN optimisation, so they are left out.
SERVICE_NAMES: Final[list[str]] = [
    "INTERCONNECT",
    "VMOTION",
    "BULK_MIGRATION",
    "RAV",
    "NETWORK_EXTENSION",
]

#: What a network profile is used for in a compute profile.
NETWORK_TAGS: Final[list[str]] = ["management", "uplink", "vmotion", "replication"]

#: The network backing types a network profile can sit on.
BACKING_TYPES: Final[list[str]] = ["DistributedVirtualPortgroup", "NsxtSegment"]


def _inventory_ref(what: str) -> dict[str, Any]:
    """Schema of one vCenter object reference (a cluster, datastore or folder)."""
    return {
        "type": "object",
        "description": f"One {what}, as hcx.inventory.compute.list or hcx.profile.list names it.",
        "properties": {
            "id": {"type": "string", "minLength": 1, "description": f"Id of the {what}."},
            "name": {"type": "string", "description": f"Name of the {what}."},
            "type": {
                "type": "string",
                "description": f"Vendor type of the {what}, for example ClusterComputeResource.",
            },
            "cmp_id": {
                "type": "string",
                "description": "Id of the vCenter instance the object is in (vendor cmpId).",
            },
            "cmp_name": {"type": "string", "description": "Name of that vCenter instance."},
            "cmp_type": {
                "type": "string",
                "description": "Vendor type of that instance, usually VC.",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    }


_COMPUTE_PROFILE_FIELD: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "The compute profile fields to set. A new profile needs name, services, "
        "clusters, deployment and networks; a change sets only the fields to replace."
    ),
    "properties": {
        "name": {"type": "string", "minLength": 1, "description": "Name of the profile."},
        "services": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "description": "The HCX services the profile allows. INTERCONNECT is always needed.",
            "items": {"type": "string", "enum": SERVICE_NAMES, "description": "One HCX service."},
        },
        "clusters": {
            "type": "array",
            "minItems": 1,
            "description": "The service clusters: the clusters whose workloads HCX may move.",
            "items": _inventory_ref("cluster"),
        },
        "deployment": {
            "type": "object",
            "description": "Where HCX deploys its appliances.",
            "properties": {
                "clusters": {
                    "type": "array",
                    "minItems": 1,
                    "description": "The cluster for the appliances.",
                    "items": _inventory_ref("cluster"),
                },
                "datastores": {
                    "type": "array",
                    "minItems": 1,
                    "description": "The datastore for the appliances.",
                    "items": _inventory_ref("datastore"),
                },
                "folder": _inventory_ref("folder"),
                "cpu_reservation": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "CPU reservation of the appliances, in percent.",
                },
                "memory_reservation": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 100,
                    "description": "Memory reservation of the appliances, in percent.",
                },
            },
            "additionalProperties": False,
        },
        "networks": {
            "type": "array",
            "minItems": 1,
            "description": "The network profiles the appliances use, each with what it is for.",
            "items": {
                "type": "object",
                "description": "One network profile of the compute profile.",
                "properties": {
                    "id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Id of the network profile (profile_id in the list).",
                    },
                    "name": {"type": "string", "description": "Name of the network profile."},
                    "tags": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "description": "What the network is used for.",
                        "items": {
                            "type": "string",
                            "enum": NETWORK_TAGS,
                            "description": "One use of the network.",
                        },
                    },
                },
                "required": ["id", "tags"],
                "additionalProperties": False,
            },
        },
        "switches": {
            "type": "array",
            "minItems": 1,
            "description": "The switches network extension may use.",
            "items": {
                "type": "object",
                "description": "One distributed switch or NSX transport zone.",
                "properties": {
                    "id": {"type": "string", "minLength": 1, "description": "Id of the switch."},
                    "name": {"type": "string", "description": "Name of the switch."},
                    "type": {"type": "string", "description": "Vendor type of the switch."},
                    "cmp_id": {
                        "type": "string",
                        "description": "Id of the vCenter or NSX instance the switch is in.",
                    },
                    "max_mtu": {
                        "type": "integer",
                        "minimum": 68,
                        "description": "Largest MTU of the switch.",
                    },
                },
                "required": ["id"],
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
    "minProperties": 1,
}

_NETWORK_PROFILE_FIELD: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "The network profile fields to set. A new profile needs name, backing, "
        "prefix_length and ip_ranges; a change sets only the fields to replace."
    ),
    "properties": {
        "name": {"type": "string", "minLength": 1, "description": "Name of the profile."},
        "description": {"type": "string", "description": "Free text about the profile."},
        "backing": {
            "type": "object",
            "description": "The port group or segment the profile sits on.",
            "properties": {
                "id": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Id of the port group or segment (hcx.inventory.network.list).",
                },
                "name": {"type": "string", "description": "Name of the port group or segment."},
                "type": {
                    "type": "string",
                    "enum": BACKING_TYPES,
                    "description": "Kind of network: a distributed port group or an NSX segment.",
                },
                "vcenter_instance_uuid": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Id of the vCenter instance (inventory vcenter_instanceId).",
                },
            },
            "required": ["id", "type", "vcenter_instance_uuid"],
            "additionalProperties": False,
        },
        "mtu": {
            "type": "integer",
            "minimum": 68,
            "maximum": 9000,
            "description": "MTU of the network.",
        },
        "prefix_length": {
            "type": "integer",
            "minimum": 1,
            "maximum": 32,
            "description": "Prefix length of the subnet, for example 24.",
        },
        "gateway": {"type": "string", "description": "Gateway address of the subnet."},
        "primary_dns": {"type": "string", "description": "First DNS server."},
        "secondary_dns": {"type": "string", "description": "Second DNS server."},
        "dns_suffix": {"type": "string", "description": "DNS suffix of the subnet."},
        "ip_ranges": {
            "type": "array",
            "minItems": 1,
            "description": "The address ranges HCX may give to its appliances.",
            "items": {
                "type": "object",
                "description": "One address range.",
                "properties": {
                    "start_address": {
                        "type": "string",
                        "minLength": 1,
                        "description": "First address of the range.",
                    },
                    "end_address": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Last address of the range.",
                    },
                },
                "required": ["start_address", "end_address"],
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
    "minProperties": 1,
}

_PROFILE_ROW: Final[dict[str, Any]] = {
    "profile_type": {
        "type": "string",
        "enum": ["compute", "network"],
        "description": "compute or network.",
    },
    "profile_id": string_schema("Id of the profile (computeProfileId or objectId)."),
    "name": string_schema("Name of the profile."),
    "state": string_schema("Vendor state of the profile."),
    "services": {
        "type": ["array", "null"],
        "description": "Compute profile: the services it allows ({name}).",
        "items": {"type": "object", "properties": {"name": string_schema("Service name.")}},
    },
    "networks": {
        "type": ["array", "null"],
        "description": "Compute profile: its network profiles with their tags.",
        "items": {"type": "object", "properties": {"id": string_schema("Network profile id.")}},
    },
    "last_task_id": string_schema("Compute profile: id of its last interconnect task."),
    "last_task_status": string_schema("Compute profile: state of that task."),
    "mtu": {"type": ["integer", "null"], "description": "Network profile: MTU."},
    "backings": {
        "type": ["array", "null"],
        "description": "Network profile: the port group or segment it sits on.",
        "items": {
            "type": "object",
            "properties": {"backingName": string_schema("Name of the port group or segment.")},
        },
    },
    "ipScopes": {
        "type": ["array", "null"],
        "description": "Network profile: prefix length, gateway, DNS and address ranges.",
        "items": {
            "type": "object",
            "properties": {"gateway": string_schema("Gateway address.")},
        },
    },
    "last_job_id": string_schema("Network profile: id of its last job."),
}

_PROFILE_LIST = HcxTypedOp(
    op_id="hcx.profile.list",
    handler_attr="profile_list",
    summary="List the HCX compute profiles and network profiles with their state.",
    description=(
        "Returns one flat row per profile. A compute profile row has its services, "
        "clusters, deployment place, network profiles and last task. A network profile "
        "row has its backing, MTU, subnet and address ranges. kind picks one kind."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["compute", "network"],
                "description": "Only this kind of profile. Default: both.",
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"kind": "network"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "One row per profile, compute profiles first.",
                "items": {"type": "object", "properties": _PROFILE_ROW},
            }
        },
    },
    group_key=GROUP_PROFILES,
    tags=("read-only", "hcx", "compute-profile", "network-profile"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call before a service mesh is built or changed, to see which compute and "
            "network profiles exist, or to find the ids hcx.profile.set and "
            "hcx.service_mesh.create need."
        ),
        "output_shape": (
            "{items: [{profile_type: compute|network, profile_id, name, state, ...vendor "
            "fields}]}. Compute rows add last_task_id and last_task_status; network rows add "
            "last_job_id. A long list is behind a result handle; read it with result_query."
        ),
        "next_step": (
            "Use profile_id with hcx.profile.set to change a profile, or a compute "
            "profile_id with hcx.service_mesh.create."
        ),
    },
)

_PROFILE_SET = HcxTypedOp(
    op_id="hcx.profile.set",
    handler_attr="profile_set",
    summary="Create or change one HCX compute profile or network profile.",
    description=(
        "Set compute_profile or network_profile, not both. Without profile_id it creates "
        "the profile; with profile_id it reads the profile, replaces the fields you set "
        "and sends it back. Returns the accepted vendor task or job."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "profile_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the profile to change. Leave out to create one.",
            },
            "compute_profile": _COMPUTE_PROFILE_FIELD,
            "network_profile": _NETWORK_PROFILE_FIELD,
        },
        "oneOf": [{"required": ["compute_profile"]}, {"required": ["network_profile"]}],
        "additionalProperties": False,
        "examples": [
            {
                "network_profile": {
                    "name": "<network-profile-name>",
                    "backing": {
                        "id": "<port-group-id>",
                        "type": "DistributedVirtualPortgroup",
                        "vcenter_instance_uuid": "<vcenter-instance-id>",
                    },
                    "mtu": 1500,
                    "prefix_length": 24,
                    "gateway": "<gateway-address>",
                    "ip_ranges": [
                        {"start_address": "<first-address>", "end_address": "<last-address>"}
                    ],
                }
            },
            {
                "profile_id": "<compute-profile-id>",
                "compute_profile": {"services": ["INTERCONNECT", "VMOTION", "BULK_MIGRATION"]},
            },
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted"],
                "description": "accepted: the Manager took the change; it may not be done yet.",
            },
            "kind": {
                "type": "string",
                "enum": ["compute", "network"],
                "description": "The kind of profile.",
            },
            "action": {
                "type": "string",
                "enum": ["create", "change"],
                "description": "create (no profile_id) or change.",
            },
            "profile_id": string_schema("Id of the profile."),
            "interconnect_task_id": string_schema(
                "Compute profile: id of the vendor task to follow with hcx.task.get."
            ),
            "job_id": string_schema("Network profile: id of the vendor job to follow."),
            "status_read": {"type": "string", "description": "The read that follows the work."},
            "warnings": {
                "type": "array",
                "description": "Warnings the Manager returned.",
                "items": {"type": "object", "properties": {"message": string_schema("Text.")}},
            },
            "errors": {
                "type": "array",
                "description": "Errors the Manager returned.",
                "items": {"type": "object", "properties": {"message": string_schema("Text.")}},
            },
        },
    },
    group_key=GROUP_PROFILES,
    tags=("write", "hcx", "compute-profile", "network-profile"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to prepare a site for a service mesh: first a network profile per "
            "network the appliances use (management, uplink, vMotion, replication), then "
            "a compute profile that names the clusters, the deployment place, the "
            "services and those network profiles. Also call it to change one."
        ),
        "preconditions": (
            "The vCenter objects exist (hcx.inventory.compute.list, "
            "hcx.inventory.datastore.list, hcx.inventory.network.list). A change of a "
            "profile that a service mesh uses needs a resync of that mesh afterwards. "
            "It waits for a person's approval."
        ),
        "output_shape": (
            "{status: accepted, kind, action, profile_id, interconnect_task_id, job_id, "
            "status_read, warnings, errors}. accepted means the Manager took the change, "
            "never that it is done."
        ),
        "next_step": (
            "For a compute profile, call status_read (hcx.task.get) with "
            "interconnect_task_id until it ends; for a network profile call hcx.job.get "
            "with job_id. Then check the profile with hcx.profile.list. After a lost reply "
            "do not create again: look for the name in hcx.profile.list first."
        ),
    },
)

HCX_PROFILE_OPS: Final[tuple[HcxTypedOp, ...]] = (_PROFILE_LIST, _PROFILE_SET)

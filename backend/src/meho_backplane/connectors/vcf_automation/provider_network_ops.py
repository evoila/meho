# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed VCFA ops for provider networking: IP spaces, gateways, regional networking.

==========================================  ===========  ========  ==========================
op_id                                       risk         approval  vendor job
==========================================  ===========  ========  ==========================
``vcfa.provider.network.list``              safe         no        the status read
``vcfa.provider.ip_space.set``              dangerous    yes       yes
``vcfa.provider.provider_gateway.set``      dangerous    yes       yes
``vcfa.provider.regional_networking.set``   dangerous    yes       yes
``vcfa.provider.network.delete``            destructive  yes       yes
==========================================  ===========  ========  ==========================

All five exist on every VCF Automation 9.x version. The networking paths sit
under ``/cloudapi/vcf/`` on 9.0 and ``/cloudapi/v1/`` on 9.1 and newer
(:func:`._provider_paths.provider_path`). Together with the two organization
settings ops (:mod:`.provider_org_settings_ops`) they form the
``vcfa-provider-networking`` group: :data:`VCFA_PROVIDER_NETWORK_OPS`.
Handlers: :mod:`._provider_network` (the read), :mod:`._provider_ip_space`,
:mod:`._provider_gateway`, :mod:`._provider_regional_networking` and
:mod:`._provider_network_delete`.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.vcf_automation._op_schemas import field, guidance, list_of, obj
from meho_backplane.connectors.vcf_automation._provider_network_schemas import (
    ASYNC_HINT,
    KIND_ENUM,
    ROW_FIELDS,
    START_RESPONSE,
    START_SCALARS,
    STATUS_READ,
    id_param,
    nullable,
)
from meho_backplane.connectors.vcf_automation._provider_paths import (
    PROVIDER_GATEWAYS_PATH,
    PROVIDER_IP_SPACES_PATH,
    PROVIDER_REGIONAL_NETWORKING_PATH,
    PROVIDER_REGIONAL_NETWORKING_SETTING_PATH,
    PROVIDER_TIER0_ROUTERS_PATH,
)
from meho_backplane.connectors.vcf_automation.provider_org_settings_ops import (
    ORG_PARAM,
    VCFA_PROVIDER_ORG_SETTINGS_OPS,
)
from meho_backplane.connectors.vcf_automation.typed_ops import VcfaTypedOp, validate_typed_ops

__all__ = ["VCFA_PROVIDER_NETWORK_OPS"]

_NETWORK_LIST = VcfaTypedOp(
    op_id="vcfa.provider.network.list",
    handler_attr="provider_network_list",
    plane="provider",
    path=PROVIDER_TIER0_ROUTERS_PATH,
    summary="List provider networking per region, or read the state of a networking job.",
    description=(
        "Reads one kind at a time: IP spaces, provider gateways, IP space links, regional "
        "networking, edge clusters, free Tier-0 gateways (by region or NSX manager), or an "
        "organization's networking settings. Each row maps the vendor status to a job state."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": KIND_ENUM,
                "description": "What to read. ip_space_associations are the IP space links.",
            },
            "region_id": id_param("Only objects of this region (and the Tier-0 list of it)."),
            "nsx_manager_id": id_param(
                "tier0_gateways only: the NSX manager, instead of a region."
            ),
            "org": ORG_PARAM,
            "org_id": id_param("regional_networking only: the organization id."),
            "provider_gateway_id": id_param(
                "ip_space_associations and regional_networking: only those of this gateway."
            ),
            "ip_space_id": id_param("ip_space_associations only: the links of this IP space."),
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "description": "Only the object with exactly this name.",
            },
            "id": id_param("Read one object by id. Not for tier0_gateways and org_networking."),
            "job_id": id_param(
                "The job id a start returned. MEHO then also reads the vendor job: its "
                "state, progress and error text. It comes back in the answer, also when the "
                "appliance does not answer."
            ),
        },
        "required": ["kind"],
        "allOf": [
            {
                "if": {"properties": {"kind": {"const": "tier0_gateways"}}},
                "then": {"anyOf": [{"required": ["region_id"]}, {"required": ["nsx_manager_id"]}]},
            },
            {
                "if": {"properties": {"kind": {"const": "org_networking"}}},
                "then": {"required": ["org"]},
            },
        ],
        "additionalProperties": False,
        "examples": [{"kind": "provider_gateways", "region_id": "<region-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "kind": field("string", "The kind that was read."),
            "reachable": field("boolean", "False when the appliance did not answer."),
            "checked_at": field("string", "When MEHO read, UTC (ISO 8601)."),
            "job_id": field(["string", "null"], "The job id that was given, echoed."),
            "state": field(
                ["string", "null"],
                "For a read of one object (id or name): its job state, not_found or "
                "ambiguous. not_reachable when the appliance did not answer; "
                "invalid_request for bad params. Null for a plain list.",
            ),
            "vendor_status": field(["string", "null"], "The vendor status of that one object."),
            "job_state": field(
                ["string", "null"],
                "With job_id: the state of the vendor job (queued, running, succeeded, "
                "failed, canceled, or unknown when MEHO could not read the job).",
            ),
            "progress": field(["integer", "null"], "With job_id: the job's progress in percent."),
            "error": field(
                ["string", "null"],
                "The vendor's error text of a failed job (short, secrets filtered out), or "
                "why the read failed when not reachable.",
            ),
            "count": field("integer", "The number of rows."),
            "items": list_of("The objects, one flat row each.", ROW_FIELDS),
            "job": nullable(
                obj(
                    "With job_id: the vendor job as read (GET /api/task/{id}).",
                    {
                        "id": field("string", "The job id."),
                        "read": field("boolean", "False when MEHO could not read the job."),
                        "state": field("string", "The job state, as job_state."),
                        "vendor_state": field(["string", "null"], "The vendor's task status."),
                        "progress": field(["integer", "null"], "Progress in percent."),
                        "error": field(["string", "null"], "The vendor's error text."),
                        "operation": field(["string", "null"], "The vendor's operation name."),
                        "started_at": field(["string", "null"], "When the job started."),
                        "ended_at": field(["string", "null"], "When the job ended."),
                        "why_not_read": field(["string", "null"], "Why the job was not read."),
                    },
                )
            ),
            "guidance": field(["string", "null"], "A short note on what to do next."),
        },
        "required": ["kind", "reachable", "state", "items"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-networking",
    tags=("read-only", "vcfa", "provider", "network"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions=guidance(
        when_to_call=(
            "Call to see the provider networking of a region before a change, to find the "
            "free Tier-0 gateways and edge clusters, or as the status read after "
            "vcfa.provider.ip_space.set, provider_gateway.set, regional_networking.set or "
            "network.delete: pass the status_read params they returned."
        ),
        output_shape=(
            "{kind, reachable, checked_at, job_id, state, vendor_status, job_state, progress, "
            "error, count, items: [flat rows], job, guidance}. With job_id, state is the state "
            "of the vendor job. state queued or running: read again later. succeeded: done. "
            "failed: error has the vendor's text. not_found after a delete: gone. "
            "not_reachable: the appliance did not answer; this is not a failure."
        ),
        next_step=(
            "Read again until state is succeeded or failed. If the login expired, MEHO logs "
            "in again by itself."
        ),
        result_scalars={
            "keys": [
                "kind",
                "reachable",
                "checked_at",
                "job_id",
                "state",
                "vendor_status",
                "job_state",
                "progress",
                "error",
                "count",
                "guidance",
            ]
        },
    ),
)

_IP_SPACE_SET = VcfaTypedOp(
    op_id="vcfa.provider.ip_space.set",
    handler_attr="provider_ip_space_set",
    plane="provider",
    path=PROVIDER_IP_SPACES_PATH,
    summary="Create or change an IP space of a region: its address ranges and default limits.",
    description=(
        "Creates an IP space (POST) or adds ranges and changes limits of one with the same "
        "name in the region (PUT). Give only ranges NSX does not have yet: VCF Automation "
        "makes a new NSX IP block even when one exists. A vendor job; a person must approve."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "The IP space name; with region_id it finds an existing one.",
            },
            "region_id": id_param("The region id (from vcfa.provider.region.list)."),
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "The description.",
            },
            "cidr_blocks": {
                "type": "array",
                "minItems": 1,
                "description": "The address ranges. On a change, ranges not listed stay.",
                "items": {
                    "type": "object",
                    "properties": {
                        "cidr": {
                            "type": "string",
                            "minLength": 1,
                            "description": "The range in CIDR form.",
                        },
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Optional name of the IP block.",
                        },
                    },
                    "required": ["cidr"],
                    "additionalProperties": False,
                },
            },
            "default_quota": {
                "type": "object",
                "description": "The default limits of each organization that uses the IP space.",
                "properties": {
                    "max_cidr_count": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "The most CIDRs an organization may take; -1 no limit.",
                    },
                    "max_ip_count": {
                        "type": "integer",
                        "minimum": -1,
                        "description": "The most single IPs an organization may take; -1 no limit.",
                    },
                    "max_subnet_size": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 32,
                        "description": "The largest subnet as a prefix length, for example 24.",
                    },
                },
                "additionalProperties": False,
            },
            "range_is_new_in_nsx": {
                "type": "boolean",
                "description": (
                    "True once the person confirmed that NSX has none of the new ranges. "
                    "Needed for any new range."
                ),
            },
        },
        "required": ["name", "region_id"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<ip-space-name>",
                "region_id": "<region-id>",
                "cidr_blocks": [{"cidr": "<cidr>"}],
                "default_quota": {"max_cidr_count": 5, "max_ip_count": 5, "max_subnet_size": 24},
                "range_is_new_in_nsx": True,
            }
        ],
    },
    response_schema=START_RESPONSE,
    group_key="vcfa-provider-networking",
    tags=("write", "vcfa", "provider", "network", "vendor-job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to give a region an IP space, or to add ranges to one.",
        preconditions=(
            "Ask the person to confirm that NSX has none of the new ranges, then pass "
            "range_is_new_in_nsx: true. A person must approve the call. " + ASYNC_HINT
        ),
        output_shape=(
            "{status: accepted | already_running | unchanged | invalid_request | "
            "precondition_failed, operation: create | update, object, job_id, status_read, "
            "guidance}. accepted means the job started, not that it is done."
        ),
        next_step=(
            "Call vcfa.provider.network.list with status_read.params until state is succeeded "
            "or failed. Then link the IP space to a gateway with "
            "vcfa.provider.provider_gateway.set."
        ),
        result_scalars=START_SCALARS,
    ),
)

_GATEWAY_SET = VcfaTypedOp(
    op_id="vcfa.provider.provider_gateway.set",
    handler_attr="provider_gateway_set",
    plane="provider",
    path=PROVIDER_GATEWAYS_PATH,
    summary="Create a provider gateway on a free Tier-0 gateway, and link IP spaces to it.",
    description=(
        "A new gateway: finds the free Tier-0 in the region and sends POST with the first IP "
        "spaces. An existing gateway with that name: links the IP spaces it lacks, one link "
        "each. A vendor job; a person must approve."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "The gateway name; with region_id it finds an existing one.",
            },
            "region_id": id_param("The region id (from vcfa.provider.region.list)."),
            "tier0": {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
                "description": (
                    "New gateway only: the free Tier-0 gateway, its display name or id "
                    "(vcfa.provider.network.list, kind tier0_gateways)."
                ),
            },
            "ip_space_ids": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "uniqueItems": True,
                "description": "The IP spaces to link (1 to 5 ids).",
                "items": id_param("An IP space id."),
            },
            "description": {
                "type": "string",
                "maxLength": 1024,
                "description": "New gateway only: the description.",
            },
            "inbound_remote_networks": {
                "type": "array",
                "maxItems": 10,
                "description": "9.1 and newer, new gateway only: the CIDRs it can reach.",
                "items": {"type": "string", "minLength": 1, "description": "A CIDR."},
            },
            "allow_advertising_private_ip_blocks": {
                "type": "boolean",
                "description": "9.1+, new gateway only: orgs may advertise private IP blocks.",
            },
            "nat": {
                "type": "object",
                "description": "9.1 and newer, new gateway only: outbound NAT; fixed later.",
                "properties": {
                    "enable_snat": {"type": "boolean", "description": "Turn outbound NAT on."},
                    "ip_space_id": id_param("The IP space for outbound NAT; needed when on."),
                    "logging": {"type": "boolean", "description": "Log the NAT rule."},
                },
                "additionalProperties": False,
            },
        },
        "required": ["name", "region_id", "ip_space_ids"],
        "additionalProperties": False,
        "examples": [
            {
                "name": "<gateway-name>",
                "region_id": "<region-id>",
                "tier0": "<tier0-name>",
                "ip_space_ids": ["<ip-space-id>"],
            }
        ],
    },
    response_schema=START_RESPONSE,
    group_key="vcfa-provider-networking",
    tags=("write", "vcfa", "provider", "network", "vendor-job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call=(
            "Call to give a region a provider gateway, or to link more IP spaces to one."
        ),
        preconditions=(
            "Find a free Tier-0 with vcfa.provider.network.list (kind tier0_gateways) and the "
            "IP spaces (kind ip_spaces; they must read REALIZED). VCF Automation 9.0 refuses "
            "inbound_remote_networks, allow_advertising_private_ip_blocks and nat; MEHO stops "
            "first. A person must approve the call. " + ASYNC_HINT
        ),
        output_shape=(
            "{status: accepted | already_running | unchanged | invalid_request | "
            "precondition_failed | failed_part_way, operation, object, job_id, steps, "
            "step_failed, steps_not_run, status_read, guidance}. failed_part_way lists the "
            "links that started, the one that failed with the vendor's error, and the rest."
        ),
        next_step=(
            "Read the gateway with status_read until it is succeeded, then give an "
            "organization networking with vcfa.provider.regional_networking.set."
        ),
        result_scalars=START_SCALARS,
    ),
)

_REGIONAL_SET = VcfaTypedOp(
    op_id="vcfa.provider.regional_networking.set",
    handler_attr="provider_regional_networking_set",
    plane="provider",
    path=PROVIDER_REGIONAL_NETWORKING_PATH,
    summary="Give an organization networking in a region via a provider gateway.",
    description=(
        "Sends POST /cloudapi/vcf/regionalNetworkingSettings (v1 on 9.1+) to link the "
        "organization to a provider gateway and an edge cluster. VCF Automation then makes "
        "its NSX project, transit gateway and default VPC. A vendor job; a person must approve."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "org": ORG_PARAM,
            "region_id": id_param("The region id (from vcfa.provider.region.list)."),
            "provider_gateway_id": id_param("The provider gateway id; it must read REALIZED."),
            "edge_cluster_id": id_param(
                "Optional edge cluster id (kind edge_clusters). Without it, VCF Automation "
                "uses the edge cluster of the gateway's Tier-0."
            ),
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "Optional name; VCF Automation makes one from org and region.",
            },
        },
        "required": ["org", "region_id", "provider_gateway_id"],
        "additionalProperties": False,
        "examples": [
            {
                "org": "<org-name>",
                "region_id": "<region-id>",
                "provider_gateway_id": "<gateway-id>",
            }
        ],
    },
    response_schema=START_RESPONSE,
    group_key="vcfa-provider-networking",
    tags=("write", "vcfa", "provider", "network", "vendor-job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to give an organization networking in a region.",
        preconditions=(
            "The organization needs a short name for logs (vcfa.provider.org.networking.set); "
            "without it VCF Automation answers 'Short Log Name is not set'. The gateway must "
            "read REALIZED. A person must approve the call. " + ASYNC_HINT
        ),
        output_shape=(
            "{status: accepted | already_running | unchanged | invalid_request | "
            "precondition_failed, operation, object, job_id, status_read, guidance}."
        ),
        next_step=("Read with status_read (kind regional_networking) until state is succeeded."),
        result_scalars=START_SCALARS,
    ),
)

_NETWORK_DELETE = VcfaTypedOp(
    op_id="vcfa.provider.network.delete",
    handler_attr="provider_network_delete",
    plane="provider",
    path=PROVIDER_REGIONAL_NETWORKING_SETTING_PATH,
    summary="Remove regional networking, a provider gateway, an IP space or one IP space link.",
    description=(
        "Sends DELETE for one object. Order: regional networking, then the gateway (its IP "
        "space links go with it), then the IP space. MEHO stops while something still uses "
        "the object. A vendor job. Needs a preview and a person's approval."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": [
                    "regional_networking",
                    "provider_gateway",
                    "ip_space",
                    "ip_space_association",
                ],
                "description": "What to remove. ip_space_association is one IP space link.",
            },
            "id": id_param("The object id (from vcfa.provider.network.list)."),
        },
        "required": ["kind", "id"],
        "additionalProperties": False,
        "examples": [{"kind": "regional_networking", "id": "<setting-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": field(
                "string",
                "accepted: the delete started (not done yet).",
                enum=[
                    "accepted",
                    "already_absent",
                    "already_running",
                    "invalid_request",
                    "precondition_failed",
                ],
            ),
            "kind": field("string", "The kind that was asked."),
            "object": nullable(obj("The object as read before the delete.", ROW_FIELDS)),
            "blockers": list_of(
                "On precondition_failed: what still uses the object.",
                {
                    "kind": field("string", "The kind of the blocking object."),
                    "id": field(["string", "null"], "Its id."),
                    "name": field(["string", "null"], "Its name."),
                },
            ),
            "job_id": field(["string", "null"], "The vendor task id from the 202 answer."),
            "status_read": nullable(STATUS_READ),
            "guidance": field(["string", "null"], "A short note on what to do next."),
        },
        "required": ["status", "guidance"],
        "additionalProperties": True,
    },
    group_key="vcfa-provider-networking",
    tags=("write", "delete", "vcfa", "provider", "network", "vendor-job"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions=guidance(
        when_to_call="Call to remove provider networking, one object at a time, in order.",
        preconditions=(
            "Call preview_operation first and pass its preview_hash to call_operation. The "
            "preview names the object, what goes with it (for regional networking: the "
            "organization's NSX project, transit gateway and default VPC) and that it cannot "
            "be undone. An agent can never run this op; a person runs it. " + ASYNC_HINT
        ),
        output_shape=(
            "{status: accepted | already_absent | already_running | invalid_request | "
            "precondition_failed, kind, object, blockers, job_id, status_read, guidance}."
        ),
        next_step=("Read with status_read until state is not_found (gone) or failed (it stays)."),
        result_scalars={"keys": ["status", "kind", "job_id", "guidance"]},
    ),
)

#: Every op of the ``vcfa-provider-networking`` group: the read first, then
#: the organization settings changes, the networking starts and the delete.
VCFA_PROVIDER_NETWORK_OPS: Final[tuple[VcfaTypedOp, ...]] = (
    _NETWORK_LIST,
    *VCFA_PROVIDER_ORG_SETTINGS_OPS,
    _IP_SPACE_SET,
    _GATEWAY_SET,
    _REGIONAL_SET,
    _NETWORK_DELETE,
)

validate_typed_ops(VCFA_PROVIDER_NETWORK_OPS)

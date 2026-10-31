# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the HCX network extension operations (the bodies live in ``network_extension``).

``hcx.network_extension.list`` is a ``safe`` read. ``extend``,
``unextend`` and ``mon.set`` are changes: ``dangerous`` and
``requires_approval``, so each waits until a person approves it.
:mod:`meho_backplane.connectors.hcx.typed_ops` adds them to
``HCX_TYPED_OPS`` and registers them at start-up.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx.op_types import (
    NO_PARAMS_SCHEMA,
    HcxTypedOp,
    list_response_schema,
    string_schema,
)

__all__ = [
    "GROUP_NETWORK_EXTENSION",
    "HCX_NETWORK_EXTENSION_OPS",
    "HCX_NETWORK_EXTENSION_WHEN_TO_USE_BY_GROUP",
]

GROUP_NETWORK_EXTENSION: Final = "hcx-network-extension"

HCX_NETWORK_EXTENSION_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    GROUP_NETWORK_EXTENSION: (
        "Use to stretch networks between two HCX sites. One read: the extended "
        "networks with their state (hcx.network_extension.list). Three changes, "
        "each waits for a person's approval: extend a network to the other site "
        "(hcx.network_extension.extend), unextend it after the move, and if asked "
        "connect the cloud network to its local router (hcx.network_extension.unextend), "
        "and turn Mobility Optimized Networking on or off and set its policy routes "
        "(hcx.network_extension.mon.set). Follow the started jobs with hcx.job.get."
    ),
}

_STATUS_READ_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": "The read that follows the job: hcx.job.get with job_id.",
}

_ASYNC_HINT = (
    "Start it in MEHO's async mode (async: true), so MEHO keeps the answer with the "
    "job id under a run handle even if the reply is lost."
)

_LIST = HcxTypedOp(
    op_id="hcx.network_extension.list",
    handler_attr="network_extension_list",
    summary="List the extended networks of this HCX Manager, with their state and features.",
    description=(
        "Returns one row per network extension: stretch id, state, job id, the source "
        "network, the destination router and site, the appliance, and whether Mobility "
        "Optimized Networking and egress optimization are on."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema=list_response_schema(
        "One row per network extension. The vendor fields stay; the main ones are flat.",
        {
            "stretch_id": string_schema("Id of the extension (stretchId)."),
            "state": string_schema("Vendor state of the extension (operationStatus.state)."),
            "job_id": string_schema("Id of the last vendor job, when the vendor gives one."),
            "source_network_id": string_schema("Id of the extended source network."),
            "source_network_name": string_schema("Name of the extended source network."),
            "source_network_type": string_schema("Vendor type of the source network."),
            "destination_network_name": string_schema("Name of the network at the other site."),
            "destination_gateway_id": string_schema("Id of the router at the other site."),
            "destination_endpoint_id": string_schema("Id of the other site."),
            "source_appliance_id": string_schema("Id of the network extension appliance."),
            "mobility_optimized_networking": {
                "type": ["boolean", "null"],
                "description": "True when Mobility Optimized Networking is on.",
            },
            "egress_optimization": {
                "type": ["boolean", "null"],
                "description": "True when egress optimization is on.",
            },
            "gateway": string_schema("Gateway address of the network."),
            "netmask": string_schema("Netmask of the network."),
        },
    ),
    group_key=GROUP_NETWORK_EXTENSION,
    tags=("read-only", "hcx", "network-extension"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which networks are extended to the other site and in what "
            "state, before an extend or unextend, and to check the result after one."
        ),
        "output_shape": (
            "{items: [{stretch_id, state, job_id, source_network_id, source_network_name, "
            "source_network_type, destination_network_name, destination_gateway_id, "
            "destination_endpoint_id, source_appliance_id, mobility_optimized_networking, "
            "egress_optimization, gateway, netmask, ...vendor fields}]}. A long list is "
            "behind a result handle."
        ),
        "next_step": (
            "Use stretch_id with hcx.network_extension.unextend or "
            "hcx.network_extension.mon.set. Follow job_id with hcx.job.get."
        ),
    },
)

_EXTEND = HcxTypedOp(
    op_id="hcx.network_extension.extend",
    handler_attr="network_extension_extend",
    summary="Extend a network to the other HCX site. Starts a vendor job and waits for approval.",
    description=(
        "Starts an HCX job that stretches one source network to the other site over a "
        "network extension appliance. Returns 'accepted' with the job id; the work is "
        "not done then. Starts nothing if the network is already extended."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "source_network": {
                "type": "object",
                "description": "The network to extend, as hcx.inventory.network.list returns it.",
                "properties": {
                    "network_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Id of the network (entity_id in the network list).",
                    },
                    "network_name": {
                        "type": "string",
                        "description": "Name of the network.",
                    },
                    "network_type": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Vendor type of the network (entityType in the network list), "
                            "for example a distributed port group or an NSX segment."
                        ),
                    },
                },
                "required": ["network_id", "network_type"],
                "additionalProperties": False,
            },
            "destination": {
                "type": "object",
                "description": "The other site and its resource container (vCenter instance).",
                "properties": {
                    "endpoint_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Id of the other site (hcx.site_pairing.list endpointId).",
                    },
                    "endpoint_name": {"type": "string", "description": "Name of the other site."},
                    "endpoint_type": {
                        "type": "string",
                        "description": "Vendor endpoint type of the other site.",
                    },
                    "resource_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Id of the resource container at the other site "
                            "(resourceId in hcx.inventory.list)."
                        ),
                    },
                    "resource_name": {
                        "type": "string",
                        "description": "Name of the resource container.",
                    },
                    "resource_type": {
                        "type": "string",
                        "description": "Vendor type of the resource container.",
                    },
                },
                "required": ["endpoint_id", "resource_id"],
                "additionalProperties": False,
            },
            "destination_gateway_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the router at the other site that gets the network.",
            },
            "source_appliance_id": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Id of the network extension appliance to use (applianceId of a "
                    "network extension row in hcx.appliance.list)."
                ),
            },
            "gateway": {
                "type": "string",
                "minLength": 1,
                "description": "Gateway address of the network, the same as at the source.",
            },
            "netmask": {
                "type": "string",
                "minLength": 1,
                "description": "Netmask of the network, for example in dotted form.",
            },
            "dns": {
                "type": "array",
                "description": "DNS server addresses for the network. Empty when not given.",
                "items": {"type": "string", "description": "One DNS server address."},
            },
            "vc_guid": {
                "type": "string",
                "description": "Id of the local vCenter instance, if the Manager has several.",
            },
            "egress_optimization": {
                "type": "boolean",
                "default": False,
                "description": "Turn egress optimization on. Default false.",
            },
            "mobility_optimized_networking": {
                "type": "boolean",
                "default": False,
                "description": "Turn Mobility Optimized Networking on at once. Default false.",
            },
        },
        "required": [
            "source_network",
            "destination",
            "destination_gateway_id",
            "source_appliance_id",
            "gateway",
            "netmask",
        ],
        "additionalProperties": False,
        "examples": [
            {
                "source_network": {
                    "network_id": "<network-id>",
                    "network_name": "<network-name>",
                    "network_type": "<network-type>",
                },
                "destination": {"endpoint_id": "<endpoint-id>", "resource_id": "<resource-id>"},
                "destination_gateway_id": "<router-id>",
                "source_appliance_id": "<appliance-id>",
                "gateway": "<gateway-address>",
                "netmask": "<netmask>",
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "already_extended"],
                "description": (
                    "accepted: HCX took the job, the work is not done yet. "
                    "already_extended: the network has an extension; nothing started."
                ),
            },
            "job_id": string_schema("Id of the vendor job; null when the vendor gives none."),
            "status_read": _STATUS_READ_SCHEMA,
            "network_id": {"type": "string", "description": "Id of the source network."},
            "stretch_id": string_schema("Id of the existing extension (already_extended only)."),
            "state": string_schema(
                "Vendor state of the existing extension (already_extended only)."
            ),
        },
    },
    group_key=GROUP_NETWORK_EXTENSION,
    tags=("hcx", "network-extension", "change", "vendor-job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to stretch a source network to the other site before VMs move, so they "
            "keep their addresses."
        ),
        "preconditions": (
            "A site pairing and a service mesh with the network extension service exist "
            "(hcx.site_pairing.list, hcx.service_mesh.list). Take the ids from "
            "hcx.inventory.network.list, hcx.inventory.list and hcx.appliance.list. "
            "A second person must approve the call. " + _ASYNC_HINT
        ),
        "output_shape": (
            "{status: accepted|already_extended, job_id, status_read: 'hcx.job.get', "
            "network_id, stretch_id, state}. accepted never means done."
        ),
        "next_step": (
            "Call hcx.job.get with job_id until outcome is succeeded or failed, then "
            "hcx.network_extension.list to see the new extension. If the reply was lost, "
            "do not start again: call hcx.network_extension.list first; a row with this "
            "source network means the job was started."
        ),
    },
)

_UNEXTEND = HcxTypedOp(
    op_id="hcx.network_extension.unextend",
    handler_attr="network_extension_unextend",
    summary="Unextend a network, and if asked connect the cloud network to its router. Vendor job.",
    description=(
        "Starts an HCX job that removes one network extension. With "
        "connect_to_local_router, the network at the other site is connected to its "
        "local router. Returns 'accepted' with the job id; the work is not done then."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "stretch_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the extension (stretch_id in hcx.network_extension.list).",
            },
            "connect_to_local_router": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Connect the network at the other site to its local router after the "
                    "unextend, so it stays routed there. Default false: it is left without "
                    "a router."
                ),
            },
        },
        "required": ["stretch_id"],
        "additionalProperties": False,
        "examples": [{"stretch_id": "<stretch-id>", "connect_to_local_router": True}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "already_running", "not_found"],
                "description": (
                    "accepted: HCX took the job, the work is not done yet. already_running: "
                    "work still runs on the extension; nothing started. not_found: no "
                    "extension has this id; nothing started."
                ),
            },
            "job_id": string_schema("Id of the vendor job; null when the vendor gives none."),
            "status_read": _STATUS_READ_SCHEMA,
            "stretch_id": {"type": "string", "description": "Id of the extension."},
            "state": string_schema("Vendor state of the extension before the call."),
        },
    },
    group_key=GROUP_NETWORK_EXTENSION,
    tags=("hcx", "network-extension", "change", "vendor-job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call after the VMs of an extended network have moved, to remove the stretch "
            "between the sites."
        ),
        "preconditions": (
            "This cuts the link between the two halves of the network. VMs left at the "
            "source lose contact with the moved VMs. Without connect_to_local_router the "
            "network at the other site has no router. A second person must approve the "
            "call. " + _ASYNC_HINT
        ),
        "output_shape": (
            "{status: accepted|already_running|not_found, job_id, status_read: "
            "'hcx.job.get', stretch_id, state}. accepted never means done."
        ),
        "next_step": (
            "Call hcx.job.get with job_id until outcome is succeeded or failed, then "
            "hcx.network_extension.list: the row is gone. If the reply was lost, do not "
            "start again: call hcx.network_extension.list first; no row, or a row in a "
            "running state, means the job was started."
        ),
    },
)

_CIDR_LIST: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string", "description": "One subnet in IP/prefix form."},
}

_MON_SET = HcxTypedOp(
    op_id="hcx.network_extension.mon.set",
    handler_attr="network_extension_mon_set",
    summary="Turn Mobility Optimized Networking on or off for an extension, and set policy routes.",
    description=(
        "Turns Mobility Optimized Networking on or off for one network extension, and "
        "sets the policy routes of the other site: which subnets go back through the "
        "source site. Runs the asked steps in order and stops at the first that fails."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "stretch_id": {
                "type": "string",
                "minLength": 1,
                "description": "Id of the extension (stretch_id in hcx.network_extension.list).",
            },
            "mobility_optimized_networking": {
                "type": "boolean",
                "description": "true turns Mobility Optimized Networking on, false turns it off.",
            },
            "policy_routes": {
                "type": "object",
                "description": (
                    "The policy routes to set. They replace the routes of the site; "
                    "send every subnet that must stay."
                ),
                "properties": {
                    "endpoint_id": {
                        "type": "string",
                        "description": (
                            "Id of the site the routes belong to. Default: the other site "
                            "of the extension (destination_endpoint_id)."
                        ),
                    },
                    "allow": {
                        **_CIDR_LIST,
                        "description": "Subnets whose traffic goes back through the source site.",
                    },
                    "deny": {
                        **_CIDR_LIST,
                        "description": "Subnets whose traffic leaves at the other site.",
                    },
                },
                "additionalProperties": False,
            },
        },
        "required": ["stretch_id"],
        "anyOf": [
            {"required": ["mobility_optimized_networking"]},
            {"required": ["policy_routes"]},
        ],
        "additionalProperties": False,
        "examples": [
            {
                "stretch_id": "<stretch-id>",
                "mobility_optimized_networking": True,
                "policy_routes": {"allow": ["<subnet>/<prefix>"], "deny": []},
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "partial", "failed"],
                "description": (
                    "accepted: HCX took every step. partial: some steps ran, one failed. "
                    "failed: the first step failed."
                ),
            },
            "stretch_id": {"type": "string", "description": "Id of the extension."},
            "steps_ran": {
                "type": "array",
                "description": "Steps HCX took, in order.",
                "items": {
                    "type": "string",
                    "enum": ["mobility_optimized_networking", "policy_routes"],
                },
            },
            "step_failed": {
                "type": ["object", "null"],
                "description": "The step that failed, with the vendor's error; null when none.",
                "properties": {
                    "step": {"type": "string", "description": "Name of the step."},
                    "error": {"type": "string", "description": "The vendor's error text."},
                },
            },
            "steps_not_run": {
                "type": "array",
                "description": "Steps that did not run because an earlier one failed.",
                "items": {"type": "string"},
            },
            "warnings": {
                "type": "array",
                "description": "Warnings HCX returned.",
                "items": {"type": "string"},
            },
        },
    },
    group_key=GROUP_NETWORK_EXTENSION,
    tags=("hcx", "network-extension", "change", "mobility-optimized-networking"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when moved VMs must route at the other site (turn it on), or route "
            "through the source again (turn it off), or to change which subnets go back "
            "through the source site."
        ),
        "preconditions": (
            "The extension exists (hcx.network_extension.list) and its router at the other "
            "site supports it. Changing routing can cut traffic of running VMs. A second "
            "person must approve the call."
        ),
        "output_shape": (
            "{status: accepted|partial|failed, stretch_id, steps_ran, step_failed: "
            "{step, error}|null, steps_not_run, warnings}."
        ),
        "next_step": (
            "Call hcx.network_extension.list and check mobility_optimized_networking and "
            "state. On partial, the steps in steps_ran stay done; nothing is undone. Fix "
            "the cause in step_failed and call again with only the steps that did not run."
        ),
    },
)

HCX_NETWORK_EXTENSION_OPS: Final[tuple[HcxTypedOp, ...]] = (_LIST, _EXTEND, _UNEXTEND, _MON_SET)

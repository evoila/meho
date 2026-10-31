# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schema pieces the provider networking ops share (:mod:`.provider_network_ops`)."""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_automation._op_schemas import field, list_of, obj

__all__ = [
    "ASYNC_HINT",
    "KIND_ENUM",
    "ROW_FIELDS",
    "START_RESPONSE",
    "START_SCALARS",
    "STATUS_READ",
    "id_param",
    "nullable",
]

ASYNC_HINT: Final = (
    "Use MEHO's async mode (async: true) for this start, so MEHO keeps the answer with the "
    "job id under a run handle. After a lost reply, do not start again: read the object "
    "with vcfa.provider.network.list first and decide from what it shows."
)


def id_param(what: str) -> dict[str, Any]:
    """A string id parameter."""
    return {"type": "string", "minLength": 1, "maxLength": 512, "description": what}


def nullable(schema: dict[str, Any]) -> dict[str, Any]:
    """*schema*, also allowing null."""
    return {**schema, "type": ["object", "null"]}


STATUS_READ = obj(
    "The read that follows the job: call this op with these params.",
    {
        "op_id": field("string", "Always vcfa.provider.network.list."),
        "params": obj(
            "The params of the status read.",
            {
                "kind": field("string", "The kind of object to read."),
                "name": field("string", "The object name, when known."),
                "region_id": field("string", "The region id, when known."),
                "org_id": field("string", "The organization id, when known."),
                "id": field("string", "The object id, when known."),
                "job_id": field("string", "The vendor task id, when known."),
                "provider_gateway_id": field("string", "The gateway id, for its IP space links."),
            },
        ),
    },
)

START_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": field(
            "string",
            "accepted: the job started (not done yet); already_running: a job runs on this "
            "object, nothing started; failed_part_way: a later step was refused.",
            enum=[
                "accepted",
                "already_running",
                "unchanged",
                "invalid_request",
                "precondition_failed",
                "failed_part_way",
            ],
        ),
        "operation": field(["string", "null"], "create, update or link_ip_spaces."),
        "object": nullable(
            obj(
                "The object the job works on.",
                {
                    "id": field(["string", "null"], "The object id; null for a new object."),
                    "name": field(["string", "null"], "The object name."),
                    "status": field(["string", "null"], "The vendor status, when read."),
                    "region_id": field(["string", "null"], "The region id."),
                },
            )
        ),
        "job_id": field(["string", "null"], "The vendor task id from the 202 answer."),
        "steps": list_of(
            "The steps that started, in order.",
            {
                "step": field("string", "The step name."),
                "status": field("string", "accepted"),
                "job_id": field(["string", "null"], "The vendor task id of the step."),
            },
        ),
        "step_failed": nullable(
            obj(
                "Only on failed_part_way: the step that failed.",
                {
                    "step": field("string", "The step name."),
                    "http_status": field("integer", "The vendor's HTTP status."),
                    "error": field("string", "The vendor's error text."),
                },
            )
        ),
        "steps_not_run": {
            "type": "array",
            "description": "Only on failed_part_way: the steps that did not run.",
            "items": {"type": "string"},
        },
        "status_read": nullable(STATUS_READ),
        "guidance": field(["string", "null"], "A short note on what to do next."),
    },
    "required": ["status", "guidance"],
    "additionalProperties": True,
}

START_SCALARS: Final = {"keys": ["status", "operation", "job_id", "guidance"]}

ROW_FIELDS: Final[dict[str, Any]] = {
    "id": field(["string", "null"], "The object id (a URN)."),
    "name": field(["string", "null"], "The object name (the display name for a Tier-0)."),
    "status": field(
        ["string", "null"],
        "The vendor status: PENDING, CONFIGURING, REALIZED, REALIZATION_FAILED or UNKNOWN.",
    ),
    "job_state": field(
        ["string", "null"], "The status as a job state: queued, running, succeeded, failed."
    ),
    "region_id": field(["string", "null"], "The region id."),
    "region_name": field(["string", "null"], "The region name."),
    "description": field(["string", "null"], "The description."),
    "cidrs": {
        "type": "array",
        "description": "IP spaces: the address ranges.",
        "items": {"type": "string"},
    },
    "max_cidr_count": field(["integer", "null"], "IP spaces: the default CIDR limit."),
    "max_ip_count": field(["integer", "null"], "IP spaces: the default single-IP limit."),
    "max_subnet_size": field(["integer", "null"], "IP spaces: the largest subnet prefix."),
    "backing_type": field(["string", "null"], "Gateways: NSX_TIER0 or NSX_VRF_TIER0."),
    "tier0_id": field(["string", "null"], "Gateways: the Tier-0 gateway id."),
    "tier0_name": field(["string", "null"], "Gateways: the Tier-0 gateway name."),
    "ip_space_id": field(["string", "null"], "Links: the IP space id."),
    "ip_space_name": field(["string", "null"], "Links: the IP space name."),
    "provider_gateway_id": field(["string", "null"], "Links, regional networking: the gateway."),
    "provider_gateway_name": field(["string", "null"], "Links, regional networking: its name."),
    "org_id": field(["string", "null"], "Regional networking, org networking: the org id."),
    "org_name": field(["string", "null"], "Regional networking, org networking: its name."),
    "edge_cluster_id": field(["string", "null"], "Regional networking: the edge cluster."),
    "edge_cluster_name": field(["string", "null"], "Regional networking: its name."),
    "health_status": field(["string", "null"], "Edge clusters: UP, DOWN or DEGRADED."),
    "node_count": field(["integer", "null"], "Edge clusters: the number of nodes."),
    "org_count": field(["integer", "null"], "Edge clusters: the organizations using it."),
    "vpc_count": field(["integer", "null"], "Edge clusters: the VPCs using it."),
    "parent_tier0_id": field(["string", "null"], "Tier-0: the parent, for a VRF."),
    "already_imported": field(["boolean", "null"], "Tier-0: true when a gateway uses it."),
    "is_enabled": field(["boolean", "null"], "Org networking: true when the org is on."),
    "networking_enabled": field(["boolean", "null"], "Org networking: true when it is on."),
    "org_name_for_logs": field(["string", "null"], "Org networking: the short log name."),
}

KIND_ENUM: Final = [
    "ip_spaces",
    "provider_gateways",
    "ip_space_associations",
    "regional_networking",
    "edge_clusters",
    "tier0_gateways",
    "org_networking",
]

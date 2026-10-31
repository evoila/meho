# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the request, environment, inventory sync, health and datacenter ops.

Seven typed ops of the ``fleet-rest`` connector (Aria Suite Lifecycle 8.x
and VCF 9.0 fleet management). The handlers are in :mod:`.lifecycle`;
:mod:`.typed_ops` adds these ops to ``FLEET_TYPED_OPS`` and holds the
group texts.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_fleet._typed_op import FleetTypedOp

__all__ = ["LIFECYCLE_OPS"]


def _field(kind: str | list[str], description: str, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "description": description, **extra}


_OUTCOMES: Final = ["queued", "running", "succeeded", "failed", "cancelled", "unknown"]

_REQUEST_ID_PARAM: Final = _field(
    "string", "The request id (vmid) from fleet.request.list or from a start answer."
)
_STATUS_READ_FIELD: Final = _field("string", "The status read to follow the request with.")

_REQUEST_ROW: Final[dict[str, dict[str, Any]]] = {
    "request_id": _field(["string", "null"], "The request id (vmid)."),
    "name": _field(["string", "null"], "The request name."),
    "type": _field(["string", "null"], "The request type, for example an inventory sync."),
    "reason": _field(["string", "null"], "Why the request ran, in the vendor's words."),
    "state": _field(["string", "null"], "The vendor's state, for example INPROGRESS or FAILED."),
    "outcome": _field(
        "string",
        "The state in plain words; not_reachable when the appliance did not answer.",
        enum=[*_OUTCOMES, "not_reachable"],
    ),
    "execution_status": _field(["string", "null"], "The vendor's execution status."),
    "error_cause": _field(
        ["string", "null"],
        "The vendor's error text, secret values replaced, cut at 2000 characters.",
    ),
    "created_by": _field(["string", "null"], "The user who started the request."),
    "last_updated_on": _field(["integer", "null"], "Last change, epoch milliseconds."),
}

_ERROR_CAUSE: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One error cause of a failed request.",
    "properties": {
        "event_id": _field(["string", "null"], "The event id of the failure."),
        "message_id": _field(["string", "null"], "The vendor's message id."),
        "message": _field(["string", "null"], "What to check or fix."),
        "exception_message": _field(["string", "null"], "The error the step raised."),
        "retry": _field(["boolean", "null"], "The vendor's retry flag."),
        "properties": _field(
            "object",
            "The inputs of the failed step; secret values are replaced.",
            additionalProperties=_field("string", "One input value."),
        ),
    },
}

_STEP: Final[dict[str, Any]] = {
    "type": "object",
    "description": "One step of the retry.",
    "properties": {
        "step": _field(
            "string",
            "The step name.",
            enum=["read_request", "read_error_causes", "retry"],
        ),
        "state": _field("string", "What happened.", enum=["done", "failed", "not_run"]),
        "error": _field(
            ["string", "null"],
            "The vendor's error text when the step failed; secret values replaced.",
        ),
    },
}


_REQUEST_LIST = FleetTypedOp(
    op_id="fleet.request.list",
    handler_attr="request_list",
    summary="List lifecycle requests with their state, newest first.",
    description=(
        "Lists the lifecycle requests (GET /lcm/request/api/v2/requests): name, type, "
        "state, a plain outcome and the error text. Filter by state or outcome. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "state": _field("string", "Only requests in this vendor state, for example FAILED."),
            "outcome": _field("string", "Only requests with this plain outcome.", enum=_OUTCOMES),
            "limit": _field(
                "integer", "Most rows to return, newest first.", minimum=1, maximum=1000
            ),
        },
        "additionalProperties": False,
        "examples": [{"outcome": "failed", "limit": 20}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "requests": {
                "type": "array",
                "description": "The requests, newest first.",
                "items": {"type": "object", "properties": _REQUEST_ROW},
            },
            "more_rows": _field("boolean", "True when more requests matched than limit."),
        },
    },
    group_key="fleet-requests",
    tags=("read-only", "fleet", "vcf", "requests"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see what the lifecycle appliance is doing or did: running, failed or "
            "finished requests (deployments, upgrades, inventory syncs, day-2 actions)."
        ),
        "output_shape": (
            "{requests: [{request_id, name, type, reason, state, outcome, execution_status, "
            "error_cause, created_by, last_updated_on}], more_rows}. A long list is behind a "
            "result handle; read it with result_query. The vendor gives no progress number."
        ),
        "next_step": (
            "Call fleet.request.get with a request_id for one request and its error causes. "
            "For a failed request, fix the cause, then fleet.request.retry."
        ),
        "result_scalars": {"keys": ["more_rows"]},
    },
)


_REQUEST_GET = FleetTypedOp(
    op_id="fleet.request.get",
    handler_attr="request_get",
    summary="Read one lifecycle request: state, outcome and the error causes of a failure.",
    description=(
        "Reads one request (GET /lcm/request/api/v2/requests/{requestId}) and, when it "
        "failed, its error causes; the first one is the failed step. Says not_reachable, "
        "not failed, when the appliance does not answer. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"request_id": _REQUEST_ID_PARAM},
        "required": ["request_id"],
        "additionalProperties": False,
        "examples": [{"request_id": "<request-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            **_REQUEST_ROW,
            "checked_at": _field("string", "When MEHO read the request, ISO 8601 UTC."),
            "error": _field(
                ["string", "null"], "Why the appliance was not reachable; null when it answered."
            ),
            "error_causes": {
                "type": "array",
                "description": "The error causes of a failed request; empty otherwise.",
                "items": _ERROR_CAUSE,
            },
            "failed_step": {**_ERROR_CAUSE, "type": ["object", "null"]},
        },
    },
    group_key="fleet-requests",
    tags=("read-only", "fleet", "vcf", "requests", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to follow a request until it ends: after fleet.product.inventory_sync, "
            "fleet.request.retry or any other start, or to see why a request failed."
        ),
        "output_shape": (
            "{request_id, name, type, reason, state, outcome, execution_status, error_cause, "
            "created_by, last_updated_on, checked_at, error, error_causes: [{event_id, "
            "message_id, message, exception_message, retry, properties}], failed_step}. "
            "outcome is queued, running, succeeded, failed, cancelled, unknown or "
            "not_reachable. Secret input values are replaced."
        ),
        "next_step": (
            "If outcome is queued or running, read again later. If not_reachable, the "
            "appliance did not answer (for example during its upgrade), on either of the two "
            "reads: keep the request id and read again later; state keeps what the first "
            "read said, when it worked. If failed, fix what "
            "failed_step.message says, then call fleet.request.retry."
        ),
        "result_scalars": {
            "keys": ["request_id", "state", "reason", "outcome", "checked_at", "error"]
        },
    },
)


_REQUEST_RETRY = FleetTypedOp(
    op_id="fleet.request.retry",
    handler_attr="request_retry",
    summary="Retry a failed lifecycle request, optionally with corrected inputs.",
    description=(
        "Reads the request and its error causes, then sends them back with retry set "
        "(PATCH /lcm/request/api/v2/requests/{requestId}/retry). Starts nothing when the "
        "request did not fail. Returns accepted, not done."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "request_id": _REQUEST_ID_PARAM,
            "properties": {
                "type": "object",
                "description": (
                    "Corrected inputs of the failed step, by the names in "
                    "fleet.request.get error_causes[].properties. A secret is a locker "
                    "reference (locker:password:<id>:<alias>), never the value."
                ),
                "additionalProperties": _field("string", "The new value of one input."),
            },
        },
        "required": ["request_id"],
        "additionalProperties": False,
        "examples": [{"request_id": "<request-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": _field(
                "string",
                "accepted: the appliance took the retry; not_started: nothing was sent; "
                "failed: the retry call failed.",
                enum=["accepted", "not_started", "failed"],
            ),
            "reason": _field(["string", "null"], "Why nothing started, or the vendor's error."),
            "request_id": _field("string", "The request id; the retry keeps it."),
            "state": _field(["string", "null"], "The vendor state before the retry."),
            "outcome": _field(
                "string",
                "The plain outcome before the retry; unknown when the request read failed.",
                enum=_OUTCOMES,
            ),
            "status_read": _STATUS_READ_FIELD,
            "retried_event_ids": {
                "type": "array",
                "description": "The event ids of the error causes sent back.",
                "items": _field(["string", "null"], "One event id."),
            },
            "steps": {
                "type": "array",
                "description": "The steps that ran, the one that failed, and those not run.",
                "items": _STEP,
            },
        },
    },
    group_key="fleet-requests",
    tags=("write", "fleet", "vcf", "requests", "retry"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to run a failed lifecycle request again after its cause is fixed, for "
            "example a wrong host name or a changed password reference."
        ),
        "preconditions": (
            "fleet.request.get shows outcome failed, and what failed_step.message names is "
            "fixed, on the system or through properties. A person must approve the call."
        ),
        "output_shape": (
            "{status, reason, request_id, state, outcome, status_read, retried_event_ids, "
            "steps: [{step, state, error}]}. accepted means the appliance took the retry, "
            "not that it worked. not_started means nothing was sent (the request did not "
            "fail, or has no error causes). failed means a step failed: steps says which "
            "ran, which failed and which did not run."
        ),
        "next_step": (
            "Follow the request with fleet.request.get until it ends. Use MEHO's async mode "
            "(async: true) for this call, so a lost reply keeps the answer under a run "
            "handle. After a lost reply, do not retry blindly: read the request first; if "
            "it is running again, the retry went through."
        ),
        "result_scalars": {
            "keys": ["status", "reason", "request_id", "state", "outcome", "status_read"]
        },
    },
)


_ENVIRONMENT_GET = FleetTypedOp(
    op_id="fleet.environment.get",
    handler_attr="environment_get",
    summary="Read one Fleet environment with its products, versions and nodes.",
    description=(
        "Reads one environment (GET /lcm/lcops/api/v2/environments/{environmentId}): its "
        "status, datacenter and products, each with version, nodes and properties. Secret "
        "values are replaced. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "environment_id": _field(
                "string", "The environment id from fleet.environment.list (environmentId)."
            ),
        },
        "required": ["environment_id"],
        "additionalProperties": False,
        "examples": [{"environment_id": "<environment-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "environmentId": _field("string", "The environment id."),
            "environmentName": _field("string", "The environment name."),
            "environmentStatus": _field("string", "The deployment state of the environment."),
            "status": _field("string", "The vendor status, for example COMPLETED."),
            "dataCenterName": _field("string", "The datacenter the environment is in."),
            "dataCenterVmid": _field("string", "The datacenter id."),
            "products": {
                "type": "array",
                "description": "The products in the environment.",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": _field("string", "The product id, for example vrops."),
                        "version": _field("string", "The product version."),
                        "nodes": {
                            "type": "array",
                            "description": "The appliance nodes of the product.",
                            "items": {"type": "object", "description": "One node."},
                        },
                        "properties": _field("object", "Product settings; secrets replaced."),
                    },
                },
            },
        },
        "additionalProperties": True,
    },
    group_key="fleet-inventory",
    tags=("read-only", "fleet", "vcf", "inventory"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call for the detail of one environment: which products and versions it holds "
            "and their nodes, for example before an inventory sync or an upgrade."
        ),
        "output_shape": (
            "The vendor's environment object: environmentId, environmentName, status, "
            "dataCenterName, products: [{id, version, nodes, properties}], with every "
            "secret-named value replaced by [REDACTED:param_name]."
        ),
        "next_step": (
            "Use a product id with fleet.product.inventory_sync to refresh what the "
            "appliance knows about that product."
        ),
    },
)


_INVENTORY_SYNC = FleetTypedOp(
    op_id="fleet.product.inventory_sync",
    handler_attr="product_inventory_sync",
    summary="Start an inventory sync of one product in an environment.",
    description=(
        "Starts an inventory sync (POST /lcm/lcops/api/v2/environments/{environmentId}/"
        "products/{productId}/inventory-sync) unless one already runs. Returns accepted "
        "with the request id, not done."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "environment_id": _field("string", "The environment id (environmentId)."),
            "product_id": _field(
                "string", "The product id in the environment, from fleet.environment.get."
            ),
        },
        "required": ["environment_id", "product_id"],
        "additionalProperties": False,
        "examples": [{"environment_id": "<environment-id>", "product_id": "<product-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": _field(
                "string",
                "accepted: a sync started; already_running: one ran, nothing started.",
                enum=["accepted", "already_running"],
            ),
            "request_id": _field(
                ["string", "null"], "The request id of the sync to follow with the status read."
            ),
            "environment_id": _field("string", "The environment id."),
            "product_id": _field("string", "The product id."),
            "status_read": _STATUS_READ_FIELD,
        },
    },
    group_key="fleet-inventory",
    tags=("write", "fleet", "vcf", "inventory", "sync"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when the appliance's view of a product is out of date, for example after "
            "nodes were added or removed or a password was changed outside the appliance."
        ),
        "preconditions": (
            "The environment and product exist (fleet.environment.get). A person must "
            "approve the call."
        ),
        "output_shape": (
            "{status, request_id, environment_id, product_id, status_read}. accepted means "
            "the appliance took the job, not that it finished. already_running gives the "
            "request id of the sync that runs. The running check is a help, not a lock."
        ),
        "next_step": (
            "Follow request_id with fleet.request.get until it ends. Use MEHO's async mode "
            "(async: true) for this call, so a lost reply keeps the request id under a run "
            "handle. After a lost reply, do not start again blindly: list running requests "
            "with fleet.request.list (outcome running) and look for the sync first."
        ),
    },
)


_HEALTH = FleetTypedOp(
    op_id="fleet.health",
    handler_attr="health",
    summary="Read the appliance health: version, disk use and the state of its services.",
    description=(
        "Reads GET /lcm/lcops/api/v2/settings/system-details (version, build, disk use) "
        "and GET /lcm/health/api/v2/status (state of each service). A part that fails is "
        "named in its error field. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "name": _field(["string", "null"], "The appliance name."),
            "vendor": _field(["string", "null"], "The vendor."),
            "version": _field(["string", "null"], "The appliance version."),
            "build_number": _field(["string", "integer", "null"], "The build number."),
            "patch_version": _field(["string", "null"], "The patch level."),
            "total_storage": _field(["string", "null"], "Disk size, for example 148G."),
            "used_storage": _field(["string", "null"], "Disk in use."),
            "available_storage": _field(["string", "null"], "Disk free."),
            "storage_percentage": _field(["string", "null"], "Disk use in percent."),
            "all_up": _field(
                ["boolean", "null"], "True when every service is UP; null when unknown."
            ),
            "services": {
                "type": "array",
                "description": "One row per service.",
                "items": {
                    "type": "object",
                    "properties": {
                        "service": _field("string", "The service, for example vrlcm-server."),
                        "state": _field("string", "The state, for example UP."),
                    },
                },
            },
            "system_details_error": _field(
                ["string", "null"], "Why the system details read failed; null when it worked."
            ),
            "health_error": _field(
                ["string", "null"], "Why the service state read failed; null when it worked."
            ),
        },
    },
    group_key="fleet-about",
    tags=("read-only", "fleet", "vcf", "health", "probe"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check that the lifecycle appliance works: its version, its free disk "
            "and whether its services (server, database, bootstrap) are up."
        ),
        "output_shape": (
            "{name, vendor, version, build_number, patch_version, total_storage, "
            "used_storage, available_storage, storage_percentage, all_up, services: "
            "[{service, state}], system_details_error, health_error}."
        ),
        "next_step": (
            "If all_up is false, report the services that are not UP. If an error field is "
            "set, that part failed; the other part may still answer."
        ),
    },
)


_DATACENTER_LIST = FleetTypedOp(
    op_id="fleet.datacenter.list",
    handler_attr="datacenter_list",
    summary="List the Fleet datacenters and the vCenters registered in each.",
    description=(
        "Lists the datacenters (GET /lcm/lcops/api/v2/datacenters) and the vCenters of "
        "each (GET .../datacenters/{dataCenterVmid}/vcenters), one row per vCenter. No "
        "password is returned. Read-only."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "description": (
                    "One row per vCenter; a datacenter with no vCenter has one row with "
                    "empty vCenter fields."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "datacenter_id": _field(["string", "null"], "The datacenter id."),
                        "datacenter_name": _field(["string", "null"], "The datacenter name."),
                        "primary_location": _field(["string", "null"], "The location text."),
                        "vcenter_name": _field(["string", "null"], "The vCenter name."),
                        "vcenter_host": _field(["string", "null"], "The vCenter host name."),
                        "vcenter_username": _field(["string", "null"], "The user it signs in as."),
                        "used_as": _field(["string", "null"], "The vCenter role."),
                        "data_collection_status": _field(
                            ["string", "null"], "State of the last data collection."
                        ),
                    },
                },
            },
        },
    },
    group_key="fleet-inventory",
    tags=("read-only", "fleet", "vcf", "inventory", "datacenter"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see where the appliance can deploy: its datacenters and the vCenters "
            "registered in each."
        ),
        "output_shape": (
            "{rows: [{datacenter_id, datacenter_name, primary_location, vcenter_name, "
            "vcenter_host, vcenter_username, used_as, data_collection_status}]}. A long list "
            "is behind a result handle."
        ),
        "next_step": "Use a datacenter with fleet.environment.list to see its environments.",
    },
)


#: The ops of this module, in the order :mod:`.typed_ops` registers them.
LIFECYCLE_OPS: Final[tuple[FleetTypedOp, ...]] = (
    _HEALTH,
    _ENVIRONMENT_GET,
    _DATACENTER_LIST,
    _INVENTORY_SYNC,
    _REQUEST_LIST,
    _REQUEST_GET,
    _REQUEST_RETRY,
)

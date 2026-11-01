# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the six catalog and request operations tell the agent.

:func:`catalog_op_specs` builds the metadata for one id prefix. The vRA 8
connector calls it with ``vcfa.vra8`` and the VCF Automation connector with
``vcfa.tenant``; each wraps the specs into its own typed-op class and
group. The handlers are in :mod:`.vra_catalog`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

from meho_backplane.connectors.vmware_shared.vra_catalog import (
    CATALOG_ITEM_PATH,
    CATALOG_ITEM_REQUEST_PATH,
    CATALOG_ITEMS_PATH,
    REQUEST_EVENT_LOGS_PATH,
    REQUEST_EVENTS_PATH,
    REQUEST_PATH,
)

__all__ = ["REQUEST_STATUSES", "VraCatalogOpSpec", "catalog_op_specs", "catalog_when_to_use"]

#: The request states the vendor documents (``Request.status``).
REQUEST_STATUSES: Final[tuple[str, ...]] = (
    "CREATED",
    "PENDING",
    "INITIALIZATION",
    "CHECKING_APPROVAL",
    "APPROVAL_PENDING",
    "USER_INTERACTION_PENDING",
    "INPROGRESS",
    "COMPLETION",
    "APPROVAL_REJECTED",
    "ABORTED",
    "SUCCESSFUL",
    "FAILED",
)


@dataclass(frozen=True)
class VraCatalogOpSpec:
    """One operation, without the connector's group and plane."""

    op_id: str
    handler_attr: str
    path: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any]
    tags: tuple[str, ...]
    safety_level: Literal["safe", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any]


def _f(type_: str | list[str], description: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "description": description, **extra}


_S = ["string", "null"]


def _obj(description: str, properties: dict[str, Any], *, nullable: bool = False) -> dict[str, Any]:
    return {
        "type": ["object", "null"] if nullable else "object",
        "description": description,
        "properties": properties,
        "additionalProperties": True,
    }


def _rows(description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": {"type": "object", "properties": properties, "additionalProperties": True},
    }


def _id_param(description: str) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "description": description}


def _item_fields() -> dict[str, Any]:
    return {
        "id": _f("string", "The catalog item id."),
        "name": _f("string", "The catalog item name."),
        "description": _f(_S, "The item description."),
        "typeId": _f(_S, "The item type, for example com.vmw.blueprint."),
        "typeName": _f(_S, "The item type name."),
        "sourceId": _f(_S, "The content source id."),
        "sourceName": _f(_S, "The content source name."),
        "projectIds": {
            "type": "array",
            "description": "The projects that may request the item.",
            "items": {"type": "string", "description": "A project id."},
        },
        "formId": _f(_S, "The custom request form id, when there is one."),
        "bulkRequestLimit": _f(["integer", "null"], "How many deployments one request may make."),
        "createdAt": _f(_S, "When the item was made."),
        "lastUpdatedAt": _f(_S, "When the item last changed."),
    }


def _list_spec(prefix: str) -> VraCatalogOpSpec:
    return VraCatalogOpSpec(
        op_id=f"{prefix}.catalog.item.list",
        handler_attr="vra_catalog_item_list",
        path=CATALOG_ITEMS_PATH,
        summary="List the catalog items a project can request.",
        description=(
            "Reads GET /catalog/api/items: the catalog items you may request, as flat "
            "rows. Give project_id for the items of one project. Paged with $top and $skip."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "project_id": _id_param(
                    "Optional project id: only items this project can request."
                ),
                "search": _id_param("Optional text to find in the item name or description."),
                "$top": {"type": "integer", "minimum": 1, "description": "Optional page size."},
                "$skip": {"type": "integer", "minimum": 0, "description": "Optional rows to skip."},
            },
            "additionalProperties": False,
            "examples": [{"project_id": "<project-id>", "$top": 50}],
        },
        response_schema=_obj(
            "The catalog items.",
            {
                "items": _rows("One row per catalog item.", _item_fields()),
                "totalElements": _f(["integer", "null"], "How many items match, on all pages."),
            },
        ),
        tags=("read-only", "vcfa", "catalog", "catalog-item"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to find a catalog item to request, or to see what a project offers."
            ),
            "output_shape": (
                "{items: [{id, name, description, typeId, typeName, sourceName, projectIds, "
                "formId, bulkRequestLimit, createdAt, lastUpdatedAt}], totalElements}. A long "
                "list comes back as a link to the stored result; read it with result_query."
            ),
            "next_step": f"Read the request form of one item with {prefix}.catalog.item.get.",
        },
    )


def _get_spec(prefix: str) -> VraCatalogOpSpec:
    input_row = {
        "name": _f("string", "The input name; the key in inputs or secret_inputs."),
        "type": _f(["string", "array", "null"], "The input type, for example string or integer."),
        "title": _f(_S, "The label of the input on the form."),
        "description": _f(_S, "What the input is for."),
        "default": _f(
            ["string", "number", "integer", "boolean", "object", "array", "null"],
            "The default value; a marker for a secret input.",
        ),
        "required": _f("boolean", "True when the request must give the input."),
        "enum": _f(["array", "null"], "The allowed values, when they are fixed."),
        "secret": _f("boolean", "True for a secret input; give it in secret_inputs."),
    }
    return VraCatalogOpSpec(
        op_id=f"{prefix}.catalog.item.get",
        handler_attr="vra_catalog_item_get",
        path=CATALOG_ITEM_PATH,
        summary="Read one catalog item with its request form (inputs, types, defaults).",
        description=(
            "Reads GET /catalog/api/items/{id}, and with version_id also the form of that "
            "version. Returns the item, one row per form input and the form schema. Secret "
            "defaults are hidden."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": _id_param(f"The catalog item id (from {prefix}.catalog.item.list)."),
                "version_id": _id_param(
                    "Optional item version, for example a released template version."
                ),
            },
            "required": ["id"],
            "additionalProperties": False,
            "examples": [{"id": "<catalog-item-id>"}],
        },
        response_schema=_obj(
            "One catalog item and its request form.",
            {
                "item": _obj("The catalog item.", _item_fields()),
                "version": _obj(
                    "The version read; null without version_id.",
                    {
                        "id": _f(_S, "The version id."),
                        "description": _f(_S, "The version description."),
                        "createdAt": _f(_S, "When the version was made."),
                        "formId": _f(_S, "The request form id of the version."),
                    },
                    nullable=True,
                ),
                "inputs": _rows("One row per form input.", input_row),
                "schema": _f(
                    ["object", "null"], "The form as a JSON schema, secret defaults hidden."
                ),
            },
        ),
        tags=("read-only", "vcfa", "catalog", "catalog-item"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": "Call before a request, to learn which inputs the item needs.",
            "output_shape": (
                "{item: {id, name, typeId, projectIds, ...}, version, inputs: [{name, type, "
                "title, description, default, required, enum, secret}], schema}."
            ),
            "next_step": (
                f"Request the item with {prefix}.catalog.item.request. Put each secret input "
                "in secret_inputs as a Vault reference."
            ),
        },
    )


def _request_params(prefix: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": _id_param(f"The catalog item id (from {prefix}.catalog.item.list)."),
            "project_id": _id_param(
                "The project to deploy into; it must be able to request the item."
            ),
            "deployment_name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "description": "The name of the new deployment.",
            },
            "version": _id_param("Optional item version; omit for the current one."),
            "inputs": {
                "type": "object",
                "description": (
                    "The form inputs by name. No secrets, also none inside an object or "
                    "a list: use secret_inputs."
                ),
                "additionalProperties": {"description": "The value of one input."},
            },
            "secret_inputs": {
                "type": "object",
                "description": (
                    "Secret inputs by top-level name. Each value is a Vault reference "
                    "'<vault-path>#<field>', never the secret."
                ),
                "additionalProperties": {
                    "type": "string",
                    "pattern": "^[^#]+#[^#]+$",
                    "description": "A Vault reference '<vault-path>#<field>'.",
                },
            },
            "reason": {
                "type": "string",
                "maxLength": 1024,
                "description": "Optional reason, kept with the request.",
            },
        },
        "required": ["id", "project_id", "deployment_name"],
        "additionalProperties": False,
        "examples": [
            {
                "id": "<catalog-item-id>",
                "project_id": "<project-id>",
                "deployment_name": "<deployment-name>",
                "inputs": {"<input-name>": "<value>"},
                "secret_inputs": {"<secret-input-name>": "<vault-path>#<field>"},
            }
        ],
    }


def _request_spec(prefix: str) -> VraCatalogOpSpec:
    return VraCatalogOpSpec(
        op_id=f"{prefix}.catalog.item.request",
        handler_attr="vra_catalog_item_request",
        path=CATALOG_ITEM_REQUEST_PATH,
        summary="Request a catalog item into a project; this builds a new deployment.",
        description=(
            "Sends POST /catalog/api/items/{id}/request with the project, the deployment "
            "name and the inputs. The build runs in the background. When the project has a "
            "deployment with this name, nothing is requested."
        ),
        parameter_schema=_request_params(prefix),
        response_schema=_obj(
            "What the op did.",
            {
                "status": _f(
                    "string",
                    "submitted, unchanged or invalid_request.",
                    enum=["submitted", "unchanged", "invalid_request"],
                ),
                "deployment": _obj(
                    "The new or the existing deployment; null on invalid_request.",
                    {
                        "id": _f(_S, "The deployment id."),
                        "name": _f(_S, "The deployment name."),
                        "projectId": _f(_S, "The project id."),
                    },
                    nullable=True,
                ),
                "request": _obj(
                    "The request that builds the deployment; null when not read.",
                    {
                        "id": _f(_S, "The request id."),
                        "name": _f(_S, "The request label."),
                        "status": _f(_S, "The request state."),
                    },
                    nullable=True,
                ),
                "guidance": _f(_S, "A short note on what to do next."),
            },
        ),
        tags=("write", "vcfa", "catalog", "catalog-item", "request", "deployment"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_call": "Call to deploy a catalog item into a project, for example a new VM.",
            "preconditions": (
                f"Read the form with {prefix}.catalog.item.get and give every required input. "
                "Store each secret input in Vault first and pass its place in secret_inputs. "
                "A second person must approve the call; an agent needs a grant for it."
            ),
            "output_shape": (
                "{status: submitted | unchanged | invalid_request, deployment: {id, name, "
                "projectId}, request: {id, name, status}, guidance}."
            ),
            "next_step": (
                f"Read {prefix}.request.get with request.id until status is SUCCESSFUL, FAILED "
                f"or ABORTED. On FAILED read {prefix}.request.events."
            ),
        },
    )


def _request_id_param(prefix: str) -> dict[str, Any]:
    return _id_param(
        f"The request id (request.id from {prefix}.catalog.item.request, "
        "or the lastRequest id of a deployment)."
    )


def _state_spec(prefix: str) -> VraCatalogOpSpec:
    statuses = ", ".join(REQUEST_STATUSES)
    return VraCatalogOpSpec(
        op_id=f"{prefix}.request.get",
        handler_attr="vra_request_get",
        path=REQUEST_PATH,
        summary="Read the state of one request (a deployment or a day-2 action).",
        description=(
            "Reads GET /deployment/api/requests/{requestId}: its state, the tasks done of "
            "all tasks, the deployment and the inputs and outputs, with secret values hidden."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"request_id": _request_id_param(prefix)},
            "required": ["request_id"],
            "additionalProperties": False,
            "examples": [{"request_id": "<request-id>"}],
        },
        response_schema=_obj(
            "One request.",
            {
                "id": _f(_S, "The request id."),
                "name": _f(_S, "The request label, for example Create or Delete."),
                "status": _f(_S, f"The request state: {statuses}.", enum=[*REQUEST_STATUSES, None]),
                "details": _f(_S, "Longer text; on a failure it often says why."),
                "deploymentId": _f(_S, "The deployment the request is for."),
                "catalogItemId": _f(_S, "The catalog item, as 'id:version'."),
                "requestedBy": _f(_S, "Who asked."),
                "completedTasks": _f(["integer", "null"], "Tasks done."),
                "totalTasks": _f(["integer", "null"], "All tasks."),
                "createdAt": _f(_S, "When the request was made."),
                "completedAt": _f(_S, "When it ended."),
                "inputs": _f(["object", "null"], "The request inputs, secret values hidden."),
                "outputs": _f(["object", "null"], "The request outputs, secret values hidden."),
            },
        ),
        tags=("read-only", "vcfa", "request", "deployment"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call after a catalog request or a day-2 action, until the request ends."
            ),
            "output_shape": (
                "Request: id, name, status, details, deploymentId, catalogItemId, requestedBy, "
                "completedTasks, totalTasks, createdAt, completedAt, inputs, outputs."
            ),
            "next_step": (
                "SUCCESSFUL: the deployment is ready. FAILED or ABORTED: read "
                f"{prefix}.request.events. Otherwise wait about 30 seconds and read again."
            ),
        },
    )


def _events_spec(prefix: str) -> VraCatalogOpSpec:
    event = {
        "id": _f(_S, "The event id; use it to read the logs."),
        "name": _f(_S, "The step, for example Allocation or Provisioning."),
        "timestamp": _f(_S, "When the event happened."),
        "details": _f(_S, "Longer text about the step."),
        "resourceName": _f(_S, "The resource the step is for."),
        "resourceType": _f(_S, "The type of that resource."),
        "hasLogs": _f(["boolean", "null"], "True when the step has logs."),
        "userEvent": _f(["boolean", "null"], "True when the event is a user input."),
    }
    return VraCatalogOpSpec(
        op_id=f"{prefix}.request.events",
        handler_attr="vra_request_events",
        path=REQUEST_EVENTS_PATH,
        summary="List the events of one request in order, to find the step that failed.",
        description=(
            "Reads the request and GET /deployment/api/requests/{requestId}/events. Returns "
            "the request state and the events oldest first, as flat rows. On a failed "
            "request the last event is the step that failed."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"request_id": _request_id_param(prefix)},
            "required": ["request_id"],
            "additionalProperties": False,
            "examples": [{"request_id": "<request-id>"}],
        },
        response_schema=_obj(
            "The request state and its events.",
            {
                "request_status": _f(_S, "The request state, for example FAILED."),
                "request_details": _f(_S, "The request text; on a failure it often says why."),
                "last_event_id": _f(_S, "The id of the newest event."),
                "last_event_name": _f(_S, "The newest event; on a failure, the step that failed."),
                "events": _rows("The events, oldest first.", event),
            },
        ),
        tags=("read-only", "vcfa", "request", "events"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": "Call when a request failed or hangs, to see which step it is on.",
            "output_shape": (
                "{request_status, request_details, last_event_id, last_event_name, events: [{id, "
                "name, timestamp, details, resourceName, resourceType, hasLogs, userEvent}]}."
            ),
            "next_step": (
                f"Read the logs of an event with hasLogs true with {prefix}.request.logs, "
                "starting with last_event_id."
            ),
        },
    )


def _logs_spec(prefix: str) -> VraCatalogOpSpec:
    return VraCatalogOpSpec(
        op_id=f"{prefix}.request.logs",
        handler_attr="vra_request_logs",
        path=REQUEST_EVENT_LOGS_PATH,
        summary="Read the log lines of one request event.",
        description=(
            "Reads GET /deployment/api/requests/{requestId}/events/{eventId}/logs as flat "
            "rows. Give since_row to read on from a row. Secret values are hidden."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "request_id": _request_id_param(prefix),
                "event_id": _id_param(f"The event id (from {prefix}.request.events)."),
                "since_row": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "Optional row number to start from; use next_since_row.",
                },
            },
            "required": ["request_id", "event_id"],
            "additionalProperties": False,
            "examples": [{"request_id": "<request-id>", "event_id": "<event-id>"}],
        },
        response_schema=_obj(
            "The log lines.",
            {
                "lines": _rows(
                    "The log lines in order.",
                    {
                        "rownum": _f(["integer", "null"], "The row number."),
                        "timestamp": _f(_S, "When the line was written."),
                        "message": _f(_S, "The log text."),
                    },
                ),
                "eof": _f("boolean", "True when the last line of the log is in this answer."),
                "next_since_row": _f(
                    ["integer", "null"], "Give it as since_row to read on; null at the end."
                ),
            },
        ),
        tags=("read-only", "vcfa", "request", "logs"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": "Call to read why a request step failed.",
            "output_shape": "{lines: [{rownum, timestamp, message}], eof, next_since_row}.",
            "next_step": (
                "If eof is false, call again with since_row = next_since_row. Then fix the "
                f"cause and request again with {prefix}.catalog.item.request."
            ),
        },
    )


def catalog_op_specs(prefix: str) -> tuple[VraCatalogOpSpec, ...]:
    """The six operations for the op id *prefix*, in the order of a request."""
    return (
        _list_spec(prefix),
        _get_spec(prefix),
        _request_spec(prefix),
        _state_spec(prefix),
        _events_spec(prefix),
        _logs_spec(prefix),
    )


def catalog_when_to_use(prefix: str, product: str) -> str:
    """The group text: which reads and which change the group holds."""
    return (
        f"Catalog items and requests of {product}. Reads: list the catalog items a project "
        f"can request ({prefix}.catalog.item.list), read one item with its request form "
        f"({prefix}.catalog.item.get), read the state of a request ({prefix}.request.get), "
        f"its events ({prefix}.request.events) and the logs of one event "
        f"({prefix}.request.logs). One change: request a catalog item into a project, "
        f"which builds a deployment ({prefix}.catalog.item.request); a second person must "
        "approve it."
    )

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of seven Orchestrator reads (the bodies live in ``typed_reads``).

``vro.about``, ``vro.health``, ``vro.workflow.list``, ``vro.workflow.get``,
``vro.execution.list``, ``vro.execution.get`` and ``vro.execution.logs``.
All are ``safe`` and need no approval.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vro.op_types import (
    GROUP_EXECUTIONS,
    GROUP_SERVER,
    GROUP_WORKFLOWS,
    NO_PARAMS_SCHEMA,
    VroTypedOp,
    id_schema,
    page_response_schema,
    string_schema,
)
from meho_backplane.connectors.vro.typed_reads import DEFAULT_LIST_LIMIT, LOG_TEXT_MAX

__all__ = ["EXECUTION_ID_SCHEMA", "MAX_RESULTS_SCHEMA", "VRO_READ_OPS", "WORKFLOW_ID_SCHEMA"]

WORKFLOW_ID_SCHEMA = id_schema(
    "Id of the workflow, as vro.workflow.list returns it (the id field of a row)."
)
EXECUTION_ID_SCHEMA = id_schema(
    "Id of the workflow run, as vro.execution.list returns it (the id field of a row)."
)
MAX_RESULTS_SCHEMA: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 10000,
    "default": DEFAULT_LIST_LIMIT,
    "description": f"Most rows to return. Default {DEFAULT_LIST_LIMIT}.",
}
_START_INDEX: dict[str, Any] = {
    "type": "integer",
    "minimum": 0,
    "default": 0,
    "description": "Index of the first row, for the next page. Default 0.",
}
_DECLARED_PARAM: dict[str, Any] = {
    "type": "object",
    "description": "One declared parameter. Default values are not returned.",
    "properties": {
        "name": string_schema("Parameter name."),
        "type": string_schema("Orchestrator type, for example string or VC:VirtualMachine."),
        "description": string_schema("Parameter description."),
    },
}
_INPUT_PARAM: dict[str, Any] = {
    "type": "object",
    "description": "One declared input. Default values are not returned.",
    "properties": {
        **_DECLARED_PARAM["properties"],
        "secret": {
            "type": "boolean",
            "description": "True when the input is a SecureString (a secret value).",
        },
    },
}
_RUN_PARAM: dict[str, Any] = {
    "type": "object",
    "description": "One input or output of the run.",
    "properties": {
        "name": string_schema("Parameter name."),
        "type": string_schema("Orchestrator type, for example string or SecureString."),
        "value": {
            "description": (
                "The value as the server sends it, usually an object keyed by the type, "
                "for example {string: {value: ...}}. A secret value is ***REDACTED***."
            ),
        },
    },
}

_ABOUT = VroTypedOp(
    op_id="vro.about",
    handler_attr="about",
    summary="Read the Orchestrator version, build and API version.",
    description=(
        "Reads GET /vco/api/about: version, build number, build date, API version, "
        "server id, time zone and features. MEHO uses the same call to detect the "
        "Orchestrator version."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema={
        "type": "object",
        "properties": {
            "version": string_schema("Orchestrator version, for example 8.18.1."),
            "build_number": string_schema("Build number."),
            "build_date": string_schema("Build date as the server sends it."),
            "api_version": string_schema("REST API version."),
            "id": string_schema("Server id."),
            "time_zone": string_schema("Time zone id of the server."),
            "features": {
                "type": "array",
                "description": "Names of the features the server reports.",
                "items": {"type": "string", "description": "Feature name."},
            },
        },
    },
    group_key=GROUP_SERVER,
    tags=("read-only", "vro", "orchestrator", "version"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first on an Orchestrator target, to learn its version and build "
            "before other calls or when a report needs the version."
        ),
        "output_shape": (
            "{version, build_number, build_date, api_version, id, time_zone, features: [name]}."
        ),
        "next_step": (
            "Call vro.health to check the services, or vro.workflow.list to find a workflow."
        ),
    },
)

_HEALTH = VroTypedOp(
    op_id="vro.health",
    handler_attr="health",
    summary="Read the health of the Orchestrator server and its services.",
    description=(
        "Reads GET /vco/api/healthstatus?showDetails=true: the server state, the overall "
        "health, and one entry per service check with its state and message."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema={
        "type": "object",
        "properties": {
            "server_state": string_schema("Server state as the server reports it."),
            "health_status": string_schema("Overall health, for example RUNNING or WARNING."),
            "description": string_schema("Text that explains the overall health."),
            "time": {
                "type": ["integer", "null"],
                "description": "Time of the check, in milliseconds since 1970.",
            },
            "instance_id": string_schema("Id of this server instance."),
            "pending_restart_in": {
                "type": ["integer", "null"],
                "description": "Time until a pending restart, when one is pending.",
            },
            "providers": {
                "type": "array",
                "description": "One entry per service check.",
                "items": {
                    "type": "object",
                    "description": "One service check.",
                    "properties": {
                        "name": string_schema("Name of the check."),
                        "state": string_schema("State of the check."),
                        "critical": {
                            "type": ["boolean", "null"],
                            "description": "True when a failure of this check is critical.",
                        },
                        "message": string_schema("Message of the check."),
                        "description": string_schema("What the check looks at."),
                    },
                },
            },
        },
    },
    group_key=GROUP_SERVER,
    tags=("read-only", "vro", "orchestrator", "health"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call when workflows fail or hang, or to check that Orchestrator is up "
            "before you look at workflow runs."
        ),
        "output_shape": (
            "{server_state, health_status, description, time, instance_id, "
            "pending_restart_in, providers: [{name, state, critical, message, description}]}."
        ),
        "next_step": (
            "If a provider is not healthy, report its name and message. If all are "
            "healthy, look at the failed runs with vro.execution.list."
        ),
    },
)

_WORKFLOW_LIST = VroTypedOp(
    op_id="vro.workflow.list",
    handler_attr="workflow_list",
    summary="List Orchestrator workflows, by part of the name or by category.",
    description=(
        "Reads GET /vco/api/workflows. Returns one flat row per workflow: id, name, "
        "category, version, description. name finds workflows whose name contains the "
        "text; category keeps one category."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "Keep workflows whose name contains this text.",
            },
            "category": {
                "type": "string",
                "minLength": 1,
                "maxLength": 200,
                "description": "Keep workflows in the category with exactly this name.",
            },
            "max_results": MAX_RESULTS_SCHEMA,
            "start_index": _START_INDEX,
        },
        "additionalProperties": False,
        "examples": [{"name": "<workflow-name>"}],
    },
    response_schema=page_response_schema(
        "One flat row per workflow.",
        {
            "id": string_schema("Workflow id; give it to vro.workflow.get."),
            "name": string_schema("Workflow name."),
            "categoryName": string_schema("Name of the workflow's category."),
            "categoryId": string_schema("Id of the workflow's category."),
            "version": string_schema("Workflow version."),
            "description": string_schema("Workflow description."),
            "href": string_schema("API address of the workflow."),
        },
    ),
    group_key=GROUP_WORKFLOWS,
    tags=("read-only", "vro", "orchestrator", "workflow"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to find a workflow and its id, for example the workflow behind a "
            "failed Automation deployment, before you read it or its runs."
        ),
        "output_shape": (
            "{items: [{id, name, categoryName, categoryId, version, description, href, "
            "...}], total, start_index, count, more_rows}. Rows hold the fields the server "
            "sends; a long list is behind a result handle."
        ),
        "next_step": (
            "Call vro.workflow.get with the id to see its inputs, or vro.execution.list "
            "with the id to see its runs. If more_rows is true, call again with "
            "start_index set to start_index + count."
        ),
        "result_scalars": {"keys": ["total", "start_index", "count", "more_rows"]},
    },
)

_WORKFLOW_GET = VroTypedOp(
    op_id="vro.workflow.get",
    handler_attr="workflow_get",
    summary="Read one Orchestrator workflow with its inputs and outputs.",
    description=(
        "Reads GET /vco/api/workflows/{workflowId} and its /content: name, version, "
        "description, category, the declared inputs and outputs (name, type, "
        "description; inputs also secret) and content_sha256, a checksum of the workflow "
        "content. Default values are not returned."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"workflow_id": WORKFLOW_ID_SCHEMA},
        "required": ["workflow_id"],
        "additionalProperties": False,
        "examples": [{"workflow_id": "<workflow-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "id": string_schema("Workflow id."),
            "name": string_schema("Workflow name."),
            "display_name": string_schema("Display name from the workflow content."),
            "version": string_schema("Workflow version."),
            "description": string_schema("Workflow description."),
            "category_id": string_schema("Id of the workflow's category."),
            "content_sha256": {
                "type": "string",
                "description": (
                    "SHA-256 (hex) of the workflow content in a fixed form. The same "
                    "content always gives the same value; a changed workflow gives a new one."
                ),
            },
            "inputs": {
                "type": "array",
                "description": "The declared inputs.",
                "items": _INPUT_PARAM,
            },
            "outputs": {
                "type": "array",
                "description": "The declared outputs.",
                "items": _DECLARED_PARAM,
            },
        },
    },
    group_key=GROUP_WORKFLOWS,
    tags=("read-only", "vro", "orchestrator", "workflow"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to learn what a workflow needs and gives back: its inputs and outputs "
            "with their types."
        ),
        "output_shape": (
            "{id, name, display_name, version, description, category_id, content_sha256, "
            "inputs: [{name, type, description, secret}], "
            "outputs: [{name, type, description}]}. secret is true for a SecureString input."
        ),
        "next_step": "Call vro.execution.list with the same workflow_id to see its runs.",
    },
)

_EXECUTION_LIST = VroTypedOp(
    op_id="vro.execution.list",
    handler_attr="execution_list",
    summary="List the runs of one Orchestrator workflow: state, start, end and user.",
    description=(
        "Reads GET /vco/api/workflows/{workflowId}/executions. Returns one flat row per "
        "run (execution) with its id, state, start and end date and the user who "
        "started it."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "workflow_id": WORKFLOW_ID_SCHEMA,
            "max_results": MAX_RESULTS_SCHEMA,
            "start_index": _START_INDEX,
        },
        "required": ["workflow_id"],
        "additionalProperties": False,
        "examples": [{"workflow_id": "<workflow-id>"}],
    },
    response_schema=page_response_schema(
        "One flat row per run.",
        {
            "id": string_schema("Run id; give it to vro.execution.get."),
            "name": string_schema("Workflow name."),
            "state": string_schema(
                "Run state: running, completed, failed, canceled, waiting, "
                "waiting-signal, suspended or initializing."
            ),
            "startDate": string_schema("When the run started."),
            "endDate": string_schema("When the run ended; empty while it runs."),
            "startedBy": string_schema("User who started the run."),
            "href": string_schema("API address of the run."),
        },
    ),
    group_key=GROUP_EXECUTIONS,
    tags=("read-only", "vro", "orchestrator", "workflow-run"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to find the runs of a workflow, for example the failed run behind a "
            "failed Automation deployment."
        ),
        "output_shape": (
            "{items: [{id, name, state, startDate, endDate, startedBy, href, ...}], total, "
            "start_index, count, more_rows}. Rows hold the fields the server sends; a long list is "
            "behind a result handle; filter it with result_query, for example on state."
        ),
        "next_step": (
            "Call vro.execution.get with the run id for its inputs, outputs and error, "
            "and vro.execution.logs for its log lines. If more_rows is true, call again "
            "with start_index set to start_index + count."
        ),
        "result_scalars": {"keys": ["total", "start_index", "count", "more_rows"]},
    },
)

_EXECUTION_GET = VroTypedOp(
    op_id="vro.execution.get",
    handler_attr="execution_get",
    summary="Read one Orchestrator workflow run: state, dates, user, error, inputs and outputs.",
    description=(
        "Reads GET /vco/api/workflows/{workflowId}/executions/{executionId}: state, start "
        "and end date, user, the error text of a failed run, and the inputs and outputs. "
        "outcome is a plain result next to the vendor state. Secret values come back "
        "replaced."
    ),
    parameter_schema={
        "type": "object",
        "properties": {"workflow_id": WORKFLOW_ID_SCHEMA, "execution_id": EXECUTION_ID_SCHEMA},
        "required": ["workflow_id", "execution_id"],
        "additionalProperties": False,
        "examples": [{"workflow_id": "<workflow-id>", "execution_id": "<execution-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "id": string_schema("Run id."),
            "name": string_schema("Workflow name."),
            "state": string_schema(
                "Run state: running, completed, failed, canceled, waiting, "
                "waiting-signal, suspended or initializing."
            ),
            "outcome": {
                "type": "string",
                "enum": [
                    "completed",
                    "failed",
                    "canceled",
                    "running",
                    "waiting",
                    "unknown",
                    "not_reachable",
                ],
                "description": (
                    "Plain outcome of the run: completed, failed, canceled, running "
                    "(also initializing), waiting (also waiting-signal and suspended), "
                    "unknown for a state MEHO does not know, or not_reachable when the "
                    "server did not answer."
                ),
            },
            "checked_at": string_schema("When MEHO read the run (ISO 8601, UTC)."),
            "business_state": string_schema("Business state the workflow last set."),
            "start_date": string_schema("When the run started."),
            "end_date": string_schema("When the run ended."),
            "started_by": string_schema("User who started the run."),
            "error": string_schema("Error text of a failed run."),
            "current_item": string_schema("Workflow step the run is at or stopped at."),
            "inputs": {"type": "array", "description": "Run inputs.", "items": _RUN_PARAM},
            "outputs": {"type": "array", "description": "Run outputs.", "items": _RUN_PARAM},
        },
    },
    group_key=GROUP_EXECUTIONS,
    tags=("read-only", "vro", "orchestrator", "workflow-run"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see why a run failed or what it was given: its state, error, "
            "current step, inputs and outputs."
        ),
        "output_shape": (
            "{id, name, state, outcome, checked_at, business_state, start_date, end_date, "
            "started_by, error, current_item, inputs: [{name, type, value}], "
            "outputs: [{name, type, value}]}. A SecureString, encrypted or secret-named "
            "value is ***REDACTED***."
        ),
        "next_step": (
            "If outcome is failed, report error and current_item, then call "
            "vro.execution.logs for the log lines of the run. If outcome is running or "
            "waiting, read the run again later. If outcome is not_reachable, check the "
            "server with vro.health."
        ),
    },
)

_EXECUTION_LOGS = VroTypedOp(
    op_id="vro.execution.logs",
    handler_attr="execution_logs",
    summary="Read the log lines of one Orchestrator workflow run, size-capped.",
    description=(
        "Reads GET /vco/api/workflows/{workflowId}/executions/{executionId}/syslogs. "
        "Returns at most max_lines lines: time, severity, user, origin and text. Each "
        f"text is cut at {LOG_TEXT_MAX} characters."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "workflow_id": WORKFLOW_ID_SCHEMA,
            "execution_id": EXECUTION_ID_SCHEMA,
            "max_lines": {
                "type": "integer",
                "minimum": 1,
                "maximum": 2000,
                "default": 200,
                "description": "Most log lines to return. Default 200, at most 2000.",
            },
        },
        "required": ["workflow_id", "execution_id"],
        "additionalProperties": False,
        "examples": [{"workflow_id": "<workflow-id>", "execution_id": "<execution-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "description": "One flat row per log line, in the order the server sends.",
                "items": {
                    "type": "object",
                    "description": "One log line.",
                    "properties": {
                        "time_stamp": string_schema("When the line was written."),
                        "severity": string_schema("Severity, for example info or error."),
                        "user": string_schema("User the line belongs to."),
                        "origin": string_schema("Where the line comes from."),
                        "short_description": string_schema("Short text of the line."),
                        "long_description": string_schema("Full text of the line."),
                        "cut": {
                            "type": "boolean",
                            "description": f"True when a text was cut at {LOG_TEXT_MAX} chars.",
                        },
                    },
                },
            },
            "lines": {"type": "integer", "description": "How many lines came back."},
            "more_lines": {
                "type": "boolean",
                "description": "True when the server sent more lines than max_lines.",
            },
            "more_rows": {
                "type": "boolean",
                "description": "Same as more_lines; every list read has more_rows.",
            },
        },
    },
    group_key=GROUP_EXECUTIONS,
    tags=("read-only", "vro", "orchestrator", "workflow-run", "logs"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to read what a workflow run wrote to its log, for example to find the "
            "step and message where a run failed."
        ),
        "output_shape": (
            "{items: [{time_stamp, severity, user, origin, short_description, "
            "long_description, cut}], lines, more_lines, more_rows}. A long log is behind a result "
            "handle; filter it with result_query, for example on severity."
        ),
        "next_step": (
            "Report the error lines. If more_lines is true, call again with a larger "
            "max_lines (at most 2000)."
        ),
        "result_scalars": {"keys": ["lines", "more_lines", "more_rows"]},
    },
)

VRO_READ_OPS: Final[tuple[VroTypedOp, ...]] = (
    _ABOUT,
    _HEALTH,
    _WORKFLOW_LIST,
    _WORKFLOW_GET,
    _EXECUTION_LIST,
    _EXECUTION_GET,
    _EXECUTION_LOGS,
)

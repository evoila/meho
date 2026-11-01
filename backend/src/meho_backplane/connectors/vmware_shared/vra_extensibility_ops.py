# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the ABX and event subscription operations tell the agent.

:func:`extensibility_op_specs` builds the metadata of the five operations
for one id prefix (``vcfa.vra8`` or ``vcfa.tenant``). The handlers and the
preview builders are in :mod:`.vra_extensibility`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_shared.vra_extensibility import (
    ABX_ACTION_RUNS_PATH,
    ABX_ACTIONS_PATH,
    ABX_RUN_PATH,
    ABX_RUN_STATES,
    SUBSCRIPTION_PATH,
    SUBSCRIPTIONS_PATH,
)
from meho_backplane.connectors.vmware_shared.vra_governance_ops import (
    VraOpSpec,
    f,
    id_param,
    obj,
    rows,
)

__all__ = ["RUNNABLE_TYPES", "extensibility_op_specs", "extensibility_when_to_use"]

#: What a subscription can run: an ABX action or an Orchestrator workflow.
RUNNABLE_TYPES: Final[tuple[str, ...]] = ("extensibility.abx", "extensibility.vro")

_S: Final[list[str]] = ["string", "null"]

_PAGING: Final[dict[str, Any]] = {
    "page": {"type": "integer", "minimum": 0, "description": "Optional page number, from 0."},
    "size": {"type": "integer", "minimum": 1, "maximum": 500, "description": "Optional page size."},
}

_DESTRUCTIVE_PRECONDITION: Final[str] = (
    "This change is destructive. Only a person can run it, not an agent. Call "
    "preview_operation with the same inputs first, then call_operation with the "
    "preview_hash it returned. A second person must approve it."
)


# ---------------------------------------------------------------------------
# ABX actions and runs
# ---------------------------------------------------------------------------


def _action_fields() -> dict[str, Any]:
    return {
        "id": f(_S, "The action id."),
        "name": f(_S, "The action name."),
        "description": f(_S, "The action description."),
        "actionType": f(_S, "SCRIPT, REST_CALL, REST_POLL or FLOW."),
        "runtime": f(_S, "The script language, for example python, nodejs or powershell."),
        "runtimeVersion": f(_S, "The runtime version."),
        "provider": f(_S, "Where the script runs, for example on-prem."),
        "projectId": f(_S, "The project of the action."),
        "orgId": f(_S, "The org of the action."),
        "entrypoint": f(_S, "The function the run calls."),
        "timeoutSeconds": f(["integer", "null"], "How long a run may take."),
        "memoryInMB": f(["integer", "null"], "The memory a run gets."),
        "shared": f(["boolean", "null"], "True when all projects may use it."),
        "system": f(["boolean", "null"], "True for a built-in action."),
        "inputNames": {
            "type": "array",
            "description": "The names of the inputs (defaults are not shown).",
            "items": {"type": "string", "description": "An input name."},
        },
    }


def _abx_list_spec(prefix: str) -> VraOpSpec:
    return VraOpSpec(
        op_id=f"{prefix}.abx.action.list",
        handler_attr="vra_abx_action_list",
        path=ABX_ACTIONS_PATH,
        summary="List ABX actions (scripts): runtime, project and input names.",
        description=(
            "Reads GET /abx/api/resources/actions as flat rows: name, type, runtime, project "
            "and input names. The source code and input defaults are never returned. Paged "
            "with page and size."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Optional text to find in the action name.",
                },
                **_PAGING,
            },
            "additionalProperties": False,
            "examples": [{"name": "<action-name>", "size": 50}],
        },
        response_schema=obj(
            "The ABX actions.",
            {
                "actions": rows("One row per action.", _action_fields()),
                "totalElements": f(["integer", "null"], "How many match, on all pages."),
            },
        ),
        tags=("read-only", "vcfa", "abx", "extensibility"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call to find an ABX action to run or to use in an event subscription."
            ),
            "output_shape": (
                "{actions: [{id, name, actionType, runtime, projectId, shared, system, "
                "inputNames, ...}], totalElements}. A long list comes back as a link to the "
                "stored result; read it with result_query."
            ),
            "next_step": (
                f"A person can run one with {prefix}.abx.action.run, or use its id in "
                f"{prefix}.subscription.set."
            ),
        },
        group="extensibility",
    )


def _abx_run_spec(prefix: str) -> VraOpSpec:
    return VraOpSpec(
        op_id=f"{prefix}.abx.action.run",
        handler_attr="vra_abx_action_run",
        path=ABX_ACTION_RUNS_PATH,
        summary="Run an ABX action (a script) with inputs; only a person can run it.",
        description=(
            "Sends POST /abx/api/resources/actions/{id}/action-runs. The script can do "
            "anything, so this is destructive: a preview first, then a second person "
            "approves. Inputs with a secret name are refused."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": id_param(f"The action id (from {prefix}.abx.action.list)."),
                "project_id": id_param(
                    "Optional project to run in; omit to use the project of the action."
                ),
                "inputs": {
                    "type": "object",
                    "description": (
                        "The inputs by name. No secrets: a script reads its secrets from "
                        "Automation action constants."
                    ),
                    "additionalProperties": {"description": "The value of one input."},
                },
            },
            "required": ["id"],
            "additionalProperties": False,
            "examples": [
                {
                    "id": "<action-id>",
                    "project_id": "<project-id>",
                    "inputs": {"<input-name>": "<value>"},
                }
            ],
        },
        response_schema=obj(
            "What the op did.",
            {
                "status": f(
                    "string",
                    "started, invalid_request or precondition_failed.",
                    enum=["started", "invalid_request", "precondition_failed"],
                ),
                "run": obj(
                    "The new run; null when nothing ran.",
                    {
                        "id": f(_S, "The run id; read it with the run read op."),
                        "actionId": f(_S, "The action id."),
                        "name": f(_S, "The action name."),
                        "runState": f(_S, "The run state."),
                        "projectId": f(_S, "The project the run is in."),
                    },
                    nullable=True,
                ),
                "guidance": f(_S, "A short note on what to do next."),
            },
        ),
        tags=("write", "vcfa", "abx", "extensibility"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions={
            "when_to_call": "Call when a person wants to run one ABX action now.",
            "preconditions": (
                f"Find the action and its input names with {prefix}.abx.action.list. "
                f"{_DESTRUCTIVE_PRECONDITION} When the action changed after the approval, "
                "nothing runs (precondition_failed)."
            ),
            "output_shape": (
                "{status: started | invalid_request | precondition_failed, run: {id, "
                "actionId, name, runState, projectId}, guidance}."
            ),
            "next_step": f"Read {prefix}.abx.run.get with run.id until done is true.",
        },
        group="extensibility",
    )


def _run_get_spec(prefix: str) -> VraOpSpec:
    states = ", ".join(ABX_RUN_STATES)
    return VraOpSpec(
        op_id=f"{prefix}.abx.run.get",
        handler_attr="vra_abx_run_get",
        path=ABX_RUN_PATH,
        summary="Read the state, outputs and log of one ABX action run.",
        description=(
            "Reads GET /abx/api/resources/action-runs/{id}: the state, the error, the inputs "
            "and outputs and the last log lines. Secret values are hidden; the script source "
            "is not returned."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": id_param(f"The run id (run.id from {prefix}.abx.action.run)."),
                "log_tail": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5000,
                    "description": "Optional; how many of the last log lines to return (200).",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
            "examples": [{"id": "<run-id>", "log_tail": 100}],
        },
        response_schema=obj(
            "One ABX action run.",
            {
                "id": f(_S, "The run id."),
                "actionId": f(_S, "The action id."),
                "name": f(_S, "The action name."),
                "runState": f(_S, f"The run state: {states}.", enum=[*ABX_RUN_STATES, None]),
                "done": f("boolean", "True when the run has ended."),
                "errorMessage": f(_S, "Why the run failed, when it failed."),
                "actionType": f(_S, "The action type."),
                "runtime": f(_S, "The script language."),
                "projectId": f(_S, "The project of the run."),
                "createdMillis": f(["integer", "null"], "When the run was made (ms)."),
                "startTimeMillis": f(["integer", "null"], "When it started (ms)."),
                "endTimeMillis": f(["integer", "null"], "When it ended (ms)."),
                "inputs": f(["object", "null"], "The run inputs, secret values hidden."),
                "outputs": f(["object", "null"], "The run outputs, secret values hidden."),
                "log_lines_total": f("integer", "How many log lines the run has."),
                "log_lines": {
                    "type": "array",
                    "description": "The last log lines, secret values hidden.",
                    "items": {"type": "string", "description": "One log line."},
                },
            },
        ),
        tags=("read-only", "vcfa", "abx", "extensibility"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": (
                "Call after an ABX run started, until it ends, or to learn why it failed."
            ),
            "output_shape": (
                "{id, actionId, name, runState, done, errorMessage, inputs, outputs, "
                "log_lines_total, log_lines: [text]}."
            ),
            "next_step": (
                "done false: wait about 10 seconds and read again. FAILED: read errorMessage "
                "and log_lines."
            ),
            "result_scalars": {"keys": ["id", "runState", "done", "errorMessage"]},
        },
        group="extensibility",
    )


# ---------------------------------------------------------------------------
# Event subscriptions
# ---------------------------------------------------------------------------


def _subscription_fields() -> dict[str, Any]:
    return {
        "id": f(_S, "The subscription id."),
        "name": f(_S, "The subscription name."),
        "description": f(_S, "The description."),
        "eventTopicId": f(_S, "The event topic, for example compute.provision.pre."),
        "runnableType": f(_S, "extensibility.abx (an ABX action) or extensibility.vro."),
        "runnableId": f(_S, "The id of the ABX action or workflow it runs."),
        "blocking": f(["boolean", "null"], "True when the event waits for the run."),
        "priority": f(["integer", "null"], "The order of blocking runs; 0 runs first."),
        "timeout": f(["integer", "null"], "Minutes a blocking run may take."),
        "disabled": f(["boolean", "null"], "True when the subscription is turned off."),
        "criteria": f(_S, "The condition on the event, as a JavaScript expression."),
        "projectIds": {
            "type": "array",
            "description": "The projects it is limited to; empty means all.",
            "items": {"type": "string", "description": "A project id."},
        },
        "system": f(["boolean", "null"], "True for a built-in subscription."),
        "ownerId": f(_S, "Who made it."),
    }


def _subscription_list_spec(prefix: str) -> VraOpSpec:
    return VraOpSpec(
        op_id=f"{prefix}.subscription.list",
        handler_attr="vra_subscription_list",
        path=SUBSCRIPTIONS_PATH,
        summary="List event subscriptions: topic, what they run and if they block.",
        description=(
            "Reads GET /event-broker/api/subscriptions as flat rows: the event topic, the "
            "ABX action or workflow it runs, blocking, priority, condition and projects. "
            "Paged with page and size."
        ),
        parameter_schema={
            "type": "object",
            "properties": dict(_PAGING),
            "additionalProperties": False,
            "examples": [{"page": 0, "size": 100}],
        },
        response_schema=obj(
            "The event subscriptions.",
            {
                "subscriptions": rows("One row per subscription.", _subscription_fields()),
                "totalElements": f(["integer", "null"], "How many there are, on all pages."),
            },
        ),
        tags=("read-only", "vcfa", "subscription", "extensibility"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_call": "Call to see which scripts run on which Automation events.",
            "output_shape": (
                "{subscriptions: [{id, name, eventTopicId, runnableType, runnableId, blocking, "
                "priority, timeout, disabled, criteria, projectIds, system}], totalElements}. "
                "A long list comes back as a link to the stored result; read it with "
                "result_query."
            ),
            "next_step": (
                f"A person can change, turn off or delete one with {prefix}.subscription.set."
            ),
        },
        group="extensibility",
    )


def _subscription_set_params(prefix: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "disable", "enable", "delete"],
                "description": (
                    "create a new one, update fields, disable (turn off), enable (turn on) "
                    "or delete one."
                ),
            },
            "id": id_param(
                f"The subscription id (from {prefix}.subscription.list); needed for every "
                "action but create."
            ),
            "name": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "description": "The name; needed for create.",
            },
            "description": {
                "type": "string",
                "maxLength": 2000,
                "description": "Optional description.",
            },
            "event_topic_id": id_param(
                "The event topic, for example compute.provision.pre; needed for create."
            ),
            "runnable_type": {
                "type": "string",
                "enum": list(RUNNABLE_TYPES),
                "description": "What it runs: an ABX action or a workflow; needed for create.",
            },
            "runnable_id": id_param(
                f"The ABX action id (from {prefix}.abx.action.list) or the workflow id; "
                "needed for create."
            ),
            "blocking": {
                "type": "boolean",
                "description": "Optional; true makes the event wait for the run.",
            },
            "priority": {
                "type": "integer",
                "minimum": 0,
                "description": "Optional order of blocking runs; 0 runs first.",
            },
            "timeout": {
                "type": "integer",
                "minimum": 0,
                "description": "Optional minutes a blocking run may take.",
            },
            "criteria": {
                "type": "string",
                "maxLength": 4096,
                "description": "Optional condition on the event, as a JavaScript expression.",
            },
            "project_ids": {
                "type": "array",
                "description": "Optional projects to limit it to; empty means all projects.",
                "items": {"type": "string", "minLength": 1, "description": "A project id."},
            },
            "disabled": {
                "type": "boolean",
                "description": "Optional for create; true makes it turned off.",
            },
        },
        "required": ["action"],
        "if": {"properties": {"action": {"const": "create"}}},
        "then": {"required": ["name", "event_topic_id", "runnable_type", "runnable_id"]},
        "else": {"required": ["id"]},
        "additionalProperties": False,
        "examples": [
            {
                "action": "create",
                "name": "<subscription-name>",
                "event_topic_id": "compute.provision.post",
                "runnable_type": "extensibility.abx",
                "runnable_id": "<action-id>",
                "blocking": False,
                "project_ids": ["<project-id>"],
            },
            {"action": "disable", "id": "<subscription-id>"},
        ],
    }


def _subscription_set_spec(prefix: str) -> VraOpSpec:
    return VraOpSpec(
        op_id=f"{prefix}.subscription.set",
        handler_attr="vra_subscription_set",
        path=SUBSCRIPTION_PATH,
        summary="Create, change, turn off or on, or delete an event subscription.",
        description=(
            "Sends POST /event-broker/api/subscriptions, or PATCH or DELETE on "
            "/event-broker/api/subscriptions/{id}. A subscription runs a script on future "
            "events, so this is destructive: a preview first, then a second person approves."
        ),
        parameter_schema=_subscription_set_params(prefix),
        response_schema=obj(
            "What the op did.",
            {
                "status": f(
                    "string",
                    "What happened.",
                    enum=[
                        "created",
                        "changed",
                        "disabled",
                        "enabled",
                        "deleted",
                        "unchanged",
                        "invalid_request",
                        "precondition_failed",
                    ],
                ),
                "subscription": obj(
                    "The subscription after the call.", _subscription_fields(), nullable=True
                ),
                "guidance": f(_S, "A short note on what to do next."),
            },
        ),
        tags=("write", "vcfa", "subscription", "extensibility"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions={
            "when_to_call": (
                "Call when a person wants a script to run on an Automation event, or wants "
                "to change, turn off or delete such a subscription."
            ),
            "preconditions": (
                f"Read the subscriptions with {prefix}.subscription.list and the action with "
                f"{prefix}.abx.action.list. {_DESTRUCTIVE_PRECONDITION} A system subscription "
                "is not changed. When the subscription changed after the approval, nothing "
                "happens (precondition_failed)."
            ),
            "output_shape": (
                "{status: created | changed | disabled | enabled | deleted | unchanged | "
                "invalid_request | precondition_failed, subscription: {id, name, eventTopicId, "
                "runnableType, runnableId, blocking, disabled, ...}, guidance}."
            ),
            "next_step": f"Check the result with {prefix}.subscription.list.",
        },
        group="extensibility",
    )


def extensibility_op_specs(prefix: str) -> tuple[VraOpSpec, ...]:
    """The five ABX and subscription operations for the op id *prefix*."""
    return (
        _abx_list_spec(prefix),
        _abx_run_spec(prefix),
        _run_get_spec(prefix),
        _subscription_list_spec(prefix),
        _subscription_set_spec(prefix),
    )


def extensibility_when_to_use(prefix: str, product: str) -> str:
    """The group text: which reads and which changes the group holds."""
    return (
        f"ABX actions and event subscriptions of {product}. Reads: list the ABX actions "
        f"(scripts) ({prefix}.abx.action.list), read the state and log of an action run "
        f"({prefix}.abx.run.get) and list the event subscriptions "
        f"({prefix}.subscription.list). Changes: run an ABX action ({prefix}.abx.action.run) "
        f"and create, change, turn off or delete an event subscription "
        f"({prefix}.subscription.set). Each change needs a preview, only a person can run it, "
        "and a second person must approve it."
    )

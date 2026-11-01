# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the two Orchestrator changes (the bodies live in ``run_writes``).

``vro.workflow.run`` is ``destructive``: a workflow can do anything, so
only a person runs it, after a preview and a second person's approval.
``vro.execution.cancel`` is ``dangerous`` and waits for approval.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vro.op_types import (
    GROUP_EXECUTIONS,
    GROUP_WORKFLOWS,
    VroTypedOp,
    string_schema,
)
from meho_backplane.connectors.vro.read_ops import EXECUTION_ID_SCHEMA, WORKFLOW_ID_SCHEMA
from meho_backplane.connectors.vro.run_writes import CANCEL_OP_ID, RUN_OP_ID

__all__ = ["VRO_WRITE_OPS"]

_SCALAR_VALUE: Final[list[dict[str, Any]]] = [
    {"type": "string", "description": "Text, a date (ISO 8601) or a plug-in object id."},
    {"type": "number", "description": "A number."},
    {"type": "boolean", "description": "true or false."},
]

_INPUT: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "One workflow input: name and value, or name and secret_ref for a secret. The "
        "type comes from the workflow."
    ),
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Input name, as vro.workflow.get lists it.",
        },
        "value": {
            "description": (
                "The value. For a plug-in type (for example VC:VirtualMachine) the object "
                "id; for an Array/<type> input a list. Never a secret."
            ),
            "anyOf": [
                *_SCALAR_VALUE,
                {
                    "type": "array",
                    "maxItems": 1000,
                    "description": "The elements of an Array/<type> input.",
                    "items": {"description": "One element.", "anyOf": _SCALAR_VALUE},
                },
            ],
        },
        "secret_ref": {
            "type": "string",
            "minLength": 3,
            "maxLength": 500,
            "pattern": "^[^#]+#[^#]+$",
            "description": (
                "Vault place of a secret value, '<vault-path>#<field>'. Required for a "
                "SecureString input; MEHO reads it when the approved run starts."
            ),
        },
    },
    "required": ["name"],
    "oneOf": [
        {"required": ["value"], "description": "A plain value."},
        {"required": ["secret_ref"], "description": "A secret, read from Vault."},
    ],
    "additionalProperties": False,
}

_WORKFLOW_RUN = VroTypedOp(
    op_id=RUN_OP_ID,
    handler_attr="workflow_run",
    summary="Run an Orchestrator workflow with inputs; returns the run id at once.",
    description=(
        "Starts POST /vco/api/workflows/{workflowId}/executions after a preview and a "
        "person's approval. Answers accepted with the run id; the run is not done yet. "
        "Secret inputs come from Vault (secret_ref), never as values."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "workflow_id": WORKFLOW_ID_SCHEMA,
            "inputs": {
                "type": "array",
                "maxItems": 200,
                "description": "The inputs to give. Left-out inputs keep their default.",
                "items": _INPUT,
            },
            "allow_parallel": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Start even when a run of this workflow is still running or waiting. "
                    "Default false: then MEHO returns that run and starts nothing."
                ),
            },
        },
        "required": ["workflow_id"],
        "additionalProperties": False,
        "examples": [
            {
                "workflow_id": "<workflow-id>",
                "inputs": [
                    {"name": "<input-name>", "value": "<input-value>"},
                    {"name": "<secret-input-name>", "secret_ref": "<vault-path>#<field>"},
                ],
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "already_running", "precondition_failed"],
                "description": (
                    "accepted: the run started. already_running: a run of this workflow "
                    "is still going; nothing started. precondition_failed: the workflow "
                    "or inputs differ from the approved card; nothing started."
                ),
            },
            "workflow_id": string_schema("Workflow id."),
            "workflow_name": string_schema("Workflow name."),
            "execution_id": string_schema(
                "Run id: the new run (accepted) or the running one (already_running)."
            ),
            "state": string_schema("Run state when the server gives it."),
            "run_sha256": {
                "type": "string",
                "description": "Checksum of the workflow version and content and the inputs.",
            },
            "status_read": {
                "type": "string",
                "description": "The operation that reads the run state: vro.execution.get.",
            },
            "message": {"type": "string", "description": "What happened, in plain words."},
        },
    },
    group_key=GROUP_WORKFLOWS,
    tags=("write", "destructive", "vro", "orchestrator", "workflow", "workflow-run"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when a person asks to run an Orchestrator workflow. Only a person can "
            "run it; an agent call is refused."
        ),
        "preconditions": (
            "Read the workflow with vro.workflow.get first and give each input by its "
            "name. Give a secret input (secret: true) as secret_ref, never as a value. "
            "Call preview_operation with the same inputs, then call_operation with its "
            "preview_hash. Use async: true, so MEHO keeps the run id under a run handle "
            "if the reply is lost."
        ),
        "output_shape": (
            "{status: accepted | already_running | precondition_failed, workflow_id, "
            "workflow_name, execution_id, state, run_sha256, status_read, message}."
        ),
        "next_step": (
            "Read vro.execution.get with workflow_id and execution_id until outcome is "
            "completed, failed or canceled; on failed, read vro.execution.logs. If the "
            "reply was lost, do not start again: first read vro.execution.list for this "
            "workflow and look for a run started at that time."
        ),
    },
)

_EXECUTION_CANCEL = VroTypedOp(
    op_id=CANCEL_OP_ID,
    handler_attr="execution_cancel",
    summary="Cancel an Orchestrator workflow run that has not ended.",
    description=(
        "Reads the run state, then sends DELETE /vco/api/workflows/{workflowId}/"
        "executions/{executionId}/state. A run that already ended is unchanged. Waits "
        "for approval. Steps that already ran are not undone."
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
            "status": {
                "type": "string",
                "enum": ["cancel_requested", "unchanged"],
                "description": (
                    "cancel_requested: Orchestrator accepted the cancel. unchanged: the "
                    "run had already ended; nothing was sent."
                ),
            },
            "workflow_id": string_schema("Workflow id."),
            "execution_id": string_schema("Run id."),
            "previous_state": string_schema("Run state before the cancel."),
            "status_read": {
                "type": "string",
                "description": "The operation that reads the run state: vro.execution.get.",
            },
            "message": {"type": "string", "description": "What happened, in plain words."},
        },
    },
    group_key=GROUP_EXECUTIONS,
    tags=("write", "vro", "orchestrator", "workflow-run"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when a person asks to stop a workflow run that hangs or runs by mistake."
        ),
        "preconditions": (
            "Find the run with vro.execution.list or vro.execution.get. The call waits "
            "for a person's approval. It is safe to run twice."
        ),
        "output_shape": (
            "{status: cancel_requested | unchanged, workflow_id, execution_id, "
            "previous_state, status_read, message}."
        ),
        "next_step": (
            "Read vro.execution.get until outcome is canceled. Check what the steps that "
            "already ran changed; MEHO does not undo them."
        ),
    },
)

VRO_WRITE_OPS: Final[tuple[VroTypedOp, ...]] = (_WORKFLOW_RUN, _EXECUTION_CANCEL)

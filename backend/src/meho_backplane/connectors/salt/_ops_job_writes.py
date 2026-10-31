# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the Salt job changes: run a state, run a command, stop a job."""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.salt._op_common import (
    GROUP_JOBS,
    JOB_STATUSES,
    WRITE_TAGS,
    SaltTypedOp,
    _text,
)
from meho_backplane.connectors.salt.job_writes import MAX_RUN_MINIONS, STATUS_READ, TGT_TYPES

__all__ = ["JOB_CANCEL_OP", "JOB_WRITE_OPS", "RUN_COMMAND_OP", "RUN_STATE_OP"]

#: The target fields both run operations take. Give tgt_uuid or tgt, not both.
_TARGET_PROPERTIES: dict[str, Any] = {
    "tgt_uuid": {
        "type": "string",
        "pattern": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        "description": "Id (uuid) of a saved target from salt.target.list. Give this or tgt.",
    },
    "tgt": {
        "type": "string",
        "minLength": 1,
        "maxLength": 1024,
        "description": (
            "Target expression, read as tgt_type says (a minion id glob by default). "
            "Give this or tgt_uuid."
        ),
    },
    "tgt_type": {
        "type": "string",
        "enum": list(TGT_TYPES),
        "default": "glob",
        "description": "How Salt reads tgt. Only used with tgt.",
    },
    "master_id": {
        "type": "string",
        "minLength": 1,
        "maxLength": 255,
        "pattern": r"^(\*|[^\s,*?\[\]]+)$",
        "default": "*",
        "description": "Only minions of this master; * for all masters. Only used with tgt.",
    },
}

_TARGET_ONE_OF: list[dict[str, Any]] = [{"required": ["tgt_uuid"]}, {"required": ["tgt"]}]

_START_RESPONSE: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["accepted", "already_running", "precondition_failed"],
            "description": (
                "accepted: RaaS took the job; it is not done yet. already_running: a job of "
                "the same function runs on the same minions; nothing was started. "
                "precondition_failed: the run differs from the approval; nothing was started."
            ),
        },
        "started": {"type": "boolean", "description": "True only when this call started a job."},
        "jid": _text(
            "Job id: of the started job, or of the job that already runs. Keep it; it is "
            "the input of the status read."
        ),
        "fun": _text("Salt function: state.apply or cmd.run."),
        "minion_count": {"type": "integer", "description": "Minions the job runs on."},
        "status_read": {
            "type": "string",
            "description": f"The operation that follows the job: {STATUS_READ}.",
        },
        "reason": _text("Why nothing was started, or a note on the answer; else null."),
    },
}

_START_OUTPUT_SHAPE = (
    "{status, started, jid, fun, minion_count, status_read, reason}. status accepted means "
    "RaaS took the job, not that it is done."
)

_FOLLOW_UP = (
    f"Call {STATUS_READ} with the jid until job_ended is true, then report job_status "
    "and the failed minions. If this reply was lost, do not start again: read "
    "salt.job.list (fun, user, newest first) to find the jid first. Pass async true "
    "to call_operation so MEHO keeps the jid under a run handle."
)

_TARGET_PRECONDITIONS = (
    "Give tgt_uuid (a saved target) or tgt with tgt_type. Call preview_operation with the "
    "same inputs: it lists every minion the target matches now, at most "
    f"{MAX_RUN_MINIONS}. Pass its preview_hash to call_operation. Only a person can run "
    "this; an agent is refused. The run uses exactly the minions on the approval card; "
    "when the target matches other minions by then, it stops with precondition_failed."
)

RUN_STATE_OP = SaltTypedOp(
    op_id="salt.job.run_state",
    handler_attr="job_run_state",
    summary="Apply Salt states, or the highstate, to the minions of a target.",
    description=(
        "Calls cmd.route_cmd with fun state.apply on the minions the approver saw. "
        "Returns the jid as soon as RaaS accepts the job; follow it with salt.job.result. "
        "A state can change anything on many machines, so only a person runs it."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            **_TARGET_PROPERTIES,
            "states": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "description": (
                    "State names (sls) to apply, in order. Leave it out to apply the "
                    "highstate: every state the top file gives each minion."
                ),
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "pattern": r"^[^\s,]+$",
                    "description": "One state name, for example a path such as web.nginx.",
                },
            },
            "test": {
                "type": "boolean",
                "default": False,
                "description": "True: a dry run (test=True) that reports what would change.",
            },
            "saltenv": {
                "type": "string",
                "minLength": 1,
                "maxLength": 255,
                "pattern": r"^[^\s,]+$",
                "description": "Salt file environment to read the states from; default base.",
            },
        },
        "oneOf": _TARGET_ONE_OF,
        "additionalProperties": False,
        "examples": [{"tgt": "<minion-glob>", "states": ["<state-name>"], "test": True}],
    },
    response_schema=_START_RESPONSE,
    group_key=GROUP_JOBS,
    tags=(*WRITE_TAGS, "job", "destructive"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to bring minions to their configured state, or to apply named states. "
            "Run with test true first to see what would change."
        ),
        "preconditions": _TARGET_PRECONDITIONS,
        "output_shape": _START_OUTPUT_SHAPE,
        "next_step": _FOLLOW_UP,
    },
)

RUN_COMMAND_OP = SaltTypedOp(
    op_id="salt.job.run_command",
    handler_attr="job_run_command",
    summary="Run one shell command on the minions of a target.",
    description=(
        "Calls cmd.route_cmd with fun cmd.run on the minions the approver saw. The "
        "command can change anything, so only a person runs it, after a preview that "
        "shows the command in full. Returns the jid; follow it with salt.job.result."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            **_TARGET_PROPERTIES,
            "command": {
                "type": "string",
                "minLength": 1,
                "maxLength": 4096,
                "description": (
                    "The shell command, as cmd.run runs it on each minion. Never put a "
                    "secret in it: MEHO refuses a command that looks like it holds one."
                ),
            },
        },
        "required": ["command"],
        "oneOf": _TARGET_ONE_OF,
        "additionalProperties": False,
        "examples": [{"tgt": "<minion-glob>", "command": "<command>"}],
    },
    response_schema=_START_RESPONSE,
    group_key=GROUP_JOBS,
    tags=(*WRITE_TAGS, "job", "destructive"),
    safety_level="destructive",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call only when no state does the job and a person asked for this exact "
            "command. Prefer salt.job.run_state."
        ),
        "preconditions": (
            _TARGET_PRECONDITIONS + " The approval card shows the command text in full. "
            "A command with a password=, token= or similar value is refused."
        ),
        "output_shape": _START_OUTPUT_SHAPE,
        "next_step": (
            _FOLLOW_UP + " salt.job.result does not show the command's output text, "
            "because it can hold a secret; it shows success and the return code."
        ),
    },
)

JOB_CANCEL_OP = SaltTypedOp(
    op_id="salt.job.cancel",
    handler_attr="job_cancel",
    summary="Stop a running Salt job.",
    description=(
        "Reads the job with cmd.get_cmds and, when it has not ended, calls cmd.stop_job "
        "(the vendor's soft stop) with its jid. Work a minion already did stays done."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "jid": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "pattern": r"^\S+$",
                "description": "Job id (jid) of the run to stop, from salt.job.list.",
            },
        },
        "required": ["jid"],
        "additionalProperties": False,
        "examples": [{"jid": "<jid>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "already_stopping", "not_running"],
                "description": (
                    "accepted: the stop was sent; the job is not stopped yet. "
                    "already_stopping or not_running: nothing was sent."
                ),
            },
            "stop_requested": {
                "type": "boolean",
                "description": "True only when this call sent the stop.",
            },
            "jid": {"type": "string", "description": "The job that was asked to stop."},
            "job_state": _text("The vendor's job state before the stop."),
            "job_status": {
                "type": "string",
                "enum": list(JOB_STATUSES),
                "description": "The job state before the stop, in plain words.",
            },
            "stop_jid": _text("Job id of the stop command RaaS sent to the masters."),
            "status_read": {
                "type": "string",
                "description": f"The operation that follows the job: {STATUS_READ}.",
            },
        },
    },
    group_key=GROUP_JOBS,
    tags=(*WRITE_TAGS, "job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call when a running job must not go on, for example a state run on the wrong minions."
        ),
        "preconditions": (
            "The job has not ended (salt.job.result shows job_ended false). The call waits "
            "until a second person approves it; an agent needs a grant for this risk level."
        ),
        "output_shape": (
            "{status, stop_requested, jid, job_state, job_status, stop_jid, status_read}."
        ),
        "next_step": (
            f"Call {STATUS_READ} with the jid until job_status is stopped or the job ended "
            "otherwise. A minion that already ran the job keeps its changes."
        ),
    },
)

JOB_WRITE_OPS: tuple[SaltTypedOp, ...] = (RUN_STATE_OP, RUN_COMMAND_OP, JOB_CANCEL_OP)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the Salt job reads: job runs and the results of one run."""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.salt._op_common import (
    _GROUP_JOBS,
    _MASTER_ID,
    _MINION_ID,
    _PAGE,
    _TAGS,
    JOB_STATES,
    SaltTypedOp,
    _limit,
    _text,
)
from meho_backplane.connectors.salt.reads import LIST_PAGE_LIMIT, RETURN_BYTES_LIMIT

_JOB_ROW_PROPERTIES: dict[str, Any] = {
    "jid": _text("Job id (jid) of the run."),
    "cmd": _text("Kind of command: local, runner or wheel."),
    "fun": _text("Salt function, for example state.apply."),
    "state": {
        "type": ["string", "null"],
        "description": "State of the run, for example completed_failures.",
    },
    "user": _text("User who started the run."),
    "start_time": _text("Start time (ISO 8601)."),
    "duration": {"type": ["number", "string", "null"], "description": "Run time."},
    "tgt_name": _text("Name of the saved target the run used."),
    "tgt_uuid": _text("Id of the saved target the run used."),
    "job_name": _text("Name of the saved job, if the run came from one."),
    "job_uuid": _text("Id of the saved job, if the run came from one."),
    "sched_name": _text("Name of the schedule, if the run was scheduled."),
    "origination": _text("Where the run was started."),
    "is_highstate": {"type": ["boolean", "null"], "description": "True for a highstate run."},
    "expected": {"type": ["integer", "null"], "description": "Minions expected to return."},
    "returned": {"type": ["integer", "null"], "description": "Minions that returned."},
    "returned_good": {"type": ["integer", "null"], "description": "Minions that succeeded."},
    "returned_failed": {"type": ["integer", "null"], "description": "Minions that failed."},
}

_JOB_LIST = SaltTypedOp(
    op_id="salt.job.list",
    handler_attr="job_list",
    summary="List recent Salt job runs: function, target, state, user and start time.",
    description=(
        "Calls cmd.get_cmds, newest first. One row per run with its jid, function, saved "
        "target, state, user, start time and minion counts (expected, returned, good, "
        "failed). Filter by state to find failed runs."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "state": {
                "type": "array",
                "minItems": 1,
                "description": "Only runs in one of these states.",
                "items": {
                    "type": "string",
                    "enum": list(JOB_STATES),
                    "description": "A run state.",
                },
            },
            "fun": {
                "type": "string",
                "minLength": 1,
                "description": "Only runs of this Salt function, for example state.apply.",
            },
            "user": {
                "type": "string",
                "minLength": 1,
                "description": "Only runs started by this user.",
            },
            "jid": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "Only the run with this job id (jid).",
            },
            "master_id": _MASTER_ID,
            "limit": _limit(LIST_PAGE_LIMIT, 500),
            "page": _PAGE,
        },
        "additionalProperties": False,
        "examples": [{"state": ["completed_failures", "completed_missing_returns"], "limit": 20}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "How many runs match, all pages."},
            "jobs": {
                "type": "array",
                "description": "One row per run, newest first.",
                "items": {
                    "type": "object",
                    "description": "One job run.",
                    "properties": _JOB_ROW_PROPERTIES,
                },
            },
        },
    },
    group_key=_GROUP_JOBS,
    tags=(*_TAGS, "job"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see what Salt ran lately, or to find failed runs: pass state "
            "completed_failures and completed_missing_returns."
        ),
        "output_shape": (
            "{count, jobs: [{jid, cmd, fun, state, user, start_time, duration, tgt_name, "
            "tgt_uuid, job_name, job_uuid, sched_name, origination, is_highstate, expected, "
            "returned, returned_good, returned_failed}]}. A long list is behind a result "
            "handle; read it with result_query."
        ),
        "next_step": (
            "For a run with returned_failed above 0 or state completed_failures, call "
            "salt.job.result with its jid to see which minions failed and why."
        ),
        "result_scalars": {"keys": ["count"]},
    },
)

_FAILED_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "One failed state.",
    "properties": {
        "state": {"type": "string", "description": "State id as Salt reports it."},
        "name": _text("Name the state works on."),
        "comment": _text("Salt's comment, at most 500 characters."),
    },
}

_JOB_RESULT = SaltTypedOp(
    op_id="salt.job.result",
    handler_attr="job_result",
    summary="Read the results of one Salt job run, one row per minion, size-capped.",
    description=(
        "Calls ret.get_returns for one jid. One row per minion: success, return code, "
        "errors, the failed states with their comments, and the return itself when it "
        "is small. Values under a secret name are replaced."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "jid": {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "Job id (jid) of the run, from salt.job.list.",
            },
            "minion_id": _MINION_ID,
            "has_errors": {
                "type": "boolean",
                "description": "True: only minions with errors. False: only minions without.",
            },
            "limit": _limit(LIST_PAGE_LIMIT, 500),
            "page": _PAGE,
        },
        "required": ["jid"],
        "additionalProperties": False,
        "examples": [{"jid": "<jid>", "has_errors": True}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "jid": {"type": "string", "description": "The job id that was read."},
            "count": {"type": "integer", "description": "How many minion results match."},
            "failed_minions": {
                "type": "integer",
                "description": "Rows on this page with errors or success false.",
            },
            "results": {
                "type": "array",
                "description": "One row per minion, sorted by minion id.",
                "items": {
                    "type": "object",
                    "description": "The result of one minion.",
                    "properties": {
                        "minion_id": _text("Minion id."),
                        "master_id": _text("Master of the minion."),
                        "jid": _text("Job id."),
                        "fun": _text("Salt function that ran."),
                        "fun_args": {
                            "type": ["array", "object", "null"],
                            "description": "Function arguments; null when longer than 2 KiB.",
                        },
                        "success": {"type": ["boolean", "null"], "description": "Success flag."},
                        "retcode": {"type": ["integer", "null"], "description": "Return code."},
                        "has_errors": {
                            "type": ["boolean", "null"],
                            "description": "True when the return has errors.",
                        },
                        "alter_time": _text("Time of the return (ISO 8601)."),
                        "return": {
                            "description": (
                                f"The return, when at most {RETURN_BYTES_LIMIT} bytes as "
                                "JSON; else null."
                            ),
                        },
                        "return_bytes": {
                            "type": "integer",
                            "description": "Size of the return as JSON, in bytes.",
                        },
                        "return_truncated": {
                            "type": "boolean",
                            "description": "True when the return was too big and left out.",
                        },
                        "return_withheld": {
                            "type": "boolean",
                            "description": (
                                "True for a pillar read (pillar.*, config.get, config.option, "
                                "config.items): the return holds pillar data and is null."
                            ),
                        },
                        "states_total": {
                            "type": ["integer", "null"],
                            "description": "States in a state run; null for other functions.",
                        },
                        "states_failed": {
                            "type": ["integer", "null"],
                            "description": "Failed states; null for other functions.",
                        },
                        "failed_states": {
                            "type": "array",
                            "description": "The failed states, at most 20.",
                            "items": _FAILED_STATE_SCHEMA,
                        },
                    },
                },
            },
        },
    },
    group_key=_GROUP_JOBS,
    tags=(*_TAGS, "job", "result"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call after salt.job.list, with the jid of a run, to see which minions failed "
            "and why. Pass has_errors true to read only the failed minions."
        ),
        "output_shape": (
            "{jid, count, failed_minions, results: [{minion_id, master_id, jid, fun, "
            "fun_args, success, retcode, has_errors, alter_time, return, return_bytes, "
            "return_truncated, return_withheld, states_total, states_failed, failed_states: "
            "[{state, name, comment}]}]}. return is null when it is bigger than 8 KiB; "
            "failed_states still names what failed. return is also null, with "
            "return_withheld true, for pillar reads (pillar.*, config.get, config.option, "
            "config.items), and a pillar argument in fun_args reads [REDACTED:param_name], "
            "as pillar data holds secrets. Values under a secret name read "
            "[REDACTED:param_name]."
        ),
        "next_step": (
            "Report the failed minions with the comments of their failed states. When "
            "return_truncated is true, the failed_states list is the summary to use."
        ),
        "result_scalars": {"keys": ["jid", "count", "failed_minions"]},
    },
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the Salt job reads: job runs and the results of one run."""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.salt._op_common import (
    GROUP_JOBS,
    JOB_STATES,
    JOB_STATUSES,
    MASTER_ID_SCHEMA,
    MINION_ID_SCHEMA,
    PAGE_SCHEMA,
    TAGS,
    SaltTypedOp,
    _limit,
    _text,
)
from meho_backplane.connectors.salt.reads import LIST_PAGE_LIMIT, RETURN_BYTES_LIMIT

__all__ = ["JOB_LIST_OP", "JOB_RESULT_OP"]

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

JOB_LIST_OP = SaltTypedOp(
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
            "master_id": MASTER_ID_SCHEMA,
            "limit": _limit(LIST_PAGE_LIMIT, 500),
            "page": PAGE_SCHEMA,
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
    group_key=GROUP_JOBS,
    tags=(*TAGS, "job"),
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

_STATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "One state of a state run: only the fields whose content can be checked. The "
        "state id, the comment and the changes are free text that can quote a command "
        "with a password, so they are left out."
    ),
    "properties": {
        "state": _text("State module and function (cmd.run, file.managed, pkg.installed)."),
        "name": _text(
            "Name the state works on, only for file, pkg, service, user, group and sysctl "
            "states (a path or an object name); null for the others, because the name of a "
            "cmd state is the command."
        ),
        "sls": _text("State file the state comes from."),
        "result": {"type": ["boolean", "null"], "description": "Outcome of the state."},
        "changed": {"type": "boolean", "description": "True when the state changed something."},
        "run_num": {"type": ["integer", "null"], "description": "Position in the run."},
        "start_time": _text("Start time of the state (HH:MM:SS.ffffff)."),
        "duration": {"type": ["number", "null"], "description": "Duration in milliseconds."},
    },
}

JOB_RESULT_OP = SaltTypedOp(
    op_id="salt.job.result",
    handler_attr="job_result",
    summary="Read the state of one Salt job run and its results, one row per minion.",
    description=(
        "Calls cmd.get_cmds and ret.get_returns for one jid: the job state and progress, "
        "then one row per minion with success, return code, errors and failed states. "
        "The status read after salt.job.run_state and salt.job.run_command."
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
            "minion_id": MINION_ID_SCHEMA,
            "has_errors": {
                "type": "boolean",
                "description": "True: only minions with errors. False: only minions without.",
            },
            "limit": _limit(LIST_PAGE_LIMIT, 500),
            "page": PAGE_SCHEMA,
        },
        "required": ["jid"],
        "additionalProperties": False,
        "examples": [{"jid": "<jid>", "has_errors": True}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "jid": {"type": "string", "description": "The job id that was read."},
            "reachable": {
                "type": "boolean",
                "description": (
                    "False when the Salt server did not answer (for example during its own "
                    "upgrade). Then job_status is not reachable; the job may still run."
                ),
            },
            "checked_at": {"type": "string", "description": "When MEHO read it (ISO 8601)."},
            "error": _text("Why the server was not reachable; null when it answered."),
            "job_state": _text(
                "The vendor's job state (cmd.get_cmds), for example retrieved or "
                "completed_failures; null when RaaS lists no run with this jid."
            ),
            "job_status": {
                "type": "string",
                "enum": list(JOB_STATUSES),
                "description": (
                    "The job state in plain words: queued, running, paused, stopping, "
                    "stopped, skipped, succeeded, failed, incomplete (some minions did "
                    "not return), unknown, or not reachable."
                ),
            },
            "job_ended": {
                "type": "boolean",
                "description": "True when the job has ended; read again until it is true.",
            },
            "fun": _text("Salt function of the run."),
            "user": _text("User who started the run."),
            "start_time": _text("Start time (ISO 8601)."),
            "expected": {"type": ["integer", "null"], "description": "Minions expected."},
            "returned": {"type": ["integer", "null"], "description": "Minions that returned."},
            "returned_good": {
                "type": ["integer", "null"],
                "description": "Minions that succeeded.",
            },
            "returned_failed": {
                "type": ["integer", "null"],
                "description": "Minions that failed.",
            },
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
                            "description": (
                                "Function arguments; null when withheld or longer than 2 KiB."
                            ),
                        },
                        "fun_args_withheld": {
                            "type": "boolean",
                            "description": (
                                "True when fun_args is null because the function is not "
                                "on the short list of functions whose arguments are only "
                                "names and keys (state.apply, pkg.remove, service.restart, "
                                "grains.get, pillar.get, test.ping and the like)."
                            ),
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
                                "JSON and its content can be checked; else null. A state "
                                "run is shown as a list with one entry per state: state, "
                                "name, sls, result, changed, run_num, start_time and "
                                "duration; the state id, the comment and the changes text "
                                "are left out. A number or a boolean is shown as is."
                            ),
                        },
                        "return_bytes": {
                            "type": "integer",
                            "description": (
                                "Size of the return the server sent, as JSON, in bytes."
                            ),
                        },
                        "return_truncated": {
                            "type": "boolean",
                            "description": (
                                "True when the return, or the state list shown for it, is "
                                "bigger than 8 KiB and left out."
                            ),
                        },
                        "return_withheld": {
                            "type": "boolean",
                            "description": (
                                "True when the return is null because its content cannot "
                                "be checked: a pillar read (pillar.*, config.get, "
                                "config.option, config.items), a text (file.read, cmd.run), "
                                "a list, or a dict that is not a state run."
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
                            "items": _STATE_SCHEMA,
                        },
                    },
                },
            },
        },
    },
    group_key=GROUP_JOBS,
    tags=(*TAGS, "job", "result"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call with the jid of a run (from salt.job.list, salt.job.run_state or "
            "salt.job.run_command) to follow it until it ends, and to see which minions "
            "failed and why. Pass has_errors true to read only the failed minions."
        ),
        "output_shape": (
            "{jid, reachable, checked_at, error, job_state, job_status, job_ended, fun, "
            "user, start_time, expected, returned, returned_good, returned_failed, count, "
            "failed_minions, results: [{minion_id, master_id, jid, fun, "
            "fun_args, fun_args_withheld, success, retcode, has_errors, alter_time, return, "
            "return_bytes, return_truncated, return_withheld, states_total, states_failed, "
            "failed_states: [{state, name, sls, result, changed, run_num, start_time, "
            "duration}]}]}. return is shown only when its content can be checked: a state "
            "run as a list of the same state entries, or a number or boolean. The state "
            "id, the comment and the changes text of a state are left out, because they "
            "can quote a command with a password; name is filled only for file, pkg, "
            "service, user, group and sysctl states. Every other return (a pillar read, a "
            "text such as file.read or cmd.run returns, a list, another dict) is null "
            "with return_withheld true, because it can hold a secret under no name. "
            "return is also null when it, or the state list shown for it, is bigger than "
            "8 KiB (return_truncated true); "
            "failed_states still names what failed. fun_args is shown only for a short "
            "list of functions whose arguments are only names and keys (state.apply, "
            "pkg.remove, service.restart, grains.get, pillar.get, test.ping and the "
            "like); for every other function it is null with fun_args_withheld true, "
            "because a positional argument can be a password. A pillar argument, the "
            "default value of pillar.get or grains.get, and every value under a secret "
            "name read [REDACTED:param_name]."
        ),
        "next_step": (
            "While job_ended is false, wait and call again; returned against expected is "
            "the progress. When reachable is false, the server did not answer: the job is "
            "not failed, keep the jid and read again later. When the job ended, report the "
            "failed minions with the state (cmd.run, file.managed) and sls of their failed "
            "states; the operator reads the comment in Salt with the jid. When "
            "return_truncated is true, the failed_states list is the summary to use."
        ),
        "result_scalars": {
            "keys": [
                "jid",
                "reachable",
                "checked_at",
                "error",
                "job_state",
                "job_status",
                "job_ended",
                "expected",
                "returned",
                "returned_good",
                "returned_failed",
                "count",
                "failed_minions",
            ]
        },
    },
)

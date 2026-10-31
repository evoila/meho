# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the six fleet-lcm lifecycle changes (VCF 9.1 and newer).

* ``fleet-lcm.task.retry`` (dangerous) and ``fleet-lcm.task.cancel``
  (dangerous), in the tasks group.
* ``fleet-lcm.upgrade-plan.create`` (caution), ``.precheck`` (caution) and
  ``.apply`` (dangerous), and ``fleet-lcm.depot.sync`` (caution), in the
  lifecycle group.

Every change waits for approval. The vendor paths are in :mod:`._paths`, the
bodies in :mod:`.lifecycle_handlers`. :mod:`.typed_ops` appends
:data:`FLEET_LCM_LIFECYCLE_OPS` to the connector's op table, so its registrar
registers them. The field names come from the vendor file
``fleet-lcm-openapi.yaml`` (9.1.0.0; the same on 9.1.1).

No input takes a password. The vendor's ``userInputSpec`` can carry a VSP
cluster password for a VCF Automation upgrade; MEHO does not offer that
field, so a password value can never be passed in clear text here.
"""

from __future__ import annotations

from typing import Any, Final, Literal

from meho_backplane.connectors.fleet_lcm.op_types import FleetLcmTypedOp

__all__ = ["FLEET_LCM_LIFECYCLE_OPS", "GROUP_LIFECYCLE", "GROUP_TASKS"]

GROUP_TASKS: Final[str] = "fleet-lcm-tasks"
GROUP_LIFECYCLE: Final[str] = "fleet-lcm-lifecycle"

_WRITE_TAGS = ("write", "fleet-lcm", "vcf")

#: The component types of ``ComponentType`` in the 9.1.0 and 9.1.1 files
#: (9.1.1 adds ``OPS_NETWORKS_COLLECTOR``).
_COMPONENT_TYPES: Final[tuple[str, ...]] = (
    "OPS",
    "OPS_DATA_PLATFORM",
    "VCFA",
    "OPS_NETWORKS",
    "OPS_NETWORKS_COLLECTOR",
    "VIDB",
    "VSP",
    "VCF_FLEET_LCM",
    "VCF_SDDC_LCM",
    "VCF_FLEET_DEPOT",
    "OPS_LOGS",
    "SALT_RAAS",
    "SALT",
    "TELEMETRY_ACCEPTOR",
    "VCFMS_METRICS_STORE",
    "VCF_OPS_CLOUD_PROXY",
    "VCD_MIGRATOR",
)


def _field(type_: Any, description: str) -> dict[str, Any]:
    """One described schema field."""
    return {"type": type_, "description": description}


def _id(description: str) -> dict[str, Any]:
    return {**_field("string", description), "minLength": 1}


_TASK_ID = _id("Task UUID, from fleet-lcm.task.list or from the answer of a fleet change.")
_PLAN_ID = _id("Upgrade plan UUID, from fleet-lcm.upgrade-plan.list.")

_USER_INPUT_SPEC: dict[str, Any] = {
    "type": "object",
    "description": (
        "Extra inputs some component upgrades need. Give only the fields the "
        "component asks for. MEHO offers no password field."
    ),
    "properties": {
        "targetVspClusterFqdn": _field(
            "string", "VCF Automation upgrade: FQDN of the VSP cluster to move to."
        ),
        "targetVspClusterIpPoolCidr": _field(
            "string", "VCF Automation upgrade: IP pool of that VSP cluster, as a CIDR."
        ),
        "fqdn": _field("string", "VCF Operations for Logs upgrade: the FQDN to use."),
        "size": _field("string", "VCF Operations for Logs upgrade: the deployment size."),
        "numberOfNodes": {
            **_field("integer", "VCF Operations for Logs upgrade: the number of nodes."),
            "minimum": 1,
        },
    },
    "additionalProperties": False,
}

_COMPONENTS_FILTER: dict[str, Any] = {
    "type": "array",
    "description": (
        "Limit the plan or the run to these components. Leave it out to cover every "
        "component the plan allows."
    ),
    "items": {
        "type": "object",
        "description": "One component of the fleet.",
        "properties": {
            "id": _id("Component UUID, from fleet-lcm.component.list."),
            "userInputSpec": _USER_INPUT_SPEC,
        },
        "required": ["id"],
        "additionalProperties": False,
    },
    "minItems": 1,
}

_DESIRED_SOFTWARE: dict[str, Any] = {
    "type": "object",
    "description": "The software the fleet should run after the upgrade.",
    "properties": {
        "version": _id("The target VCF release, from fleet-lcm.release-version.list."),
        "components": {
            "type": "array",
            "description": "Target version per component type. Leave out to use the release.",
            "items": {
                "type": "object",
                "description": "The target version of one component type.",
                "properties": {
                    "type": {
                        **_field("string", "The component type."),
                        "enum": list(_COMPONENT_TYPES),
                    },
                    "version": _id("The target version of that component type."),
                    "publicName": _field("string", "The display name of the component."),
                },
                "required": ["type", "version"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["version"],
    "additionalProperties": False,
}

_STATUS_READ: dict[str, Any] = {
    "type": "object",
    "description": "The read to follow the job: op_id and params.",
    "properties": {
        "op_id": _field("string", "Always fleet-lcm.task.info."),
        "params": _field("object", "The params for that read: {taskId}."),
    },
}

_JOB_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "The vendor took the job (or one of the same kind already runs).",
    "properties": {
        "status": {
            **_field("string", "accepted: the job started. already_running: nothing started."),
            "enum": ["accepted", "already_running"],
        },
        "started": _field("boolean", "Whether this call started a job."),
        "action": _field("string", "retry, cancel, precheck, apply or sync."),
        "task_id": _field(["string", "null"], "The vendor's task id of the job."),
        "vendor_status": _field(
            ["string", "null"], "The vendor's task status when MEHO got the answer."
        ),
        "status_read": _STATUS_READ,
        "note": _field("string", "What to do next, in words."),
    },
}

_STEP: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": _field("string", "validate or create."),
        "result": _field("string", "succeeded, failed, still_running or not_run."),
        "task_id": _field(["string", "null"], "The vendor's task id of the plan check."),
        "vendor_status": _field(["string", "null"], "The vendor's status of the plan check."),
        "errors": {"type": "array", "items": {"type": "string"}, "description": "Error texts."},
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Warnings of the plan check; they do not stop the create.",
        },
    },
}

_CREATE_RESPONSE: dict[str, Any] = {
    "type": "object",
    "description": "The two steps (check, create) and the new plan.",
    "properties": {
        "outcome": {
            **_field("string", "What happened."),
            "enum": [
                "created",
                "validation_failed",
                "validation_still_running",
                "create_failed",
            ],
        },
        "plan_id": _field(["string", "null"], "The id of the created plan."),
        "plan": {
            "type": ["object", "null"],
            "description": "The vendor's upgrade plan: id, status, spec and more.",
            "properties": {
                "id": _field("string", "The plan id."),
                "status": _field("string", "DRAFT, SYNCING, READY or COMPLETED."),
                "spec": _field("object", "The plan spec as sent."),
            },
        },
        "steps": {"type": "array", "description": "Each step and its result.", "items": _STEP},
        "steps_ran": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The steps that ran to the end.",
        },
        "step_failed": {
            **_STEP,
            "type": ["object", "null"],
            "description": "The step that failed.",
        },
        "steps_not_run": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The steps that did not run.",
        },
    },
}

_ASYNC_HINT = (
    "Call it with async: true so MEHO keeps the answer (with the task id) under a run "
    "handle; a lost reply then does not lose the task id."
)


def _job_instructions(
    when: str, preconditions: str, lost_reply: str, extra_next: str = ""
) -> dict[str, object]:
    return {
        "when_to_call": when,
        "preconditions": preconditions + " The call waits until a second person approves it.",
        "output_shape": (
            "{status: accepted | already_running, started, action, task_id, vendor_status, "
            "status_read: {op_id: fleet-lcm.task.info, params: {taskId}}, note}. accepted "
            "means the vendor took the job, not that it is done."
        ),
        "next_step": (
            "Read fleet-lcm.task.info with task_id until state is succeeded, failed or "
            "canceled. not_reachable there is not a failure: read again later. "
            f"{_ASYNC_HINT} After a lost reply, do not start again blindly: {lost_reply}"
            f"{extra_next}"
        ),
    }


def _schema(
    properties: dict[str, Any], required: list[str], example: dict[str, Any]
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
        "examples": [example],
    }


_TASK_SCHEMA = _schema({"taskId": _TASK_ID}, ["taskId"], {"taskId": "<task-id>"})
_PLAN_ACTION_SCHEMA = _schema(
    {"planId": _PLAN_ID, "componentsFilter": _COMPONENTS_FILTER},
    ["planId"],
    {"planId": "<upgrade-plan-id>"},
)


def _change(
    op_id: str,
    handler_attr: str,
    summary: str,
    description: str,
    parameter_schema: dict[str, Any],
    response_schema: dict[str, Any],
    group_key: str,
    safety_level: Literal["caution", "dangerous"],
    llm_instructions: dict[str, object],
) -> FleetLcmTypedOp:
    return FleetLcmTypedOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=summary,
        description=description,
        parameter_schema=parameter_schema,
        response_schema=response_schema,
        group_key=group_key,
        tags=(*_WRITE_TAGS, op_id.split(".")[1]),
        safety_level=safety_level,
        requires_approval=True,
        llm_instructions=llm_instructions,
    )


FLEET_LCM_LIFECYCLE_OPS: tuple[FleetLcmTypedOp, ...] = (
    _change(
        "fleet-lcm.task.retry",
        "task_retry",
        "Retry a failed fleet task from the stage that failed.",
        "Retries one failed task (POST /v1/tasks/{taskId}?action=retry). The vendor "
        "runs it again from the failed stage. Returns accepted with the task id; "
        "it is not done yet. Waits for approval.",
        _TASK_SCHEMA,
        _JOB_RESPONSE,
        GROUP_TASKS,
        "dangerous",
        _job_instructions(
            "Call when a fleet task failed and fleet-lcm.task.info shows retriable=true, "
            "for example after the cause was fixed.",
            "The task must be failed and retriable (see fleet-lcm.task.info); the vendor "
            "refuses others with an error. An agent needs a grant for this risk level.",
            "read fleet-lcm.task.info first; if state is queued or running, the retry "
            "already took.",
        ),
    ),
    _change(
        "fleet-lcm.task.cancel",
        "task_cancel",
        "Cancel a running fleet task.",
        "Cancels one running task (POST /v1/tasks/{taskId}?action=cancel). The stage "
        "that runs now ends first; later stages do not run. Nothing is undone. Waits "
        "for approval.",
        _TASK_SCHEMA,
        _JOB_RESPONSE,
        GROUP_TASKS,
        "dangerous",
        _job_instructions(
            "Call to stop a running fleet task, for example an upgrade started by mistake.",
            "The task must be running and cancellable (see fleet-lcm.task.info). Stages "
            "that already ran are not undone. An agent needs a grant for this risk level.",
            "read fleet-lcm.task.info first; state canceled means the cancel took.",
        ),
    ),
    _change(
        "fleet-lcm.upgrade-plan.create",
        "upgrade_plan_create",
        "Create an upgrade plan for a target release, checked first.",
        "Checks the plan spec with the vendor (POST /v1/upgrade-plans/validations), "
        "then creates the plan (POST /v1/upgrade-plans) only when the check passed. "
        "Starts no upgrade. Waits for approval.",
        _schema(
            {"desiredSoftware": _DESIRED_SOFTWARE, "componentsFilter": _COMPONENTS_FILTER},
            ["desiredSoftware"],
            {"desiredSoftware": {"version": "<release-version>"}},
        ),
        _CREATE_RESPONSE,
        GROUP_LIFECYCLE,
        "caution",
        {
            "when_to_call": (
                "Call to plan an upgrade of the fleet to a release from "
                "fleet-lcm.release-version.list. It only makes the plan; it starts nothing."
            ),
            "preconditions": (
                "Pick the release version first. MEHO sends the spec to the vendor's plan "
                "check and waits up to one minute for it; only a passed check creates the "
                "plan. The vendor updates a matching plan instead of making a second one. "
                "The call waits until a second person approves it."
            ),
            "output_shape": (
                "{outcome: created | validation_failed | validation_still_running | "
                "create_failed, plan_id, plan, steps: [{name, result, task_id, errors, "
                "warnings}], steps_ran, step_failed, steps_not_run}. Nothing is undone "
                "when a step fails."
            ),
            "next_step": (
                "created: run fleet-lcm.upgrade-plan.precheck with plan_id. "
                "validation_failed: fix the spec from the errors and call again. "
                "validation_still_running: follow steps[0].task_id with fleet-lcm.task.info, "
                "then call again. After a lost reply, read fleet-lcm.upgrade-plan.list "
                "before you call again."
            ),
        },
    ),
    _change(
        "fleet-lcm.upgrade-plan.precheck",
        "upgrade_plan_precheck",
        "Start the pre-check of an upgrade plan.",
        "Starts the pre-check of one plan (POST /v1/upgrade-plans/{planId}?action="
        "precheck), or returns the pre-check that already runs. Returns accepted with "
        "the task id; it is not done yet. Waits for approval.",
        _PLAN_ACTION_SCHEMA,
        _JOB_RESPONSE,
        GROUP_LIFECYCLE,
        "caution",
        _job_instructions(
            "Call after fleet-lcm.upgrade-plan.create, and before apply, to check that "
            "the upgrade can run.",
            "The plan must exist (fleet-lcm.upgrade-plan.list). MEHO reads the plan first "
            "and starts nothing when a pre-check of it still runs.",
            "read fleet-lcm.upgrade-plan.info; its executions list the pre-check runs.",
            " When it succeeded, read the result in fleet-lcm.task.info, then apply.",
        ),
    ),
    _change(
        "fleet-lcm.upgrade-plan.apply",
        "upgrade_plan_apply",
        "Apply an upgrade plan: start the upgrades.",
        "Starts the upgrades of one plan (POST /v1/upgrade-plans/{planId}?action=apply), "
        "or returns the upgrade that already runs. Components restart. Returns accepted "
        "with the task id; it is not done yet. Waits for approval.",
        _PLAN_ACTION_SCHEMA,
        _JOB_RESPONSE,
        GROUP_LIFECYCLE,
        "dangerous",
        _job_instructions(
            "Call to start the upgrades of a plan whose pre-check passed.",
            "Run fleet-lcm.upgrade-plan.precheck first and check it succeeded. MEHO reads "
            "the plan first and starts nothing when an upgrade of it still runs. The "
            "upgraded components restart. An agent needs a grant for this risk level.",
            "read fleet-lcm.upgrade-plan.info; its executions list the upgrade runs.",
        ),
    ),
    _change(
        "fleet-lcm.depot.sync",
        "depot_sync",
        "Sync the fleet depot metadata now.",
        "Starts a sync of the depot metadata (POST /v1/depot-metadata?action=sync): the "
        "release catalog the fleet upgrades from. One sync runs at a time. Returns "
        "accepted with the task id. Waits for approval.",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
            "examples": [{}],
        },
        _JOB_RESPONSE,
        GROUP_LIFECYCLE,
        "caution",
        _job_instructions(
            "Call when fleet-lcm.release-version.list does not show a new release yet.",
            "None. The vendor runs one sync at a time: when one runs, it returns that "
            "sync's task and starts nothing new.",
            "list running tasks with fleet-lcm.task.list; calling again is safe, the "
            "vendor returns the running sync.",
            " When it succeeded, read fleet-lcm.release-version.list.",
        ),
    ),
)

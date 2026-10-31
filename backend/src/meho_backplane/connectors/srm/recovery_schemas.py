# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Response schemas of the SRM pairing, recovery plan, protection group, issue and history reads.

The enums copy the SRM REST API v2 file (9.1). They describe what MEHO
returns; MEHO passes a value the vendor adds later through unchanged.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "GROUP_LIST_RESPONSE",
    "HISTORY_LIST_RESPONSE",
    "ISSUE_LIST_RESPONSE",
    "PAIRING_LIST_RESPONSE",
    "PLAN_GET_RESPONSE",
    "PLAN_LIST_RESPONSE",
    "REMOTE_LOGIN_RESPONSE",
]

PLAN_STATUSES: Final = [
    "READY_STATE",
    "NO_PROTECTION_GROUPS",
    "DIRECTION_ERROR",
    "DELETING",
    "WAITING_FOR_USER_INPUT",
    "CANCEL_IN_PROGRESS",
    "TEST_IN_PROGRESS",
    "PROTECTION_GROUPS_IN_USE",
    "TEST_COMPLETE",
    "TEST_INTERRUPTED",
    "CLEANUP_IN_PROGRESS",
    "CLEANUP_INCOMPLETE",
    "CLEANUP_INTERRUPTED",
    "RECOVERY_IN_PROGRESS",
    "RECOVERY_COMPLETE",
    "RECOVERY_STARTED",
    "PARTIAL_RECOVERY",
    "RECOVERY_SPLIT_BRAIN",
    "DISASTER_RECOVERY_COMPLETE",
    "INCOMPLETE_RECOVERY",
    "RECOVERY_INTERRUPTED",
    "REPROTECT_IN_PROGRESS",
    "INCOMPLETE_REPROTECT",
    "PARTIAL_REPROTECT",
    "REPROTECT_INTERRUPTED",
    "UNKNOWN_STATE",
    "SYNC_CONFLICT",
]


def _s(description: str, enum: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": ["string", "null"], "description": description}
    if enum is not None:
        schema["enum"] = [*enum, None]
    return schema


def _t(kind: str, description: str) -> dict[str, Any]:
    return {"type": [kind, "null"], "description": description}


def _texts(description: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _list_response(row: dict[str, Any], description: str, **scalars: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            **scalars,
            "total": {"type": "integer", "description": "Number of rows in items."},
            "items": {"type": "array", "description": description, "items": row},
        },
    }


_PAIRING_ID_OUT: Final = _s("Id of the site pairing.")

_SERVER_STATUS: Final = ["OK", "WARNING", "ERROR"]


def _server_columns(prefix: str, what: str) -> dict[str, Any]:
    return {
        f"{prefix}_id": _s(f"Id of the {what}."),
        f"{prefix}_name": _s(f"Name of the {what}."),
        f"{prefix}_url": _s(f"Address of the {what}."),
        f"{prefix}_status": _s(f"Status of the {what}.", _SERVER_STATUS),
        f"{prefix}_version": _s(f"Version of the {what}."),
        f"{prefix}_build_number": _s(f"Build of the {what}."),
    }


PAIRING_LIST_RESPONSE: Final = _list_response(
    {
        "type": "object",
        "properties": {
            "pairing_id": _PAIRING_ID_OUT,
            **_server_columns("local_srm", "local SRM server"),
            **_server_columns("remote_srm", "remote SRM server"),
            **_server_columns("local_vc", "local vCenter"),
            **_server_columns("remote_vc", "remote vCenter"),
        },
    },
    "One row per pairing.",
)

REMOTE_LOGIN_RESPONSE: Final = {
    "type": "object",
    "properties": {
        "pairing_id": _PAIRING_ID_OUT,
        "logged_in": {"type": "boolean", "description": "True when the remote login worked."},
        "remote_username": _s("The user the remote site reports for the session."),
    },
}

_PLAN_ROW_PROPERTIES: Final = {
    "id": _s("Id of the recovery plan."),
    "name": _s("Name of the plan."),
    "status": _s("State of the plan as SRM reports it.", PLAN_STATUSES),
    "needs_cleanup": {
        "type": "boolean",
        "description": "True when a test ended and the plan waits for a cleanup run.",
    },
    "is_running": _t("boolean", "True while the plan runs."),
    "progress": _t("integer", "Progress in percent while the plan runs."),
    "protected_site_name": _s("Name of the protected site."),
    "recovery_site_name": _s("Name of the recovery site."),
    "location_name": _s("Folder of the plan."),
    "description": _s("Description of the plan."),
}

PLAN_LIST_RESPONSE: Final = _list_response(
    {"type": "object", "properties": _PLAN_ROW_PROPERTIES},
    "One row per recovery plan.",
    pairing_id=_PAIRING_ID_OUT,
)

_STEP_ROW: Final = {
    "type": "object",
    "properties": {
        "id": _t("integer", "Id of the step."),
        "step_number": _s("Number of the step, for example 3.1."),
        "title": _s("What the step does."),
        "status": _s(
            "State of the step.",
            [
                "INACTIVE",
                "RUNNING",
                "CANCELLED",
                "SKIPPED",
                "ALREADY_DONE",
                "SUCCESS",
                "WARNING",
                "ERROR",
            ],
        ),
        "depth": _t("integer", "Level of the step in the tree; 0 is the top."),
        "child_count": _t("integer", "Number of sub-steps."),
        "progress": _t("integer", "Progress in percent while the step runs."),
        "start_time": _s("When the step started, ISO 8601 UTC."),
        "finish_time": _s("When the step ended, ISO 8601 UTC."),
        "warnings": _texts("Warnings of the step."),
        "errors": _texts("Errors of the step."),
    },
}

_PRIORITIES: Final = ["P1", "P2", "P3", "P4", "P5"]

_PLAN_VM_ROW: Final = {
    "type": "object",
    "properties": {
        "id": _s("Id of the VM."),
        "name": _s("Name of the VM."),
        "status": _s("Recovery state of the VM, for example READY_TO_RECOVER or TEST_ERROR."),
        "protection_group": _s("Id of the protection group that protects the VM."),
        "protection_group_name": _s("Name of that protection group."),
        "priority": _s("Recovery priority group; P1 starts first.", _PRIORITIES),
        "startup_action": _s("Whether SRM powers the VM on.", ["POWER_ON", "DO_NOT_POWER_ON"]),
        "allow_migration": _t("boolean", "Whether vMotion is allowed for the VM."),
        "dependent_vms": _texts("Names of the VMs this VM waits for."),
    },
}

PLAN_GET_RESPONSE: Final = {
    "type": "object",
    "properties": {
        "pairing_id": _PAIRING_ID_OUT,
        "plan": {
            "type": "object",
            "description": "The plan, same fields as a row of srm.plan.list.",
            "properties": _PLAN_ROW_PROPERTIES,
        },
        "protection_groups": {
            "type": "array",
            "description": "The protection groups of the plan.",
            "items": {
                "type": "object",
                "properties": {
                    "id": _s("Id of the protection group."),
                    "name": _s("Name of the protection group."),
                    "status": _s("State of the protection group."),
                },
            },
        },
        "view_mode": _s(
            "The view mode of the steps.", ["test", "recovery", "cleanup", "reprotect"]
        ),
        "steps": {
            "type": "array",
            "description": "The recovery steps in order.",
            "items": _STEP_ROW,
        },
        "steps_error": _s("Why SRM gave no steps in this view mode; null when it did."),
        "vms": {"type": "array", "description": "The VMs of the plan.", "items": _PLAN_VM_ROW},
        "vm_recovery_settings": {
            "type": ["object", "null"],
            "description": (
                "Full recovery settings of the VM named by vm_id, as SRM returns them: "
                "vm_id, recovery_settings (priority, startup and shutdown actions, "
                "pre and post power-on steps, IP customization, dependent VMs) and status. "
                "Null without vm_id."
            ),
            "properties": {
                "vm_id": _s("Id of the VM."),
                "recovery_settings": {
                    "type": "object",
                    "description": "The VM's recovery settings.",
                },
                "status": _s("State of the settings.", ["OK", "SYNC_CONFLICT"]),
            },
        },
    },
}

_GROUP_ROW: Final = {
    "type": "object",
    "description": "A protection group (kind groups) or a VM of one group (kind vms).",
    "properties": {
        "id": _s("Id of the group or the VM."),
        "name": _s("Name of the group or the VM."),
        "replication_type": _s(
            "Groups: HBR is vSphere Replication, ABR is array based.",
            ["UNKNOWN", "ABR", "HBR", "VVOL", "VSAN"],
        ),
        "status": _s("Groups: state of the group, for example OK or NOT_CONFIGURED."),
        "recovery_status": _s("Groups: recovery state, for example READY or TEST_COMPLETE."),
        "total_vms": _t("integer", "Groups: number of VMs."),
        "protected_site_name": _s("Groups: name of the protected site."),
        "recovery_site_name": _s("Groups: name of the recovery site."),
        "location_name": _s("Groups: folder of the group."),
        "description": _s("Groups: description of the group."),
        "protection_status": _s("VMs: protection state, for example OK or MISSING_MAPPING."),
        "protection_errors": _texts("VMs: errors of the protection state."),
        "recovery_folder": _s("VMs: folder at the recovery site."),
        "recovery_resource_pool": _s("VMs: resource pool at the recovery site."),
        "recovery_networks": _texts("VMs: networks at the recovery site."),
        "vmotion_eligible": _t("boolean", "VMs: whether the VM can move with vMotion."),
    },
}

GROUP_LIST_RESPONSE: Final = _list_response(
    _GROUP_ROW,
    "The groups, or the VMs of one group.",
    pairing_id=_PAIRING_ID_OUT,
    group_id=_s("The group_id that was asked for; null for the group list."),
    kind={"type": "string", "enum": ["groups", "vms"], "description": "What the rows are."},
)

ISSUE_LIST_RESPONSE: Final = _list_response(
    {
        "type": "object",
        "properties": {
            "severity": _s("How bad the issue is.", ["WARNING", "ERROR"]),
            "issue_type": _s(
                "What the issue is about.",
                ["VC", "VRS", "VRMS", "SRM", "PROTECTION_GROUP", "RECOVERY_PLAN", "UNKNOWN"],
            ),
            "entity": _s("Id of the object the issue is about."),
            "entity_name": _s("Name of that object."),
            "description": _s("The issue text."),
            "time": _s("When SRM found the issue, ISO 8601 UTC."),
        },
    },
    "One row per issue.",
    pairing_id=_PAIRING_ID_OUT,
    scope={
        "type": "string",
        "enum": ["pairing", "plan", "group", "srm"],
        "description": "Whose issues these are.",
    },
    errors={"type": "integer", "description": "Number of ERROR rows."},
    warnings={"type": "integer", "description": "Number of WARNING rows."},
)

HISTORY_LIST_RESPONSE: Final = _list_response(
    {
        "type": "object",
        "properties": {
            "report_id": _s("Id of the run report (SRM run_key)."),
            "plan_name": _s("Name of the plan when it ran."),
            "operation_type": _s("Kind of run.", ["TEST", "CLEANUP", "FAILOVER", "REPROTECT"]),
            "result_state": _s(
                "How the run ended.", ["SUCCESS", "WARNINGS", "ERRORS", "CANCELLED"]
            ),
            "username": _s("User who started the run."),
            "start_time": _s("When the run started, ISO 8601 UTC."),
            "duration_ms": _t("integer", "How long the run took, in milliseconds."),
            "duration": _s("How long the run took, as text."),
            "error_count": _t("integer", "Number of errors in the run."),
            "warning_count": _t("integer", "Number of warnings in the run."),
            "errors": _texts("With report_id: the error texts."),
            "warnings": _texts("With report_id: the warning texts."),
        },
    },
    "One row per run, newest first; one row with report_id.",
    pairing_id=_PAIRING_ID_OUT,
    plan_id=_s("Id of the plan."),
)

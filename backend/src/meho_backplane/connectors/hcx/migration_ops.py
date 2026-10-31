# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the HCX migration operations (the bodies live in ``migrations``).

Reads (``safe``, no approval): ``hcx.migration.validate``,
``hcx.migration.list``, ``hcx.mobility_group.list``. Changes (each waits
for a person's approval): ``hcx.migration.start`` (``dangerous``),
``hcx.migration.cancel`` (``dangerous``), ``hcx.migration.schedule``
(``dangerous``) and ``hcx.mobility_group.set`` (``caution``).
:mod:`meho_backplane.connectors.hcx.typed_ops` adds them to
``HCX_TYPED_OPS`` and their group text to the group texts.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.hcx.migration_schemas import (
    CANCEL_ANSWER_SCHEMA,
    GROUP_ID_SCHEMA,
    GROUP_OR_ID_PARAMS,
    GROUP_SCHEMA,
    JOB_ANSWER_SCHEMA,
    MIGRATION_RESULTS_SCHEMA,
    NOTES_PROPERTIES,
    WINDOW_SCHEMA,
)
from meho_backplane.connectors.hcx.op_types import HcxTypedOp, list_response_schema, string_schema

__all__ = ["GROUP_MIGRATION", "HCX_MIGRATION_OPS", "HCX_MIGRATION_WHEN_TO_USE_BY_GROUP"]

GROUP_MIGRATION: Final = "hcx-migration"

HCX_MIGRATION_WHEN_TO_USE_BY_GROUP: Final[dict[str, str]] = {
    GROUP_MIGRATION: (
        "Use to move VMs with HCX. Reads: check a planned migration or a mobility "
        "group before it starts (hcx.migration.validate), list the migrations with "
        "their state and progress (hcx.migration.list) and list the mobility groups, "
        "the migration waves (hcx.mobility_group.list). Changes: make or change a "
        "mobility group (hcx.mobility_group.set), start migrations "
        "(hcx.migration.start), cancel or force-cancel them (hcx.migration.cancel) "
        "and set the switchover window of a group (hcx.migration.schedule)."
    ),
}

_ASYNC_NOTE: Final = (
    "Call with MEHO's async mode (async: true), so the answer with the ids is kept "
    "under a run handle even if the reply is lost. After a lost reply, do not call "
    "again blindly: read hcx.migration.list first and decide from what it shows."
)

_VALIDATE = HcxTypedOp(
    op_id="hcx.migration.validate",
    handler_attr="migration_validate",
    summary="Check a planned migration or a mobility group before it starts; changes nothing.",
    description=(
        "Lets HCX check a mobility group, or a planned one with its VMs, target and "
        "networks. Returns valid, and HCX's errors and warnings for the group and for "
        "each VM. Nothing is created or started."
    ),
    parameter_schema=GROUP_OR_ID_PARAMS,
    response_schema={
        "type": "object",
        "properties": {
            "valid": {
                "type": "boolean",
                "description": "True when HCX found no error in the group or any migration.",
            },
            "mobility_group_id": string_schema("Id of the group that was checked, if it has one."),
            **NOTES_PROPERTIES,
            "migrations": MIGRATION_RESULTS_SCHEMA,
        },
    },
    group_key=GROUP_MIGRATION,
    tags=("read-only", "hcx", "migration", "validate"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call before hcx.migration.start, with the same group or mobility_group_id, "
            "to learn whether HCX would accept the migration."
        ),
        "output_shape": (
            "{valid, mobility_group_id, errors, warnings, migrations: [{migration_id, "
            "vm_id, errors, warnings}]}. A failed check is a result (valid: false), not "
            "an error."
        ),
        "next_step": (
            "If valid is false, fix the input named by the errors and check again. If "
            "valid is true, call hcx.migration.start with the same input."
        ),
    },
)


def _with_scalars(schema: dict[str, Any]) -> dict[str, Any]:
    """The migration list schema plus the fields next to its rows."""
    properties = dict(schema["properties"])
    properties["reachable"] = {
        "type": "boolean",
        "description": "False when HCX did not answer; then items is empty.",
    }
    properties["checked_at"] = {"type": "string", "description": "When MEHO asked, ISO 8601."}
    properties["migration_ids"] = {
        "type": "array",
        "description": "The migration ids that were asked for.",
        "items": {"type": "string", "description": "Id of one migration."},
    }
    properties["mobility_group_id"] = string_schema("The mobility group that was asked for.")
    return {**schema, "properties": properties}


_START = HcxTypedOp(
    op_id="hcx.migration.start",
    handler_attr="migration_start",
    summary="Start the migrations of a mobility group, or of VMs (bulk, vMotion, RAV, cold).",
    description=(
        "Starts all migrations of a mobility group, or makes a group for the VMs and "
        "starts it. Returns as soon as HCX accepted the start, with the ids. Starts "
        "nothing when a migration of the same group or VMs still runs."
    ),
    parameter_schema=GROUP_OR_ID_PARAMS,
    response_schema=JOB_ANSWER_SCHEMA,
    group_key=GROUP_MIGRATION,
    tags=("write", "hcx", "migration", "vendor-job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to move VMs with HCX: with mobility_group_id to start a mobility group "
            "made with hcx.mobility_group.set, or with group to move VMs in one call."
        ),
        "preconditions": (
            "The site pairing and the service mesh are up (hcx.service_mesh.list). "
            "hcx.migration.validate with the same input returned valid: true. A person "
            "must approve the call. " + _ASYNC_NOTE
        ),
        "output_shape": (
            "{status, mobility_group_id, migrations: [{migration_id, vm_id, errors, "
            "warnings}], running, errors, warnings, steps: [{step, state, error}], "
            "status_read, status_read_params}. status accepted means HCX took the start; "
            "the migration is not done. A start of VMs has two steps, create_group and "
            "start_group; when start_group fails the group stays and is not removed."
        ),
        "next_step": (
            "Call hcx.migration.list with status_read_params until every migration's "
            "outcome is succeeded, failed or cancelled. If status is already_running, "
            "follow the running migrations instead. If status is failed or no_answer after "
            "create_group was done, read hcx.migration.list with the mobility_group_id, "
            "then call again with that mobility_group_id, not with group."
        ),
    },
)

_LIST = HcxTypedOp(
    op_id="hcx.migration.list",
    handler_attr="migration_list",
    summary="List the HCX migrations with their state, progress and errors; filter by state.",
    description=(
        "Returns one row per migration: id, mobility group, VM, type, vendor state, "
        "outcome (running, succeeded, failed, cancelled), progress, messages and times. "
        "Filters: state, migration ids, VM ids, mobility group."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "state": {
                "type": "array",
                "minItems": 1,
                "description": "Only migrations in one of these vendor states.",
                "items": {"type": "string", "description": "One vendor state."},
            },
            "migration_ids": {
                "type": "array",
                "minItems": 1,
                "description": "Only these migrations.",
                "items": {"type": "string", "description": "Id of one migration."},
            },
            "vm_ids": {
                "type": "array",
                "minItems": 1,
                "description": "Only the migrations of these VMs.",
                "items": {"type": "string", "description": "Id of one VM (entityId)."},
            },
            "mobility_group_id": {
                **GROUP_ID_SCHEMA,
                "description": "Only the migrations of this mobility group.",
            },
            "include_drafts": {
                "type": "boolean",
                "description": "Also list migrations of groups not started yet. Default false.",
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"mobility_group_id": "<mobility-group-id>"}],
    },
    response_schema=_with_scalars(
        list_response_schema(
            "One row per migration; the vendor fields stay.",
            {
                "migration_id": string_schema("Id of the migration."),
                "mobility_group_id": string_schema("Id of its mobility group."),
                "vm_id": string_schema("Id of the VM."),
                "vm_name": string_schema("Name of the VM."),
                "migrationType": string_schema("Vendor migration type."),
                "state": string_schema("Vendor state of the migration."),
                "outcome": {
                    "type": "string",
                    "enum": ["running", "succeeded", "failed", "cancelled", "unknown"],
                    "description": "Plain result, from the vendor progress flags and state.",
                },
                "status_message": string_schema("HCX's text on the current step."),
                "error_message": string_schema("HCX's error text when the migration failed."),
                "start_time": {
                    "type": ["integer", "null"],
                    "description": "Start, epoch milliseconds.",
                },
                "end_time": {
                    "type": ["integer", "null"],
                    "description": "End, epoch milliseconds.",
                },
                "progress": {
                    "type": ["object", "null"],
                    "description": "The vendor progress object: transfer and switchover counts.",
                },
            },
        ),
    ),
    group_key=GROUP_MIGRATION,
    tags=("read-only", "hcx", "migration", "status"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to follow migrations after hcx.migration.start or hcx.migration.cancel "
            "(pass its status_read_params), or to see which migrations run or failed."
        ),
        "output_shape": (
            "{items: [{migration_id, mobility_group_id, vm_id, vm_name, state, outcome, "
            "status_message, error_message, start_time, end_time, progress, ...}], "
            "reachable, checked_at, migration_ids, mobility_group_id}. A long list is "
            "behind a result handle; read it with result_query. reachable false means "
            "HCX did not answer at checked_at; it does not mean the migrations failed."
        ),
        "next_step": (
            "Call again later while an outcome is running. For a failed migration, report "
            "error_message. If reachable is false, keep the ids and read again later; a "
            "new login happens by itself."
        ),
        "result_scalars": {"keys": ["reachable", "checked_at", "mobility_group_id"]},
    },
)

_CANCEL = HcxTypedOp(
    op_id="hcx.migration.cancel",
    handler_attr="migration_cancel",
    summary="Cancel HCX migrations, or force-cancel a stuck one.",
    description=(
        "Asks HCX to cancel the migrations, or with force to force-cancel a stuck one. "
        "Returns as soon as HCX accepted it. A migration that already ended is not sent "
        "and is listed under already_ended."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "migrations": {
                "type": "array",
                "minItems": 1,
                "description": "The migrations to cancel, from hcx.migration.list.",
                "items": {
                    "type": "object",
                    "properties": {
                        "migration_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Id of the migration.",
                        },
                        "mobility_group_id": {
                            **GROUP_ID_SCHEMA,
                            "description": "Id of the migration's mobility group, if it has one.",
                        },
                    },
                    "required": ["migration_id"],
                    "additionalProperties": False,
                },
            },
            "force": {
                "type": "boolean",
                "description": "Force-cancel a migration that a normal cancel does not stop.",
            },
        },
        "required": ["migrations"],
        "additionalProperties": False,
        "examples": [{"migrations": [{"migration_id": "<migration-id>"}]}],
    },
    response_schema=CANCEL_ANSWER_SCHEMA,
    group_key=GROUP_MIGRATION,
    tags=("write", "hcx", "migration", "vendor-job"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to stop migrations that should not finish. Use force only for a "
            "migration a normal cancel did not stop."
        ),
        "preconditions": (
            "The migration ids come from hcx.migration.list. A person must approve the "
            "call. A cancel after the switchover started can leave the VM on either site; "
            "check before. " + _ASYNC_NOTE
        ),
        "output_shape": (
            "{status, force, migrations: [{migration_id, mobility_group_id, vm_id, errors, "
            "warnings}], already_ended: [{migration_id, outcome}], errors, warnings, "
            "status_read, status_read_params}."
        ),
        "next_step": (
            "Call hcx.migration.list with status_read_params until each outcome is "
            "cancelled (or ended otherwise). If a migration stays running, call again "
            "with force true."
        ),
    },
)

_GROUP_LIST = HcxTypedOp(
    op_id="hcx.mobility_group.list",
    handler_attr="mobility_group_list",
    summary="List the HCX mobility groups (migration waves) with their state.",
    description=(
        "Returns one row per mobility group: id, name, state, configuration status, "
        "wave, pairing, service mesh, times and user. With mobility_group_id, the "
        "summary of that one group."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "mobility_group_id": {
                **GROUP_ID_SCHEMA,
                "description": "Read only this mobility group. The other filters are ignored.",
            },
            "name": {"type": "string", "minLength": 1, "description": "Only groups of this name."},
            "state": {
                "type": "array",
                "minItems": 1,
                "description": "Only groups in one of these vendor states.",
                "items": {"type": "string", "description": "One vendor state."},
            },
            "page_size": {
                "type": "integer",
                "minimum": 1,
                "description": "Rows per page HCX returns; HCX's default when unset.",
            },
            "page_number": {
                "type": "integer",
                "minimum": 0,
                "description": "Which page HCX returns, as the page field of an answer counts.",
            },
        },
        "additionalProperties": False,
        "examples": [{}, {"mobility_group_id": "<mobility-group-id>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            **list_response_schema(
                "One row per mobility group.",
                {
                    "mobility_group_id": string_schema("Id of the group."),
                    "name": string_schema("Name of the group."),
                    "state": string_schema("Vendor state of the group."),
                    "configuration_status": string_schema("Vendor configuration status."),
                    "is_archived": {
                        "type": ["boolean", "null"],
                        "description": "True when the group is archived.",
                    },
                    "migration_wave_name": string_schema("Name of the wave the group is in."),
                    "service_mesh_id": string_schema("Id of the service mesh it uses."),
                    "created": string_schema("When the group was made."),
                    "last_updated": string_schema("When the group last changed."),
                    "migration_started": string_schema("When its migrations started."),
                    "username": string_schema("User who made the group."),
                },
            )["properties"],
            "page": {
                "type": ["object", "null"],
                "description": "HCX's page fields: page size and number, total pages and rows.",
            },
            "errors": NOTES_PROPERTIES["errors"],
        },
    },
    group_key=GROUP_MIGRATION,
    tags=("read-only", "hcx", "mobility-group"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see the mobility groups (migration waves) and their state, or to find "
            "the id of a group to start, schedule or change."
        ),
        "output_shape": (
            "{items: [{mobility_group_id, name, state, configuration_status, is_archived, "
            "migration_wave_id, migration_wave_name, pairing_id, service_mesh_id, created, "
            "last_updated, migration_started, username}], page, errors}. A long list is "
            "behind a result handle."
        ),
        "next_step": (
            "Call hcx.migration.list with a mobility_group_id for its migrations. If page "
            "says there are more pages, call again with the next page_number."
        ),
        "result_scalars": {"keys": ["page"]},
    },
)

_GROUP_SET = HcxTypedOp(
    op_id="hcx.mobility_group.set",
    handler_attr="mobility_group_set",
    summary="Make an HCX mobility group (a migration wave), or replace the settings of one.",
    description=(
        "Without mobility_group_id, makes a new mobility group with its VMs, target, "
        "networks and switchover options. With it, replaces that group's settings with "
        "the group given. Starts no migration."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "mobility_group_id": {
                **GROUP_ID_SCHEMA,
                "description": "Id of the group to change. Leave out to make a new group.",
            },
            "group": {
                **GROUP_SCHEMA,
                "description": "The whole group. A change replaces the group's settings.",
            },
        },
        "required": ["group"],
        "additionalProperties": False,
        "examples": [
            {
                "group": {
                    "name": "<group-name>",
                    "migration_type": "bulk",
                    "source": {"endpoint_id": "<site-id>", "resource_id": "<vcenter-id>"},
                    "destination": {"endpoint_id": "<site-id>", "resource_id": "<vcenter-id>"},
                    "vms": [{"vm_id": "<vm-id>"}],
                }
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "created": {"type": "boolean", "description": "True when a new group was made."},
            "mobility_group_id": string_schema("Id of the group."),
            "name": string_schema("Name of the group."),
            "state": string_schema("Vendor state of the group."),
            **NOTES_PROPERTIES,
            "migrations": MIGRATION_RESULTS_SCHEMA,
        },
    },
    group_key=GROUP_MIGRATION,
    tags=("write", "hcx", "mobility-group"),
    safety_level="caution",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to plan a migration wave: make a mobility group to check, schedule and "
            "start later, or fix the settings of a group not started yet."
        ),
        "preconditions": (
            "The site, vCenter, placement, datastore and network ids come from the "
            "hcx.inventory reads. A person must approve the call. A change sends the whole "
            "group, so read it first and keep what should stay."
        ),
        "output_shape": (
            "{created, mobility_group_id, name, state, errors, warnings, migrations: "
            "[{migration_id, vm_id, errors, warnings}]}."
        ),
        "next_step": (
            "Call hcx.migration.validate with the mobility_group_id, then "
            "hcx.migration.schedule or hcx.migration.start. If errors is not empty, fix "
            "the input and call again with the mobility_group_id."
        ),
    },
)

_SCHEDULE = HcxTypedOp(
    op_id="hcx.migration.schedule",
    handler_attr="migration_schedule",
    summary="Set or clear the switchover window of an HCX mobility group.",
    description=(
        "Sets when the migrations of a mobility group may switch over (and, optionally, "
        "when the copy may run), or clears the window. Returns how many migration "
        "schedules changed and HCX's errors."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "mobility_group_id": GROUP_ID_SCHEMA,
            "switchover": {
                **WINDOW_SCHEMA,
                "description": "The switchover window: start_time and end_time, or clear.",
            },
            "transfer": {
                **WINDOW_SCHEMA,
                "description": "Optional window for the data copy: start_time and end_time.",
            },
        },
        "required": ["mobility_group_id", "switchover"],
        "additionalProperties": False,
        "examples": [
            {
                "mobility_group_id": "<mobility-group-id>",
                "switchover": {"start_time": "<start-time>", "end_time": "<end-time>"},
            }
        ],
    },
    response_schema={
        "type": "object",
        "properties": {
            "mobility_group_id": string_schema("Id of the group."),
            "switchover": {
                "type": "object",
                "description": "The switchover window sent, in epoch milliseconds.",
                "properties": {
                    "startTime": {"type": "integer", "description": "Start, epoch ms."},
                    "expiryTime": {"type": "integer", "description": "End, epoch ms."},
                    "clearSchedule": {"type": "boolean", "description": "True when cleared."},
                },
            },
            "transfer": {
                "type": ["object", "null"],
                "description": "The copy window sent, or null.",
            },
            "affected_transfer_schedules": {
                "type": ["integer", "null"],
                "description": "How many copy schedules changed.",
            },
            "affected_switchover_schedules": {
                "type": ["integer", "null"],
                "description": "How many switchover schedules changed.",
            },
            "errors": {
                "type": "array",
                "description": "HCX's errors for the group or one migration.",
                "items": {
                    "type": "object",
                    "properties": {
                        "scope": string_schema("group or migration."),
                        "id": string_schema("Id of the group or migration."),
                        "code": string_schema("Vendor error code."),
                        "message": string_schema("Error text."),
                    },
                },
            },
        },
    },
    group_key=GROUP_MIGRATION,
    tags=("write", "hcx", "migration", "schedule"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call to set when the VMs of a mobility group switch over to the destination "
            "(the short break of a bulk or RAV migration), or to clear that window."
        ),
        "preconditions": (
            "The group exists (hcx.mobility_group.list). The window is agreed with the "
            "owners of the VMs. A person must approve the call."
        ),
        "output_shape": (
            "{mobility_group_id, switchover, transfer, affected_transfer_schedules, "
            "affected_switchover_schedules, errors: [{scope, id, code, message}]}."
        ),
        "next_step": (
            "If errors is empty, call hcx.migration.start with the mobility_group_id if it "
            "has not started; HCX switches over inside the window. Otherwise fix the window."
        ),
    },
)

HCX_MIGRATION_OPS: Final[tuple[HcxTypedOp, ...]] = (
    _VALIDATE,
    _START,
    _LIST,
    _CANCEL,
    _GROUP_LIST,
    _GROUP_SET,
    _SCHEDULE,
)

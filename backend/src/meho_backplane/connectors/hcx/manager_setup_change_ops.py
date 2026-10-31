# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the role mapping, activation, restart and service status of the HCX Manager.

The bodies live in :mod:`.manager_setup` and :mod:`.manager_restart`; the
other setup operations and the group text are in :mod:`.manager_setup_ops`.
"""

from __future__ import annotations

from typing import Final

from meho_backplane.connectors.hcx.manager_setup_ops import (
    ACTIVATION_SCHEMA,
    GROUP_MANAGER_SETUP,
    ROLE_MAPPINGS_SCHEMA,
    SETUP_PRECONDITION,
    STEPS_SCHEMA,
)
from meho_backplane.connectors.hcx.op_types import NO_PARAMS_SCHEMA, HcxTypedOp, string_schema

__all__ = ["HCX_MANAGER_CHANGE_OPS"]

_GROUPS_FIELD_TEXT: Final = (
    "SSO groups, as DOMAIN\\group (for example vsphere.local\\<group>). Replaces the "
    "groups of this role; an empty list removes them all."
)

_ROLE_MAPPING_SET = HcxTypedOp(
    op_id="hcx.manager.role_mapping.set",
    handler_attr="manager_role_mapping_set",
    summary="Set which SSO groups get the HCX System Administrator and Enterprise roles.",
    description=(
        "Reads the role mapping, replaces the groups of the roles you name and sends the "
        "whole mapping back. HCX 9.1 calls the roles Migration Admin and Migration User; "
        "the call keeps the names the Manager uses."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "system_admin_groups": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "description": "One SSO group."},
                "description": "Groups for System Administrator (Migration Admin). "
                + _GROUPS_FIELD_TEXT,
            },
            "enterprise_admin_groups": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "description": "One SSO group."},
                "description": "Groups for Enterprise Administrator (Migration User). "
                + _GROUPS_FIELD_TEXT,
            },
        },
        "anyOf": [{"required": ["system_admin_groups"]}, {"required": ["enterprise_admin_groups"]}],
        "additionalProperties": False,
        "examples": [{"system_admin_groups": ["vsphere.local\\<group>"]}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["done"], "description": "done."},
            "role_mappings": ROLE_MAPPINGS_SCHEMA,
            "message": string_schema("The Manager's message."),
        },
    },
    group_key=GROUP_MANAGER_SETUP,
    tags=("write", "hcx", "manager-setup", "role-mapping"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call after hcx.manager.vcenter.set and a restart, to give SSO groups the HCX "
            "roles. Without a mapping no SSO user can log in to HCX."
        ),
        "preconditions": (
            SETUP_PRECONDITION + " The groups must exist in SSO; HCX refuses a group it "
            "does not know. The SSO lookup service must be registered and active."
        ),
        "output_shape": "{status, role_mappings: [{role, user_groups}], message}.",
        "next_step": "Read hcx.manager.config.show to check the mapping.",
    },
)

_ACTIVATE = HcxTypedOp(
    op_id="hcx.manager.activate",
    handler_attr="manager_activate",
    summary="Activate HCX with a key from Vault, or report that HCX is already active.",
    description=(
        "HCX 4.x: activates with the key from Vault. An active HCX (for example one with "
        "a VCF licence from vCenter) answers already_active. HCX 9 has no key call: it "
        "takes the licence from vCenter."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "activation_key_secret_ref": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Vault reference (path) of a secret with the field activation_key. "
                    "Needed only when HCX 4.x is not active. Never the key itself."
                ),
            },
            "activation_server_url": {
                "type": "string",
                "pattern": r"^https://[^/\s]+(/\S*)?$",
                "description": (
                    "HCX 4.x activation server. Default https://connect.hcx.vmware.com."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{"activation_key_secret_ref": "<vault-path>"}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["activated", "already_active"],
                "description": "activated, or already_active when no key was needed.",
            },
            "activation": ACTIVATION_SCHEMA,
        },
    },
    group_key=GROUP_MANAGER_SETUP,
    tags=("write", "hcx", "manager-setup", "activation"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": "Call on a new HCX Manager after vCenter is connected.",
        "preconditions": (
            SETUP_PRECONDITION + " HCX 4.x needs a key that is not used elsewhere and a "
            "path to the activation server."
        ),
        "output_shape": "{status, activation: {activated, status, type, source, ...}}.",
        "next_step": (
            "Read hcx.manager.config.show to check activation. A refusal on HCX 9 means: "
            "add the VCF licence in vCenter."
        ),
    },
)

_RESTART = HcxTypedOp(
    op_id="hcx.manager.restart",
    handler_attr="manager_restart",
    summary="Restart the HCX application service so a setup change takes effect.",
    description=(
        "Stops, then starts the HCX application service. HCX is not usable for several "
        "minutes. Refuses while migrations run. Returns accepted; follow it with "
        "hcx.manager.service.status."
    ),
    parameter_schema={
        "type": "object",
        "properties": {
            "skip_migration_check": {
                "type": "boolean",
                "description": (
                    "Skip the check for running migrations. Default false. Set it only "
                    "on a new HCX Manager whose SSO login does not work yet."
                ),
            },
        },
        "additionalProperties": False,
        "examples": [{}],
    },
    response_schema={
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["accepted", "already_running", "failed", "no_answer"],
                "description": "accepted, already_running, failed or no_answer.",
            },
            "job_id": {
                "type": "null",
                "description": "Always null: HCX gives no job id for a restart.",
            },
            "service_state": string_schema(
                "Service state from the last answer: RUNNING, STOPPED or STARTING."
            ),
            "migration_check": {
                "type": "object",
                "description": "The check for running migrations.",
                "properties": {
                    "state": string_schema("passed, skipped or not_run."),
                    "reason": string_schema("Why."),
                },
            },
            "steps": STEPS_SCHEMA,
            "status_read": {"type": "string", "description": "hcx.manager.service.status."},
            "checked_at": {"type": "string", "description": "Time of the answer, ISO 8601."},
        },
    },
    group_key=GROUP_MANAGER_SETUP,
    tags=("write", "hcx", "manager-setup", "restart"),
    safety_level="dangerous",
    requires_approval=True,
    llm_instructions={
        "when_to_call": (
            "Call after hcx.manager.vcenter.set or hcx.manager.nsx.set, so the change "
            "takes effect. Use async mode (async: true) so a lost reply does not lose "
            "the answer."
        ),
        "preconditions": (
            SETUP_PRECONDITION + " No migration may run: the call checks the migrations "
            "first and refuses. HCX is down for several minutes."
        ),
        "output_shape": (
            "{status, job_id: null, service_state, migration_check, steps: [{step, state, "
            "error}], status_read, checked_at}. accepted never means HCX is ready."
        ),
        "next_step": (
            "Call hcx.manager.service.status until running is true, then hcx.about until "
            "it answers. If the reply was lost, read hcx.manager.service.status first; "
            "do not restart again blindly. failed after the stop means the service is "
            "stopped: call hcx.manager.restart again, which then only starts it."
        ),
    },
)

_SERVICE_STATUS = HcxTypedOp(
    op_id="hcx.manager.service.status",
    handler_attr="manager_service_status",
    summary="Read the state of the HCX application service: running, stopped or starting.",
    description=(
        "Reads the state of the HCX application service from the appliance management "
        "API. Says not_reachable, with the time, when the Manager does not answer."
    ),
    parameter_schema=NO_PARAMS_SCHEMA,
    response_schema={
        "type": "object",
        "properties": {
            "service": {"type": "string", "description": "Always appengine."},
            "state": string_schema(
                "RUNNING, STOPPED, STARTING, or not_reachable when the Manager did not answer."
            ),
            "reachable": {"type": "boolean", "description": "False when there was no answer."},
            "running": {"type": "boolean", "description": "True when the state is RUNNING."},
            "operation_status": string_schema("Vendor status of the last action."),
            "checked_at": {"type": "string", "description": "Time of the read, ISO 8601."},
        },
    },
    group_key=GROUP_MANAGER_SETUP,
    tags=("read-only", "hcx", "manager-setup", "restart"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call after hcx.manager.restart until running is true, or before a restart "
            "whose reply was lost."
        ),
        "output_shape": (
            "{service, state, reachable, running, operation_status, checked_at}. "
            "not_reachable is not a failure."
        ),
        "next_step": (
            "When running is true, call hcx.about until it answers; then the setup is in "
            "effect. Read again after a minute while it is STARTING or not_reachable."
        ),
    },
)

HCX_MANAGER_CHANGE_OPS: Final[tuple[HcxTypedOp, ...]] = (
    _ROLE_MAPPING_SET,
    _ACTIVATE,
    _RESTART,
    _SERVICE_STATUS,
)

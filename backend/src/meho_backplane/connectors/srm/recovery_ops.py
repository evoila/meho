# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Metadata of the SRM pairing, recovery plan, protection group, issue and history reads.

Seven ``safe`` reads, on SRM 8.8, Live Site Recovery 9.0 and VCF 9.1
Protection and Recovery (all serve the SRM REST API v2). The bodies live
in :mod:`meho_backplane.connectors.srm.recovery_reads`; the response
schemas in :mod:`meho_backplane.connectors.srm.recovery_schemas`.
:mod:`meho_backplane.connectors.srm.typed_ops` joins them into
``SRM_TYPED_OPS``.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.srm._op import SrmTypedOp
from meho_backplane.connectors.srm.recovery_schemas import (
    GROUP_LIST_RESPONSE,
    HISTORY_LIST_RESPONSE,
    ISSUE_LIST_RESPONSE,
    PAIRING_LIST_RESPONSE,
    PLAN_GET_RESPONSE,
    PLAN_LIST_RESPONSE,
    REMOTE_LOGIN_RESPONSE,
)

__all__ = ["RECOVERY_TYPED_OPS", "RECOVERY_WHEN_TO_USE_BY_GROUP"]

GROUP_PAIRING: Final = "srm-pairing"
GROUP_PLANS: Final = "srm-recovery-plans"
GROUP_PROTECTION: Final = "srm-protection-groups"
GROUP_ISSUES: Final = "srm-issues"

#: What each group holds. No claim about the safety of the whole group.
RECOVERY_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    GROUP_PAIRING: (
        "Use to see the site pairings of a Site Recovery Manager server and to log in "
        "to the remote site. Reads: the pairings with the SRM and vCenter servers of "
        "both sites and their status (srm.pairing.list); the login to the remote site "
        "of one pairing with an account from Vault (srm.remote_login), which many "
        "other SRM calls need."
    ),
    GROUP_PLANS: (
        "Use to check recovery plans. Reads: the plans with their state, for example "
        "ready, running or needing a cleanup (srm.plan.list); one plan with its "
        "protection groups, recovery steps, VMs and the recovery settings of one VM "
        "(srm.plan.get); the run history of a plan and one run report with its errors "
        "and warnings (srm.history.list)."
    ),
    GROUP_PROTECTION: (
        "Use to check protection groups. Read: the groups with their replication type "
        "(vSphere Replication or array based), VM count and state, or the VMs of one "
        "group with their protection state and errors (srm.group.list)."
    ),
    GROUP_ISSUES: (
        "Use to read health issues (errors and warnings). Read: the issues of a "
        "pairing, of one recovery plan, of one protection group or of one SRM server "
        "(srm.issue.list)."
    ),
}

_TAGS: Final = ("read-only", "srm", "live-recovery")


def _id(what: str, placeholder: str) -> dict[str, Any]:
    """Schema of an id that becomes one path segment."""
    return {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
        "pattern": r"^(?!\.{1,2}$)[^/\\?#%]+$",
        "description": f"{what} Example form: {placeholder}.",
    }


_PAIRING_ID: Final = _id(
    "Id of the site pairing (pairing_id from srm.pairing.list).", "<pairing-id>"
)
_PLAN_ID: Final = _id("Id of the recovery plan (id from srm.plan.list).", "<plan-id>")
_GROUP_ID: Final = _id("Id of the protection group (id from srm.group.list).", "<group-id>")


def _params(
    properties: dict[str, Any], required: list[str], example: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
        **extra,
        "examples": [example],
    }


_PAIRING_LIST = SrmTypedOp(
    op_id="srm.pairing.list",
    handler_attr="pairing_list",
    summary="List the site pairings of the SRM server with both sites' servers and status.",
    description=(
        "Reads GET /api/rest/srm/v2/pairings. One row per pairing: the pairing id and, "
        "for the local and the remote site, the SRM server and the vCenter with name, "
        "status (OK, WARNING, ERROR), version and build."
    ),
    parameter_schema=_params({}, [], {}),
    response_schema=PAIRING_LIST_RESPONSE,
    group_key=GROUP_PAIRING,
    tags=(*_TAGS, "pairing"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call first on a Site Recovery Manager target, to get the pairing_id every "
            "other SRM read needs, and to check that both sites' servers are OK."
        ),
        "output_shape": (
            "{total, items: [{pairing_id, local_srm_id, local_srm_name, local_srm_url, "
            "local_srm_status, local_srm_version, local_srm_build_number, the same for "
            "remote_srm, local_vc and remote_vc}]}. A long list is stored behind a result "
            "handle; read it with result_query."
        ),
        "next_step": (
            "Call srm.remote_login with the pairing_id before reads that need the remote "
            "site. Then srm.plan.list, srm.group.list or srm.issue.list. A status of "
            "WARNING or ERROR: call srm.issue.list for the pairing, or with srm_id set to "
            "local_srm_id or remote_srm_id."
        ),
        "result_scalars": {"keys": ["total"]},
    },
)

_REMOTE_LOGIN = SrmTypedOp(
    op_id="srm.remote_login",
    handler_attr="remote_login",
    summary="Log in to the remote site of an SRM pairing with an account from Vault.",
    description=(
        "Reads the remote site's account from the Vault path in secret_ref and sends it "
        "to POST /pairings/{pairing_id}/remote-session. Changes nothing on either site. "
        "The answer names the remote user, never the password."
    ),
    parameter_schema=_params(
        {
            "pairing_id": _PAIRING_ID,
            "secret_ref": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
                "description": (
                    "Vault path (KV version 2, mount 'secret') of the remote site's "
                    "vCenter single sign-on account, with the keys 'username' and "
                    "'password'. A path, never the password itself."
                ),
            },
        },
        ["pairing_id", "secret_ref"],
        {"pairing_id": "<pairing-id>", "secret_ref": "<vault-path-of-remote-login>"},
    ),
    response_schema=REMOTE_LOGIN_RESPONSE,
    group_key=GROUP_PAIRING,
    tags=(*_TAGS, "pairing", "login"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call before work that needs the remote site, for example reading plan "
            "steps or VM settings of the other site, or when an SRM read answers that "
            "the remote site is not logged in."
        ),
        "output_shape": "{pairing_id, logged_in: true, remote_username}.",
        "next_step": (
            "Run the SRM read you need. The remote login belongs to MEHO's SRM session: "
            "when that session ends, a read may again answer 'not logged in'; then call "
            "srm.remote_login again."
        ),
    },
)

_PLAN_LIST = SrmTypedOp(
    op_id="srm.plan.list",
    handler_attr="plan_list",
    summary="List the recovery plans of an SRM pairing with their state.",
    description=(
        "Reads GET /pairings/{pairing_id}/recovery-management/plans. One row per plan: "
        "id, name, status (for example READY_STATE, TEST_COMPLETE), needs_cleanup, "
        "is_running, progress and the two site names."
    ),
    parameter_schema=_params(
        {"pairing_id": _PAIRING_ID}, ["pairing_id"], {"pairing_id": "<pairing-id>"}
    ),
    response_schema=PLAN_LIST_RESPONSE,
    group_key=GROUP_PLANS,
    tags=(*_TAGS, "recovery-plan"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see which recovery plans exist and their state: ready, running, "
            "or waiting for a cleanup after a test."
        ),
        "output_shape": (
            "{pairing_id, total, items: [{id, name, status, needs_cleanup, is_running, "
            "progress, protected_site_name, recovery_site_name, location_name, "
            "description}]}. needs_cleanup is true for TEST_COMPLETE, TEST_INTERRUPTED, "
            "CLEANUP_INCOMPLETE and CLEANUP_INTERRUPTED. A long list is stored behind a "
            "result handle; read it with result_query."
        ),
        "next_step": (
            "For one plan's groups, steps and VMs call srm.plan.get. For its last runs "
            "call srm.history.list. For a plan in a bad state call srm.issue.list with "
            "plan_id."
        ),
        "result_scalars": {"keys": ["pairing_id", "total"]},
    },
)

_PLAN_GET = SrmTypedOp(
    op_id="srm.plan.get",
    handler_attr="plan_get",
    summary="Read one SRM recovery plan with its groups, recovery steps and VMs.",
    description=(
        "Reads one plan, its protection groups, its recovery steps in one view mode and "
        "its VMs with priority and start action. With vm_id it also reads that VM's full "
        "recovery settings."
    ),
    parameter_schema=_params(
        {
            "pairing_id": _PAIRING_ID,
            "plan_id": _PLAN_ID,
            "view_mode": {
                "type": "string",
                "enum": ["test", "recovery", "cleanup", "reprotect"],
                "default": "recovery",
                "description": (
                    "Which recovery steps to show: those of a test, a recovery, a cleanup "
                    "or a reprotect run. Default: recovery."
                ),
            },
            "vm_id": _id(
                "Id of one VM of the plan (id from the vms list). When set, the "
                "answer also has that VM's full recovery settings.",
                "<vm-id>",
            ),
        },
        ["pairing_id", "plan_id"],
        {"pairing_id": "<pairing-id>", "plan_id": "<plan-id>", "view_mode": "test"},
    ),
    response_schema=PLAN_GET_RESPONSE,
    group_key=GROUP_PLANS,
    tags=(*_TAGS, "recovery-plan"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see what a recovery plan does: which protection groups and VMs it "
            "holds, in which order the steps run and how far a running plan is."
        ),
        "output_shape": (
            "{pairing_id, plan: {id, name, status, needs_cleanup, ...}, protection_groups: "
            "[{id, name, status}], view_mode, steps: [{id, step_number, title, status, "
            "depth, progress, start_time, finish_time, warnings, errors}], steps_error, "
            "vms: [{id, name, status, protection_group_name, priority, startup_action, "
            "dependent_vms}], vm_recovery_settings}. steps_error names why SRM gave no "
            "steps in this view mode (some modes do not fit the plan's state)."
        ),
        "next_step": (
            "For steps or VMs with errors call srm.issue.list with plan_id. For past runs "
            "call srm.history.list. If a read answers that the remote site is not logged "
            "in, call srm.remote_login."
        ),
    },
)

_GROUP_LIST = SrmTypedOp(
    op_id="srm.group.list",
    handler_attr="group_list",
    summary="List SRM protection groups, or the VMs of one group, with their state.",
    description=(
        "Without group_id: one row per protection group with replication type (HBR is "
        "vSphere Replication, ABR is array based), VM count and state. With group_id: "
        "one row per VM of that group with its protection state and errors."
    ),
    parameter_schema=_params(
        {
            "pairing_id": _PAIRING_ID,
            "group_id": _id(
                "Id of one protection group. When set, the rows are the VMs of that "
                "group instead of the groups.",
                "<group-id>",
            ),
        },
        ["pairing_id"],
        {"pairing_id": "<pairing-id>"},
    ),
    response_schema=GROUP_LIST_RESPONSE,
    group_key=GROUP_PROTECTION,
    tags=(*_TAGS, "protection-group"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check which VMs SRM protects and how: the groups and their state, "
            "then the VMs of a group whose state is not OK or READY."
        ),
        "output_shape": (
            "{pairing_id, group_id, kind: 'groups' or 'vms', total, items}. kind groups: "
            "items are [{id, name, replication_type, status, recovery_status, total_vms, "
            "...}]. kind vms: items are [{id, name, protection_status, protection_errors, "
            "recovery_folder, recovery_resource_pool, recovery_networks, "
            "vmotion_eligible}]. A long list is stored behind a result handle; read it "
            "with result_query."
        ),
        "next_step": (
            "For a group with problems call srm.issue.list with group_id. To see which "
            "plans use the groups call srm.plan.get."
        ),
        "result_scalars": {"keys": ["pairing_id", "group_id", "kind", "total"]},
    },
)

_ISSUE_SCOPE_NOT_SET: Final = {
    "not": {
        "anyOf": [{"required": ["plan_id"]}, {"required": ["group_id"]}, {"required": ["srm_id"]}]
    }
}

_ISSUE_LIST = SrmTypedOp(
    op_id="srm.issue.list",
    handler_attr="issue_list",
    summary="List SRM health issues of a pairing, a recovery plan, a group or a server.",
    description=(
        "Reads the errors and warnings SRM reports. Without a scope: the pairing's "
        "issues. Set one of plan_id, group_id or srm_id for the issues of that plan, "
        "protection group or SRM server."
    ),
    parameter_schema=_params(
        {
            "pairing_id": _PAIRING_ID,
            "plan_id": {**_PLAN_ID, "description": f"{_PLAN_ID['description']} Scope: one plan."},
            "group_id": {
                **_GROUP_ID,
                "description": f"{_GROUP_ID['description']} Scope: one protection group.",
            },
            "srm_id": _id(
                "Id of one SRM server (local_srm_id or remote_srm_id from "
                "srm.pairing.list). Scope: that server.",
                "<srm-server-id>",
            ),
        },
        ["pairing_id"],
        {"pairing_id": "<pairing-id>", "plan_id": "<plan-id>"},
        oneOf=[
            _ISSUE_SCOPE_NOT_SET,
            {
                "required": ["plan_id"],
                "not": {"anyOf": [{"required": ["group_id"]}, {"required": ["srm_id"]}]},
            },
            {
                "required": ["group_id"],
                "not": {"anyOf": [{"required": ["plan_id"]}, {"required": ["srm_id"]}]},
            },
            {
                "required": ["srm_id"],
                "not": {"anyOf": [{"required": ["plan_id"]}, {"required": ["group_id"]}]},
            },
        ],
    ),
    response_schema=ISSUE_LIST_RESPONSE,
    group_key=GROUP_ISSUES,
    tags=(*_TAGS, "health", "issues"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to check the health of SRM: after srm.pairing.list shows a WARNING or "
            "ERROR status, or for a plan or group in a bad state."
        ),
        "output_shape": (
            "{pairing_id, scope: 'pairing', 'plan', 'group' or 'srm', total, errors, "
            "warnings, items: [{severity: ERROR or WARNING, issue_type, entity, "
            "entity_name, description, time}]}. A long list is stored behind a result "
            "handle; read it with result_query."
        ),
        "next_step": (
            "Report the ERROR rows first. To see the plan or group an issue names, call "
            "srm.plan.get or srm.group.list."
        ),
        "result_scalars": {"keys": ["pairing_id", "scope", "total", "errors", "warnings"]},
    },
)

_HISTORY_LIST = SrmTypedOp(
    op_id="srm.history.list",
    handler_attr="history_list",
    summary="List the run history of an SRM recovery plan, or read one run report.",
    description=(
        "Without report_id: the test, cleanup, recovery and reprotect runs of a plan "
        "(default the last 7 days), newest first, with result and error counts. With "
        "report_id: that run with its error and warning texts."
    ),
    parameter_schema=_params(
        {
            "pairing_id": _PAIRING_ID,
            "plan_id": _PLAN_ID,
            "report_id": _id(
                "Id of one run report (report_id from an earlier srm.history.list). "
                "When set, the answer is that run with its errors and warnings.",
                "<report-id>",
            ),
            "start_time": {
                "type": "string",
                "format": "date-time",
                "description": (
                    "Start of the time range, ISO 8601 (for example "
                    "<yyyy-mm-dd>T00:00:00Z). Default: 7 days ago."
                ),
            },
            "end_time": {
                "type": "string",
                "format": "date-time",
                "description": "End of the time range, ISO 8601. Default: now.",
            },
        },
        ["pairing_id", "plan_id"],
        {"pairing_id": "<pairing-id>", "plan_id": "<plan-id>"},
    ),
    response_schema=HISTORY_LIST_RESPONSE,
    group_key=GROUP_PLANS,
    tags=(*_TAGS, "recovery-plan", "history"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_call": (
            "Call to see when a plan last ran a test, cleanup, recovery or reprotect, "
            "and whether the run ended with errors or warnings."
        ),
        "output_shape": (
            "{pairing_id, plan_id, total, items: [{report_id, plan_name, operation_type "
            "(TEST, CLEANUP, FAILOVER, REPROTECT), result_state (SUCCESS, WARNINGS, "
            "ERRORS, CANCELLED), username, start_time, duration_ms, duration, error_count, "
            "warning_count}]}. With report_id each row also has errors and warnings "
            "(texts). SRM builds the list in a task; the read waits up to 60 seconds."
        ),
        "next_step": (
            "For a run with errors or warnings call srm.history.list again with its "
            "report_id to read the texts."
        ),
        "result_scalars": {"keys": ["pairing_id", "plan_id", "total"]},
    },
)

RECOVERY_TYPED_OPS: tuple[SrmTypedOp, ...] = (
    _PAIRING_LIST,
    _REMOTE_LOGIN,
    _PLAN_LIST,
    _PLAN_GET,
    _GROUP_LIST,
    _ISSUE_LIST,
    _HISTORY_LIST,
)

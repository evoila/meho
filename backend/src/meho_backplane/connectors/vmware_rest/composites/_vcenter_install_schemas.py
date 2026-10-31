# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and AI guidance of ``vcenter.install`` and ``vcenter.deployment.answer`` (#4394).

Both run on the new appliance. Step 1 (``vcenter.appliance.deploy``) keeps
its schemas in :mod:`._vcenter_deploy_schemas`, and the status read
``vmware.vcenter.deployment.status`` in
:mod:`..typed_ops_vcenter_deployment_status`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vmware_rest.composites._vcenter_deploy_schemas import (
    HOST_NAME_PATTERN,
    ISSUE_SCHEMA,
    secret_ref_fields,
)
from meho_backplane.connectors.vmware_rest.typed_ops_vcenter_deployment_status import (
    QUESTION_ROW_SCHEMA,
)
from meho_backplane.connectors.vmware_rest.vcenter_deployment import MEHO_STATES, VENDOR_STATES

__all__ = [
    "DEPLOYMENT_ANSWER_LLM_INSTRUCTIONS",
    "DEPLOYMENT_ANSWER_PARAMETER_SCHEMA",
    "DEPLOYMENT_ANSWER_RESPONSE_SCHEMA",
    "INSTALL_LLM_INSTRUCTIONS",
    "INSTALL_PARAMETER_SCHEMA",
    "INSTALL_RESPONSE_SCHEMA",
]

# ``$(?!\n)`` ends a pattern: Python's ``re.search`` lets a bare ``$`` match
# before a trailing newline.
_DOMAIN_PATTERN: Final = r"^[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$(?!\n)"
_SHA1_THUMBPRINT_PATTERN: Final = r"^([0-9A-Fa-f]{2}:){19}[0-9A-Fa-f]{2}$(?!\n)"

_STEP_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step": {"type": "string", "description": "Name of the step."},
        "result": {
            "type": "string",
            "enum": ["done", "failed", "unknown", "not_run"],
            "description": "done, failed, unknown (sent, but the reply was lost) or not_run.",
        },
        "error": {"type": ["string", "null"], "description": "The error of a failed step."},
    },
    "required": ["step", "result", "error"],
}

_STATUS_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": "The status read that follows the setup.",
    "properties": {
        "op_id": {"type": "string", "description": "vmware.vcenter.deployment.status."},
        "target": {"type": "string", "description": "The appliance target to read."},
    },
    "required": ["op_id", "target"],
}

# ===========================================================================
# vcenter.install
# ===========================================================================

INSTALL_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "standalone": {
            "type": "object",
            "description": "Set up a new SSO domain. Give standalone or replicated, not both.",
            "properties": {
                "sso_domain_name": {
                    "type": "string",
                    "pattern": _DOMAIN_PATTERN,
                    "description": "Name of the new SSO domain (default vsphere.local).",
                }
            },
            "additionalProperties": False,
        },
        "replicated": {
            "type": "object",
            "description": "Join the SSO domain of an existing vCenter (the partner).",
            "properties": {
                "partner_hostname": {
                    "type": "string",
                    "pattern": HOST_NAME_PATTERN,
                    "description": "Host name or IP address of the partner vCenter.",
                },
                "https_port": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 65535,
                    "description": "HTTPS port of the partner vCenter (default 443).",
                },
                "ssl_thumbprint": {
                    "type": "string",
                    "pattern": _SHA1_THUMBPRINT_PATTERN,
                    "description": "SHA-1 thumbprint of the partner's certificate (AA:BB:...).",
                },
                "ssl_verify": {
                    "type": "boolean",
                    "description": "Check the partner's certificate (default true).",
                },
            },
            "required": ["partner_hostname"],
            "additionalProperties": False,
        },
        **secret_ref_fields(
            "sso_admin_password", "the SSO administrator password (new or of the partner domain)"
        ),
        "ceip_enabled": {
            "type": "boolean",
            "description": "Join the vendor's data sharing program (CEIP). Default false.",
        },
        "auto_answer": {
            "type": "boolean",
            "description": (
                "Let the appliance take the default answer of any question (default false: "
                "the setup waits, and you answer with vcenter.deployment.answer)."
            ),
        },
    },
    "required": ["sso_admin_password_secret_ref"],
    "oneOf": [{"required": ["standalone"]}, {"required": ["replicated"]}],
    "additionalProperties": False,
    "examples": [
        {
            "standalone": {"sso_domain_name": "vsphere.local"},
            "sso_admin_password_secret_ref": "<vault-path-of-the-sso-password>",
        }
    ],
}

_CHECK_SCHEMA: dict[str, Any] = {
    "type": ["object", "null"],
    "description": "The vendor install check; null when it did not run.",
    "properties": {
        "status": {
            "type": ["string", "null"],
            "enum": ["SUCCESS", "FAILED", None],
            "description": "Vendor check status.",
        },
        "errors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Check errors, with the fix when the vendor gives it.",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Check warnings.",
        },
        "info": {"type": "array", "items": {"type": "string"}, "description": "Check notes."},
    },
}

INSTALL_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "accepted",
                "already_running",
                "waiting_for_answer",
                "already_done",
                "failed_before",
                "not_ready",
                "unknown_state",
                "not_reachable",
                "check_failed",
                "start_failed",
                "start_unknown",
            ],
            "description": (
                "accepted: the setup runs (not done). already_running / waiting_for_answer: "
                "a setup runs; nothing started. check_failed: nothing started. start_unknown: "
                "the reply was lost; read the status before you try again."
            ),
        },
        "mode": {
            "type": "string",
            "enum": ["new_sso_domain", "join_sso_domain"],
            "description": "New SSO domain or a join to the partner's domain.",
        },
        "state": {
            "type": ["string", "null"],
            "enum": [*MEHO_STATES, None],
            "description": "Appliance state read before the start.",
        },
        "vendor_state": {
            "type": ["string", "null"],
            "enum": [*VENDOR_STATES, None],
            "description": "The vendor's appliance state.",
        },
        "check": _CHECK_SCHEMA,
        "job": {
            "type": "object",
            "description": "The setup job. The vendor gives no job id: the target names it.",
            "properties": {
                "kind": {"type": "string", "description": "install."},
                "target": {"type": "string", "description": "The appliance target."},
            },
            "required": ["kind", "target"],
        },
        "status_read": _STATUS_READ_SCHEMA,
        "steps": {
            "type": "array",
            "items": _STEP_ROW_SCHEMA,
            "description": "read_state, read_sso_password, check, start: what ran.",
        },
        "issues": {"type": "array", "items": ISSUE_SCHEMA, "description": "What went wrong."},
        "guidance": {"type": "string", "description": "What to do next, in plain words."},
    },
    "required": ["status", "mode", "check", "job", "status_read", "steps", "issues"],
}

INSTALL_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call as step 2 of a new vCenter, on the target of the new appliance (port 5480, "
        "root login), when vmware.vcenter.deployment.status says ready. It checks the spec, "
        "then starts the setup: a new SSO domain (standalone) or a join to the SSO domain of "
        "an existing vCenter (replicated). Use MEHO's async mode (async: true), so the "
        "answer with the start is kept under a run handle even when the reply is lost."
    ),
    "preconditions": (
        "The appliance target answers on port 5480 with user root and the root password of "
        "step 1. The SSO administrator password is in Vault at "
        "sso_admin_password_secret_ref (for a join: the partner domain's password). The "
        "host name of the appliance resolves in DNS."
    ),
    "parameter_hints": {
        "standalone": "New SSO domain. Leave sso_domain_name out for vsphere.local.",
        "replicated": "Join: partner_hostname of a running vCenter in that SSO domain.",
        "ceip_enabled": "Leave false unless the owner wants the vendor's data sharing.",
    },
    "output_shape": (
        "{status, mode, state, vendor_state, check: {status, errors, warnings, info}, "
        "job: {kind, target}, status_read: {op_id, target}, steps: [{step, result, error}], "
        "issues, guidance}. accepted never means done. Never a password."
    ),
    "next_step": (
        "accepted: read vmware.vcenter.deployment.status on the same target every few "
        "minutes until done or failed; answer a question with "
        "vmware.composite.vcenter.deployment.answer. start_unknown or a lost reply: read the "
        "status first; running means the start worked, ready means call this again. "
        "check_failed: fix what check.errors says. done: add the new vCenter as a target on "
        "port 443."
    ),
    "result_scalars": {"keys": ["status", "mode", "state", "vendor_state", "guidance"]},
}

# ===========================================================================
# vcenter.deployment.answer
# ===========================================================================

DEPLOYMENT_ANSWER_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 256,
            "description": "Id of the open question, from vmware.vcenter.deployment.status.",
        },
        "answer": {
            "type": "string",
            "minLength": 1,
            "maxLength": 64,
            "description": "One of the question's possible_answers, for example yes.",
        },
    },
    "required": ["question_id", "answer"],
    "additionalProperties": False,
    "examples": [{"question_id": "<question-id>", "answer": "<one-of-possible_answers>"}],
}

DEPLOYMENT_ANSWER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": [
                "accepted",
                "no_question",
                "unknown_question",
                "answer_not_allowed",
                "answer_failed",
                "answer_unknown",
            ],
            "description": (
                "accepted: the setup goes on (not done). no_question, unknown_question, "
                "answer_not_allowed: nothing was sent. answer_unknown: the reply was lost."
            ),
        },
        "question_id": {"type": "string", "description": "The question answered."},
        "answer": {"type": "string", "description": "The answer sent."},
        "question": {
            **QUESTION_ROW_SCHEMA,
            "type": ["object", "null"],
            "description": "The open question as read; null when it was not found.",
        },
        "status_read": _STATUS_READ_SCHEMA,
        "steps": {
            "type": "array",
            "items": _STEP_ROW_SCHEMA,
            "description": "read_state, read_question, answer: what ran.",
        },
        "issues": {"type": "array", "items": ISSUE_SCHEMA, "description": "What went wrong."},
        "guidance": {"type": "string", "description": "What to do next, in plain words."},
    },
    "required": ["status", "question_id", "answer", "status_read", "steps", "issues"],
}

DEPLOYMENT_ANSWER_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call when vmware.vcenter.deployment.status on the new appliance says "
        "waiting_for_answer, to answer one open question so the setup goes on. Use MEHO's "
        "async mode (async: true), so the answer is kept under a run handle."
    ),
    "preconditions": (
        "The appliance waits for an answer, question_id is open, and answer is one of its "
        "possible_answers. MEHO checks all three before it sends anything."
    ),
    "parameter_hints": {
        "question_id": "The id of a row in questions of the status read.",
        "answer": "Exactly one of possible_answers of that row.",
    },
    "output_shape": (
        "{status, question_id, answer, question: {id, text, type, default_answer, "
        "possible_answers}, status_read: {op_id, target}, steps: [{step, result, error}], "
        "issues, guidance}."
    ),
    "next_step": (
        "accepted: read vmware.vcenter.deployment.status until done or failed. "
        "answer_unknown: read the status first; when the question is still open, answer "
        "again. unknown_question or answer_not_allowed: read the status for the open "
        "question and its answers."
    ),
    "result_scalars": {"keys": ["status", "question_id", "answer", "guidance"]},
}

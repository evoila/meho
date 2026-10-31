# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and response schemas of the Cloud Builder operations.

A bring-up spec holds passwords. A call never sends them as values: the
``spec`` schema refuses text in any field whose name ends with ``password``,
``passwd``, ``secret``, ``license``, ``licenseKey``, ``privateKey`` or
``passphrase``, and the caller names each such value in ``secrets`` as a
Vault reference. The check runs before the call waits for approval, so no
password is stored with the approval request.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.connectors.vcf_cloud_builder._jobs import (
    DEFAULT_SECRET_KEY,
    SDDC_STATES,
    VALIDATION_STATES,
)

__all__ = [
    "ABOUT_PARAMS",
    "ABOUT_RESPONSE",
    "CHANGE_RESPONSE",
    "REPORT_PARAMS",
    "REPORT_RESPONSE",
    "SDDC_RETRY_PARAMS",
    "SDDC_START_PARAMS",
    "SDDC_STATUS_PARAMS",
    "SDDC_STATUS_RESPONSE",
    "SECRET_FIELD_PATTERN",
    "SPEC_EXAMPLE",
    "VALIDATION_START_PARAMS",
    "VALIDATION_STATUS_PARAMS",
    "VALIDATION_STATUS_RESPONSE",
]

#: Field names that must not hold text in a spec (searched, case-insensitive).
SECRET_FIELD_PATTERN: Final = (
    "(?i)(password|passwd|secret|licen[cs]e|licen[cs]ekey|privatekey|passphrase)$"
)

#: A short placeholder spec. A real Cloud Builder spec has many more fields.
SPEC_EXAMPLE: Final[dict[str, Any]] = {
    "sddcId": "<sddc-id>",
    "ntpServers": ["<ntp-server>"],
    "dnsSpec": {"subdomain": "<dns-subdomain>", "nameserver": "<dns-server-ip>"},
    "vcenterSpec": {"vcenterHostname": "<vcenter-hostname>"},
    "sddcManagerSpec": {
        "hostname": "<sddc-manager-hostname>",
        "rootUserCredentials": {"username": "root"},
    },
    "hostSpecs": [{"hostname": "<esx-host-name>", "credentials": {"username": "root"}}],
    "networkSpecs": [
        {"networkType": "MANAGEMENT", "vlanId": "<vlan-id>", "subnet": "<subnet-cidr>"}
    ],
}

_SECRETS_EXAMPLE: Final[list[dict[str, str]]] = [
    {"path": "/sddcManagerSpec/rootUserCredentials/password", "secret_ref": "<vault-path>"},
    {"path": "/hostSpecs/0/credentials/password", "secret_ref": "<vault-path>"},
]

_NO_SECRET_DEFS: Final[dict[str, Any]] = {
    "noSecretText": {
        "description": (
            "Any JSON value. A field whose name ends with password, secret, license, "
            "licenseKey, privateKey or passphrase must not hold text."
        ),
        "patternProperties": {
            SECRET_FIELD_PATTERN: {
                "not": {"type": "string", "minLength": 1},
                "$ref": "#/$defs/noSecretText",
            }
        },
        "additionalProperties": {"$ref": "#/$defs/noSecretText"},
        "items": {"$ref": "#/$defs/noSecretText"},
    }
}


def _spec_field(required_text: str) -> dict[str, Any]:
    return {
        "type": "object",
        "description": (
            f"The Cloud Builder bring-up spec (SddcSpec) as JSON, {required_text}. Leave out "
            "every password and licence key; name them in secrets."
        ),
        "$ref": "#/$defs/noSecretText",
    }


_SECRETS_FIELD: Final[dict[str, Any]] = {
    "type": "array",
    "description": (
        "The secret values of the spec, each read from Vault and put into the spec "
        "before it is sent. Only with spec."
    ),
    "items": {
        "type": "object",
        "description": "One secret value of the spec and where it comes from.",
        "properties": {
            "path": {
                "type": "string",
                "pattern": "^/",
                "description": (
                    "Where the value goes in the spec, as a JSON Pointer, for example "
                    "/hostSpecs/0/credentials/password. The parent object must exist."
                ),
            },
            "secret_ref": {
                "type": "string",
                "minLength": 1,
                "description": "The Vault path (KV v2) that holds the value.",
            },
            "secret_key": {
                "type": "string",
                "minLength": 1,
                "default": DEFAULT_SECRET_KEY,
                "description": "The key in the Vault secret. Default: password.",
            },
            "secret_mount": {
                "type": "string",
                "minLength": 1,
                "default": "secret",
                "description": "The Vault KV mount. Default: secret.",
            },
        },
        "required": ["path", "secret_ref"],
        "additionalProperties": False,
    },
}


def _start_params() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "spec": _spec_field("sent as is apart from the secrets"),
            "secrets": _SECRETS_FIELD,
        },
        "required": ["spec"],
        "additionalProperties": False,
        "$defs": _NO_SECRET_DEFS,
        "examples": [{"spec": SPEC_EXAMPLE, "secrets": _SECRETS_EXAMPLE}],
    }


VALIDATION_START_PARAMS: Final = _start_params()
SDDC_START_PARAMS: Final = _start_params()

SDDC_RETRY_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "minLength": 1,
            "description": "The bring-up id (from installer.cb.sddc.start or .status).",
        },
        "spec": _spec_field(
            "only when you fixed the spec; without it Cloud Builder retries the stored spec"
        ),
        "secrets": _SECRETS_FIELD,
    },
    "required": ["id"],
    "dependentRequired": {"secrets": ["spec"]},
    "additionalProperties": False,
    "$defs": _NO_SECRET_DEFS,
    "examples": [{"id": "<bringup-id>"}],
}


def _status_params(what: str, example: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "minLength": 1,
                "description": f"The {what} id. Leave it out to list every {what}.",
            }
        },
        "additionalProperties": False,
        "examples": [{"id": example}, {}],
    }


VALIDATION_STATUS_PARAMS: Final = _status_params("validation", "<validation-id>")
SDDC_STATUS_PARAMS: Final = _status_params("bring-up", "<bringup-id>")

ABOUT_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}

REPORT_PARAMS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "sddc_id": {
            "type": "string",
            "minLength": 1,
            "description": "The bring-up id, for the bring-up detail report.",
        },
        "format": {
            "type": "string",
            "enum": ["pdf", "csv"],
            "default": "pdf",
            "description": "The file format of the bring-up detail report. Default: pdf.",
        },
        "validation_id": {
            "type": "string",
            "minLength": 1,
            "description": "The validation id, for the validation report (always PDF).",
        },
    },
    "oneOf": [
        {"required": ["sddc_id"], "not": {"required": ["validation_id"]}},
        {
            "required": ["validation_id"],
            "not": {"anyOf": [{"required": ["sddc_id"]}, {"required": ["format"]}]},
        },
    ],
    "additionalProperties": False,
    "examples": [
        {"sddc_id": "<bringup-id>", "format": "pdf"},
        {"validation_id": "<validation-id>"},
    ],
}


def _text(description: str) -> dict[str, Any]:
    return {"type": ["string", "null"], "description": description}


def _count(description: str) -> dict[str, Any]:
    return {"type": "integer", "description": description}


ABOUT_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": _text("Id of the bring-up service."),
        "name": _text("Name of the bring-up service."),
        "version": _text("Cloud Builder version, for example 5.2.0.0-24108943."),
        "status": _text("Status of the bring-up service."),
    },
}

_NOT_REACHABLE_FIELDS: Final[dict[str, Any]] = {
    "target": _text("The target name (only when not reachable)."),
    "reason": _text("timeout, tls_verify_failed or connect_failed (only when not reachable)."),
    "guidance": _text("What to do next (only when not reachable)."),
    "checked_at": _text("When MEHO read the state (UTC)."),
}

VALIDATION_STATUS_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": _text("Validation id."),
        "description": _text("What the validation checks."),
        "state": {"enum": list(VALIDATION_STATES), "description": "MEHO's state word."},
        "vendor_state": _text("Cloud Builder executionStatus, for example IN_PROGRESS."),
        "result": _text("Cloud Builder resultStatus: SUCCEEDED, FAILED, FAILED_WITH_WARNINGS."),
        "checks_total": _count("Number of checks."),
        "checks_finished": _count("Checks that ended."),
        "checks_failed": _count("Checks that failed."),
        "error": _text("The vendor's error texts of the failed checks."),
        "checks": {
            "type": "array",
            "description": "One row per check.",
            "items": {
                "type": "object",
                "properties": {
                    "description": _text("What the check checks."),
                    "severity": _text("ERROR, WARNING or INFO."),
                    "result": _text("The check's resultStatus."),
                    "error": _text("The vendor's error text."),
                },
            },
        },
        "validations": {
            "type": "array",
            "description": "Without an id: one row per validation (id, description, state, ...).",
            "items": {"type": "object", "properties": {"id": _text("Validation id.")}},
        },
        **_NOT_REACHABLE_FIELDS,
    },
}

SDDC_STATUS_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "id": _text("Bring-up id."),
        "name": _text("Bring-up task name."),
        "state": {"enum": list(SDDC_STATES), "description": "MEHO's state word."},
        "vendor_state": _text("Cloud Builder status, for example IN_PROGRESS."),
        "created_at": _text("When the bring-up started."),
        "tasks_total": _count("Number of tasks."),
        "tasks_done": _count("Tasks that ended well."),
        "tasks_running": _count("Tasks that run now."),
        "tasks_failed": _count("Tasks that failed."),
        "failed_task": _text("Name of the first failed task."),
        "error": _text("The vendor's error text of the first failed task."),
        "tasks": {
            "type": "array",
            "description": "One row per task of the bring-up.",
            "items": {
                "type": "object",
                "properties": {
                    "name": _text("Task name."),
                    "description": _text("What the task does."),
                    "status": _text("The task's status, for example IN_PROGRESS."),
                    "updated_at": _text("Last change of the task."),
                    "error": _text("The vendor's error text."),
                },
            },
        },
        "bringups": {
            "type": "array",
            "description": "Without an id: one row per bring-up (id, name, state, ...).",
            "items": {"type": "object", "properties": {"id": _text("Bring-up id.")}},
        },
        **_NOT_REACHABLE_FIELDS,
    },
}

CHANGE_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "status": {
            "enum": [
                "accepted",
                "already_running",
                "invalid_input",
                "not_reachable",
                "check_failed",
                "start_failed",
                "start_unknown",
            ],
            "description": "What happened. accepted means started, not done.",
        },
        "job": {
            "type": "object",
            "description": "The vendor job.",
            "properties": {
                "kind": _text("validation or bringup."),
                "id": _text("The vendor's id of the job."),
                "vendor_state": _text("The vendor's state when MEHO last saw the job."),
                "target": _text("The target name."),
            },
        },
        "status_read": {
            "type": "object",
            "description": "The read that follows the job.",
            "properties": {
                "op_id": _text("The status operation."),
                "params": {"type": "object", "description": "Its parameters."},
            },
        },
        "steps": {
            "type": "array",
            "description": "Each step: done, failed, unknown (reply lost) or not_run.",
            "items": {
                "type": "object",
                "properties": {
                    "step": _text("read_secrets, check_running or start."),
                    "result": _text("done, failed, unknown or not_run."),
                    "error": _text("The error of the step."),
                },
            },
        },
        "error": _text("The vendor's error text, when the call failed."),
        "guidance": _text("What to do next."),
    },
}

REPORT_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "report": _text("bringup_detail or validation."),
        "id": _text("The bring-up or validation id."),
        "format": _text("pdf or csv."),
        "content_type": _text("The media type Cloud Builder sent."),
        "size_bytes": _count("Size of the file."),
        "sha256": _text("SHA-256 of the file."),
        "encoding": _text("Always base64."),
        "content": _text("The file, base64-encoded."),
    },
}

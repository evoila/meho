# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema + ``llm_instructions`` for ``vault.auth.userpass.write``.

Split out of :mod:`meho_backplane.connectors.vault.ops_auth_write_schemas`
to keep both files small. The op takes its password from one of three
sources (the request, a KV path, or a password MEHO makes); see
:mod:`meho_backplane.connectors.vault.userpass_password`.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vault.ops_auth_write_schemas import (
    _PASSWORD_PROPERTY,
    _USERNAME_PROPERTY,
    _mount_hint,
    _mount_property,
)

__all__ = [
    "VAULT_AUTH_USERPASS_WRITE_LLM_INSTRUCTIONS",
    "VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_WRITE_RESPONSE_SCHEMA",
]

VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": _USERNAME_PROPERTY,
        "password": {
            **_PASSWORD_PROPERTY,
            "description": (
                _PASSWORD_PROPERTY["description"]
                + " Give exactly one of password, password_secret_ref and "
                "generate_password. Prefer the other two: then nobody passes "
                "the password through an agent or a chat."
            ),
        },
        "password_secret_ref": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "KV-v2 path (no mount, no 'data/') in this Vault that holds "
                "the password. MEHO reads it and sets it on the login. The "
                "path is not secret; the value is never shown."
            ),
        },
        "password_secret_mount": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "default": "secret",
            "description": "KV-v2 mount of password_secret_ref. Default 'secret'.",
        },
        "password_secret_key": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "default": "password",
            "description": "Field of the KV secret that holds the password. Default 'password'.",
        },
        "generate_password": {
            "type": "object",
            "description": (
                "MEHO makes a random password, writes it to the KV-v2 path "
                "first, and then sets it on the login. The result names only "
                "the path. Safe to run twice: when the login exists and the "
                "path holds a password, nothing changes (unless rotate is true)."
            ),
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": "KV-v2 path (no mount, no 'data/') that stores the password.",
                },
                "mount": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "default": "secret",
                    "description": "KV-v2 mount. Default 'secret'.",
                },
                "field": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "default": "password",
                    "description": "Field that holds the password. Default 'password'.",
                },
                "length": {
                    "type": "integer",
                    "minimum": 16,
                    "maximum": 128,
                    "default": 32,
                    "description": "Length of the password. Default 32.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "rotate": {
            "type": "boolean",
            "default": False,
            "description": (
                "Only with generate_password. true makes a new password even "
                "when the path already holds one. Default false."
            ),
        },
        "token_policies": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Policies bound to tokens this user receives at login -- "
                "the privilege assignment. Omit to leave unchanged on an "
                "update (Vault treats the field as optional)."
            ),
        },
        "mount": _mount_property("userpass"),
    },
    "required": ["username"],
    "oneOf": [
        {"required": ["password"]},
        {"required": ["password_secret_ref"]},
        {"required": ["generate_password"]},
    ],
    "additionalProperties": False,
}

VAULT_AUTH_USERPASS_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": {"type": "string", "description": "The user created or updated."},
        "mount": {"type": "string", "description": "The mount the user lives on."},
        "token_policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Policies assigned to the user (echoed from the request).",
        },
        "written": {
            "type": "boolean",
            "description": (
                "True when MEHO changed the login. False when nothing changed "
                "(a repeat of generate_password)."
            ),
        },
        "credential_source": {
            "type": "string",
            "enum": ["request", "kv_path", "generated"],
            "description": "Where the password came from.",
        },
        "credential_generated": {
            "type": "boolean",
            "description": "True when this call made a new password and stored it.",
        },
        "stored_at": {
            "type": "object",
            "description": (
                "Where the password is stored (mount, path, field). Present "
                "unless the password came in the request. Never the value."
            ),
            "properties": {
                "mount": {"type": "string", "description": "KV-v2 mount."},
                "path": {"type": "string", "description": "KV-v2 path."},
                "field": {"type": "string", "description": "Field that holds the password."},
            },
        },
    },
    "required": ["username", "written"],
    "description": (
        "Confirms the create/update. NEVER carries the password -- the "
        "value-free echo reports the username, mount, assigned policies "
        "and where a stored password lives."
    ),
}

VAULT_AUTH_USERPASS_WRITE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Create a new userpass user or update an existing one's password "
        "and/or token policies. Use when provisioning a human/service "
        "login on the userpass backend. DANGEROUS, requires approval: "
        "binding token_policies is a privilege assignment. Prefer "
        "generate_password (MEHO makes the password and stores it in KV) or "
        "password_secret_ref (MEHO reads it from KV): then no one passes a "
        "password through an agent or a chat. The password is SECRET -- it "
        "is redacted from the audit row and the broadcast feed "
        "(credential_write) and never echoed back."
    ),
    "parameter_hints": {
        "username": "Required. The userpass username, no mount prefix.",
        "password": (
            "One of password, password_secret_ref, generate_password. The "
            "password itself (secret; redacted from audit/broadcast)."
        ),
        "password_secret_ref": (
            "One of the three. KV-v2 path that holds the password; MEHO reads "
            "it. Also password_secret_mount (default 'secret') and "
            "password_secret_key (default 'password')."
        ),
        "generate_password": (
            "One of the three. {path, mount?, field?, length?}: MEHO makes a "
            "random password and writes it to that KV path first. A repeat "
            "changes nothing unless rotate is true."
        ),
        "rotate": "Only with generate_password. true makes a new password.",
        "token_policies": (
            "Optional. Policy names bound to the user's tokens (privilege "
            "assignment). Omit to leave unchanged on an update."
        ),
        "mount": _mount_hint("userpass"),
    },
    "output_shape": (
        "On success: {'username': ..., 'mount': ..., 'token_policies': "
        "[...], 'written': bool, 'credential_source': ..., "
        "'credential_generated': bool, 'stored_at': {mount, path, "
        "field}} -- never the password. On failure: a "
        "connector_error OperationResult with extras.exception_class "
        "naming the failure class ('VaultAuthBackendNotMountedError' when "
        "userpass is not enabled at the mount)."
    ),
    # Credential-class ops get no default scalar copy when token_policies
    # spills; these value-free fields stay inline (#3633).
    "result_scalars": {
        "keys": ["username", "mount", "written", "credential_source", "credential_generated"]
    },
}

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema + ``llm_instructions`` constants for the Vault auth ops.

Split out of :mod:`meho_backplane.connectors.vault.ops_auth` so the
handler/registration module stays focused on behaviour. The blobs here
are pure data -- JSON Schema 2020-12 ``parameter_schema`` /
``response_schema`` documents and the structured ``llm_instructions``
payload the meta-tools (G0.6-T8) inline verbatim when an LLM is
choosing an op. The split keeps both modules well under the file-size
budget and lets the schemas be imported by tests / the CLI verb tree
(Task #550) without pulling in the hvac handler chain.

Mirrors the schema/``llm_instructions`` shape of the existing
``vault.kv.read`` registration (Task #547 requirement): when-to-use
prose + a parameter-hint block + an output-shape sketch, plus
``minLength=1`` / ``pattern="\\S"`` / ``additionalProperties=False``
input discipline.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "VAULT_AUTH_APPROLE_LIST_LLM_INSTRUCTIONS",
    "VAULT_AUTH_APPROLE_LIST_PARAMETER_SCHEMA",
    "VAULT_AUTH_APPROLE_LIST_RESPONSE_SCHEMA",
    "VAULT_AUTH_APPROLE_READ_LLM_INSTRUCTIONS",
    "VAULT_AUTH_APPROLE_READ_PARAMETER_SCHEMA",
    "VAULT_AUTH_APPROLE_READ_RESPONSE_SCHEMA",
    "VAULT_AUTH_USERPASS_LIST_LLM_INSTRUCTIONS",
    "VAULT_AUTH_USERPASS_LIST_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_LIST_RESPONSE_SCHEMA",
    "VAULT_AUTH_USERPASS_READ_LLM_INSTRUCTIONS",
    "VAULT_AUTH_USERPASS_READ_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_READ_RESPONSE_SCHEMA",
]


def _mount_property(default: str) -> dict[str, Any]:
    """Build the shared optional ``mount`` schema property.

    ``mount`` is optional with a per-op default so non-default mounts
    (``auth/userpass-prod``) work without a code change. ``pattern="\\S"``
    rejects a whitespace-only override; the enclosing schema's
    ``additionalProperties=False`` turns a typo (``{"moutn": "x"}``)
    into a clear validation error instead of a silent default-mount
    dispatch.
    """
    return {
        "type": "string",
        "minLength": 1,
        "pattern": "\\S",
        "default": default,
        "description": (
            f"Auth-method mount path (no leading slash, no 'auth/' prefix), "
            f"default {default!r}. Override only when the backend was "
            f"enabled at a non-default path (vault auth enable -path=...)."
        ),
    }


def _mount_hint(default: str) -> str:
    return f"Optional. Defaults to {default!r}. Supply only for a non-default mount path."


# --- userpass.list ---------------------------------------------------------

VAULT_AUTH_USERPASS_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"mount": _mount_property("userpass")},
    "required": [],
    "additionalProperties": False,
}

VAULT_AUTH_USERPASS_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Configured userpass usernames at the mount.",
        },
    },
    "required": ["keys"],
}

VAULT_AUTH_USERPASS_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "List every username configured on a Vault userpass auth mount. "
        "Use when the operator asks 'who can log in with userpass?' or "
        "wants the userpass roster before reading one user's policies. "
        "Read-only -- never mutates the auth backend."
    ),
    "parameter_hints": {"mount": _mount_hint("userpass")},
    "output_shape": (
        "On success: {'keys': [<username>, ...]} (empty list when the "
        "mount has no users). On failure: a connector_error "
        "OperationResult with extras.exception_class set to "
        "'VaultAuthBackendNotMountedError' (userpass not enabled at the "
        "mount) or a Vault-side error class."
    ),
}


# --- userpass.read ---------------------------------------------------------

VAULT_AUTH_USERPASS_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "userpass username to read (no mount prefix). Forwarded "
                "verbatim to client.auth.userpass.read_user(username=...)."
            ),
        },
        "mount": _mount_property("userpass"),
    },
    "required": ["username"],
    "additionalProperties": False,
}

VAULT_AUTH_USERPASS_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "token_policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Policies attached to tokens this user receives.",
        },
        "token_ttl": {
            "type": "integer",
            "description": "Default token TTL in seconds (0 = system default).",
        },
        "token_max_ttl": {
            "type": "integer",
            "description": "Maximum token TTL in seconds (0 = system default).",
        },
    },
    "description": (
        "The userpass user's config object (token_policies, token_ttl, "
        "token_max_ttl, token_bound_cidrs, token_type, ...). Returned "
        "verbatim from Vault's data envelope; extra keys are "
        "Vault-version dependent."
    ),
}

VAULT_AUTH_USERPASS_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Read one userpass user's configuration -- attached policies and "
        "token TTL/CIDR bindings. Use when the operator names a specific "
        "user ('what policies does the ci user have?'). Read-only. Does "
        "NOT return the user's password (Vault never exposes it)."
    ),
    "parameter_hints": {
        "username": "Required. The userpass username, no mount prefix.",
        "mount": _mount_hint("userpass"),
    },
    "output_shape": (
        "On success: the user's config dict (token_policies, token_ttl, "
        "token_max_ttl, token_bound_cidrs, ...). On failure: a "
        "connector_error OperationResult; extras.exception_class is "
        "'VaultAuthBackendNotMountedError' when userpass is not enabled, "
        "'InvalidPath' when the backend is mounted but the user does "
        "not exist."
    ),
}


# --- approle.list ----------------------------------------------------------

VAULT_AUTH_APPROLE_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"mount": _mount_property("approle")},
    "required": [],
    "additionalProperties": False,
}

VAULT_AUTH_APPROLE_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Configured AppRole role names at the mount.",
        },
    },
    "required": ["keys"],
}

VAULT_AUTH_APPROLE_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "List every role name on a Vault approle auth mount. Use when "
        "the operator asks 'which AppRoles exist?' or wants the role "
        "roster before reading one role's policies/TTLs. Read-only -- "
        "never mutates the auth backend and never generates a secret-id."
    ),
    "parameter_hints": {"mount": _mount_hint("approle")},
    "output_shape": (
        "On success: {'keys': [<role-name>, ...]} (empty list when the "
        "mount has no roles). On failure: a connector_error "
        "OperationResult with extras.exception_class set to "
        "'VaultAuthBackendNotMountedError' (approle not enabled) or a "
        "Vault-side error class."
    ),
}


# --- approle.read ----------------------------------------------------------

VAULT_AUTH_APPROLE_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "AppRole role name to read (no mount prefix). Forwarded "
                "verbatim to client.auth.approle.read_role(role_name=...)."
            ),
        },
        "mount": _mount_property("approle"),
    },
    "required": ["role_name"],
    "additionalProperties": False,
}

VAULT_AUTH_APPROLE_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "token_policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Policies attached to tokens this role issues.",
        },
        "token_ttl": {
            "type": "integer",
            "description": "Default token TTL in seconds.",
        },
        "token_max_ttl": {
            "type": "integer",
            "description": "Maximum token TTL in seconds.",
        },
        "secret_id_ttl": {
            "type": "integer",
            "description": "TTL of secret-ids issued for this role (seconds).",
        },
        "bind_secret_id": {
            "type": "boolean",
            "description": "Whether a secret-id is required at login.",
        },
    },
    "description": (
        "The AppRole's config object (token_policies, token_ttl, "
        "token_max_ttl, secret_id_ttl, secret_id_num_uses, "
        "bind_secret_id, ...). Returned verbatim from Vault's data "
        "envelope; secret-id *generation* is a separate write op "
        "deliberately out of scope for v0.2."
    ),
}

VAULT_AUTH_APPROLE_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Read one AppRole's configuration -- attached policies, token "
        "and secret-id TTLs, bind_secret_id. Use when the operator "
        "names a specific role ('what policies does the deploy role "
        "grant?'). Read-only -- returns config only, never a secret-id "
        "(generation is out of scope for v0.2)."
    ),
    "parameter_hints": {
        "role_name": "Required. The AppRole role name, no mount prefix.",
        "mount": _mount_hint("approle"),
    },
    "output_shape": (
        "On success: the role's config dict (token_policies, token_ttl, "
        "secret_id_ttl, bind_secret_id, ...). On failure: a "
        "connector_error OperationResult; extras.exception_class is "
        "'VaultAuthBackendNotMountedError' when approle is not enabled, "
        "'InvalidPath' when the backend is mounted but the role does "
        "not exist."
    ),
}

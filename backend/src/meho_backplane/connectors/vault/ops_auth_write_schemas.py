# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema + ``llm_instructions`` for the Vault auth *write* ops.

Split out of :mod:`meho_backplane.connectors.vault.ops_auth_write` so the
handler/registration module stays focused on behaviour and both modules
stay under the file-size budget. The blobs here are pure data --
JSON Schema 2020-12 ``parameter_schema`` / ``response_schema`` documents
and the structured ``llm_instructions`` payload the meta-tools (G0.6-T8)
inline verbatim when an LLM is choosing an op.

Mirrors the schema/``llm_instructions`` shape of the read-side
:mod:`meho_backplane.connectors.vault.ops_auth_schemas` -- ``minLength=1``
/ ``pattern="\\S"`` / ``additionalProperties=False`` input discipline,
a shared optional ``mount`` property, and a when-to-use + parameter-hint
+ output-shape ``llm_instructions`` block per op.

Secret-material discipline (G3.15-T3, #1411): the userpass password
properties and the ``generate_secret_id`` response carry secret
material. The redaction is enforced at the classification layer
(:func:`meho_backplane.broadcast.events.classify_op` maps
``vault.auth.userpass.write`` / ``.update_password`` to
``credential_write`` and ``vault.auth.approle.generate_secret_id`` to
``credential_mint``), not in these schemas -- the schemas only describe
the wire shape. The ``llm_instructions`` flag the redaction behaviour
and, for ``generate_secret_id``, its non-idempotent secret-minting
nature so an agent does not re-issue it expecting a stable value.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "VAULT_AUTH_APPROLE_DELETE_LLM_INSTRUCTIONS",
    "VAULT_AUTH_APPROLE_DELETE_PARAMETER_SCHEMA",
    "VAULT_AUTH_APPROLE_DELETE_RESPONSE_SCHEMA",
    "VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_LLM_INSTRUCTIONS",
    "VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_PARAMETER_SCHEMA",
    "VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_RESPONSE_SCHEMA",
    "VAULT_AUTH_APPROLE_WRITE_LLM_INSTRUCTIONS",
    "VAULT_AUTH_APPROLE_WRITE_PARAMETER_SCHEMA",
    "VAULT_AUTH_APPROLE_WRITE_RESPONSE_SCHEMA",
    "VAULT_AUTH_USERPASS_DELETE_LLM_INSTRUCTIONS",
    "VAULT_AUTH_USERPASS_DELETE_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_DELETE_RESPONSE_SCHEMA",
    "VAULT_AUTH_USERPASS_UPDATE_PASSWORD_LLM_INSTRUCTIONS",
    "VAULT_AUTH_USERPASS_UPDATE_PASSWORD_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_UPDATE_PASSWORD_RESPONSE_SCHEMA",
    "VAULT_AUTH_USERPASS_WRITE_LLM_INSTRUCTIONS",
    "VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_WRITE_RESPONSE_SCHEMA",
]


def _mount_property(default: str) -> dict[str, Any]:
    """Build the shared optional ``mount`` schema property.

    ``mount`` is optional with a per-op default so non-default mounts
    (``auth/userpass-prod``) work without a code change. ``pattern="\\S"``
    rejects a whitespace-only override; the enclosing schema's
    ``additionalProperties=False`` turns a typo (``{"moutn": "x"}``)
    into a clear validation error instead of a silent default-mount
    dispatch. Identical to the read-side helper -- kept local rather
    than imported to keep the two schema modules independently
    reviewable.
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


_USERNAME_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "userpass username (no mount prefix). Forwarded verbatim to hvac's username= argument."
    ),
}

_PASSWORD_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "The user's password. SECRET MATERIAL: redacted from the audit "
        "row (only a params hash is stored) and from the broadcast feed "
        "(this op classifies credential_write -> aggregate-only). Reaches "
        "Vault verbatim; never echoed in the op response."
    ),
}

_ROLE_NAME_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": (
        "AppRole role name (no mount prefix). Forwarded verbatim to hvac's role_name= argument."
    ),
}


# --- userpass.write --------------------------------------------------------

VAULT_AUTH_USERPASS_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": _USERNAME_PROPERTY,
        "password": _PASSWORD_PROPERTY,
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
    "required": ["username", "password"],
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
        "written": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["username", "written"],
    "description": (
        "Confirms the create/update. NEVER carries the password -- the "
        "value-free echo reports the username, mount, and assigned "
        "policies only."
    ),
}

VAULT_AUTH_USERPASS_WRITE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Create a new userpass user or update an existing one's password "
        "and/or token policies. Use when provisioning a human/service "
        "login on the userpass backend. DANGEROUS, requires approval: "
        "binding token_policies is a privilege assignment. The password "
        "is SECRET -- it is redacted from the audit row and the broadcast "
        "feed (credential_write), reaches Vault verbatim, and is never "
        "echoed back."
    ),
    "parameter_hints": {
        "username": "Required. The userpass username, no mount prefix.",
        "password": "Required. The user's password (secret; redacted from audit/broadcast).",
        "token_policies": (
            "Optional. Policy names bound to the user's tokens (privilege "
            "assignment). Omit to leave unchanged on an update."
        ),
        "mount": _mount_hint("userpass"),
    },
    "output_shape": (
        "On success: {'username': ..., 'mount': ..., 'token_policies': "
        "[...], 'written': true} -- never the password. On failure: a "
        "connector_error OperationResult with extras.exception_class "
        "naming the failure class ('VaultAuthBackendNotMountedError' when "
        "userpass is not enabled at the mount)."
    ),
}


# --- userpass.update_password ----------------------------------------------

VAULT_AUTH_USERPASS_UPDATE_PASSWORD_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": _USERNAME_PROPERTY,
        "password": _PASSWORD_PROPERTY,
        "mount": _mount_property("userpass"),
    },
    "required": ["username", "password"],
    "additionalProperties": False,
}

VAULT_AUTH_USERPASS_UPDATE_PASSWORD_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": {"type": "string", "description": "The user whose password changed."},
        "mount": {"type": "string", "description": "The mount the user lives on."},
        "password_updated": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["username", "password_updated"],
    "description": ("Confirms the password rotation. NEVER carries the new password."),
}

VAULT_AUTH_USERPASS_UPDATE_PASSWORD_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Rotate an existing userpass user's password without touching "
        "their token policies. Use for a password-only change (the user "
        "must already exist). Requires approval. The password is SECRET "
        "-- redacted from the audit row and the broadcast feed "
        "(credential_write), reaches Vault verbatim, never echoed back."
    ),
    "parameter_hints": {
        "username": "Required. The existing userpass username, no mount prefix.",
        "password": "Required. The new password (secret; redacted from audit/broadcast).",
        "mount": _mount_hint("userpass"),
    },
    "output_shape": (
        "On success: {'username': ..., 'mount': ..., 'password_updated': "
        "true} -- never the password. On failure: a connector_error "
        "OperationResult; extras.exception_class is "
        "'VaultAuthBackendNotMountedError' when userpass is not enabled, "
        "'InvalidPath' when the user does not exist under a mounted "
        "backend."
    ),
}


# --- userpass.delete -------------------------------------------------------

VAULT_AUTH_USERPASS_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": _USERNAME_PROPERTY,
        "mount": _mount_property("userpass"),
    },
    "required": ["username"],
    "additionalProperties": False,
}

VAULT_AUTH_USERPASS_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": {"type": "string", "description": "The user deleted."},
        "mount": {"type": "string", "description": "The mount the user lived on."},
        "deleted": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["username", "deleted"],
    "description": "Confirms the user removal.",
}

VAULT_AUTH_USERPASS_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Delete a userpass user, revoking their ability to log in. "
        "DANGEROUS, requires approval -- irreversible removal of a login "
        "identity. Vault's delete is idempotent: deleting a "
        "non-existent user is a no-op success."
    ),
    "parameter_hints": {
        "username": "Required. The userpass username to remove, no mount prefix.",
        "mount": _mount_hint("userpass"),
    },
    "output_shape": (
        "On success: {'username': ..., 'mount': ..., 'deleted': true}. On "
        "failure: a connector_error OperationResult; extras.exception_class "
        "is 'VaultAuthBackendNotMountedError' when userpass is not enabled."
    ),
}


# --- approle.write ---------------------------------------------------------

VAULT_AUTH_APPROLE_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_name": _ROLE_NAME_PROPERTY,
        "token_policies": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Policies bound to tokens this role issues.",
        },
        "token_ttl": {
            "type": "integer",
            "minimum": 0,
            "description": "Default token TTL in seconds (0 = system default).",
        },
        "token_max_ttl": {
            "type": "integer",
            "minimum": 0,
            "description": "Maximum token TTL in seconds (0 = system default).",
        },
        "secret_id_ttl": {
            "type": "integer",
            "minimum": 0,
            "description": "TTL of SecretIDs issued for this role, seconds (0 = unlimited).",
        },
        "secret_id_num_uses": {
            "type": "integer",
            "minimum": 0,
            "description": "Number of times a SecretID can be used (0 = unlimited).",
        },
        "bind_secret_id": {
            "type": "boolean",
            "description": "Whether a SecretID is required at login (default true).",
        },
        "mount": _mount_property("approle"),
    },
    "required": ["role_name"],
    "additionalProperties": False,
}

VAULT_AUTH_APPROLE_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_name": {"type": "string", "description": "The role created or updated."},
        "mount": {"type": "string", "description": "The mount the role lives on."},
        "written": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["role_name", "written"],
    "description": (
        "Confirms the create/update. No SecretID is minted by this op -- "
        "use vault.auth.approle.generate_secret_id for that."
    ),
}

VAULT_AUTH_APPROLE_WRITE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Create a new AppRole or update an existing one's token / "
        "SecretID policy and TTL configuration. DANGEROUS, requires "
        "approval -- binding token_policies is a privilege assignment. "
        "Does NOT mint a SecretID (that is generate_secret_id, a separate "
        "op)."
    ),
    "parameter_hints": {
        "role_name": "Required. The AppRole role name, no mount prefix.",
        "token_policies": "Optional. Policy names bound to tokens this role issues.",
        "token_ttl": "Optional. Default token TTL in seconds.",
        "token_max_ttl": "Optional. Maximum token TTL in seconds.",
        "secret_id_ttl": "Optional. TTL of SecretIDs issued for this role, seconds.",
        "secret_id_num_uses": "Optional. How many times a SecretID can be used.",
        "bind_secret_id": "Optional. Whether a SecretID is required at login.",
        "mount": _mount_hint("approle"),
    },
    "output_shape": (
        "On success: {'role_name': ..., 'mount': ..., 'written': true}. On "
        "failure: a connector_error OperationResult; extras.exception_class "
        "is 'VaultAuthBackendNotMountedError' when approle is not enabled."
    ),
}


# --- approle.delete --------------------------------------------------------

VAULT_AUTH_APPROLE_DELETE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_name": _ROLE_NAME_PROPERTY,
        "mount": _mount_property("approle"),
    },
    "required": ["role_name"],
    "additionalProperties": False,
}

VAULT_AUTH_APPROLE_DELETE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_name": {"type": "string", "description": "The role deleted."},
        "mount": {"type": "string", "description": "The mount the role lived on."},
        "deleted": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["role_name", "deleted"],
    "description": "Confirms the role removal.",
}

VAULT_AUTH_APPROLE_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Delete an AppRole, invalidating its role-id and all issued "
        "SecretIDs. DANGEROUS, requires approval -- irreversible removal "
        "of a machine-login identity. Vault's delete is idempotent: "
        "deleting a non-existent role is a no-op success."
    ),
    "parameter_hints": {
        "role_name": "Required. The AppRole role name to remove, no mount prefix.",
        "mount": _mount_hint("approle"),
    },
    "output_shape": (
        "On success: {'role_name': ..., 'mount': ..., 'deleted': true}. On "
        "failure: a connector_error OperationResult; extras.exception_class "
        "is 'VaultAuthBackendNotMountedError' when approle is not enabled."
    ),
}


# --- approle.generate_secret_id --------------------------------------------

VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_name": _ROLE_NAME_PROPERTY,
        "metadata": {
            "type": "object",
            "description": (
                "Optional metadata tied to the SecretID (visible in token "
                "metadata after login). Plain key/value strings."
            ),
            "additionalProperties": {"type": "string"},
        },
        "cidr_list": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Optional CIDR blocks the SecretID may log in from.",
        },
        "token_bound_cidrs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Optional CIDR blocks tokens issued via this SecretID are bound to.",
        },
        "mount": _mount_property("approle"),
    },
    "required": ["role_name"],
    "additionalProperties": False,
}

VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "secret_id": {
            "type": "string",
            "description": (
                "The freshly-minted SecretID. SECRET, one-time value -- "
                "store it immediately. Redacted from the audit row and the "
                "broadcast feed (credential_mint); only the caller's "
                "OperationResult carries it."
            ),
        },
        "secret_id_accessor": {
            "type": "string",
            "description": (
                "Non-secret accessor for the SecretID (use to revoke without the value)."
            ),
        },
        "secret_id_ttl": {
            "type": "integer",
            "description": "TTL of the issued SecretID in seconds.",
        },
        "role_name": {"type": "string", "description": "The role the SecretID was minted for."},
        "mount": {"type": "string", "description": "The mount the role lives on."},
    },
    "required": ["secret_id", "role_name"],
    "description": (
        "The minted SecretID plus its accessor and TTL. The secret_id is "
        "secret material: present in the caller's OperationResult, absent "
        "from the audit row and broadcast feed."
    ),
}

VAULT_AUTH_APPROLE_GENERATE_SECRET_ID_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Mint a new SecretID for an AppRole so a machine identity can log "
        "in. DANGEROUS, requires approval. NON-IDEMPOTENT: each call mints "
        "a brand-new SecretID -- calling twice yields two distinct "
        "credentials, so never retry it expecting a stable value, and "
        "never call it to 'read' an existing SecretID (there is no such "
        "read; SecretIDs are write-only after minting). The response "
        "carries the SecretID, a one-time secret -- store it immediately. "
        "The SecretID is redacted from the audit row and the broadcast "
        "feed (credential_mint classification); only your OperationResult "
        "sees it."
    ),
    "parameter_hints": {
        "role_name": "Required. The AppRole to mint a SecretID for, no mount prefix.",
        "metadata": "Optional. String key/value metadata tied to the SecretID.",
        "cidr_list": "Optional. CIDR blocks the SecretID may log in from.",
        "token_bound_cidrs": "Optional. CIDR blocks issued tokens are bound to.",
        "mount": _mount_hint("approle"),
    },
    "output_shape": (
        "On success: {'secret_id': '<minted-secret>', "
        "'secret_id_accessor': ..., 'secret_id_ttl': <int>, 'role_name': "
        "..., 'mount': ...}. The secret_id is one-time -- save it now. On "
        "failure: a connector_error OperationResult; extras.exception_class "
        "is 'VaultAuthBackendNotMountedError' when approle is not enabled, "
        "'InvalidPath' when the role does not exist under a mounted "
        "backend."
    ),
}

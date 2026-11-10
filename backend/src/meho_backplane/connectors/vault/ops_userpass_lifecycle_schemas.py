# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Schemas and guidance for ``vault.auth.userpass.disable`` and ``.enable``.

Both ops switch a userpass login off or on without deleting it. They do this
through the login's identity entity (see
:mod:`meho_backplane.connectors.vault.ops_userpass_lifecycle`).
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vault.ops_auth_write_schemas import _mount_property

__all__ = [
    "VAULT_AUTH_USERPASS_DISABLE_LLM_INSTRUCTIONS",
    "VAULT_AUTH_USERPASS_DISABLE_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_ENABLE_LLM_INSTRUCTIONS",
    "VAULT_AUTH_USERPASS_ENABLE_PARAMETER_SCHEMA",
    "VAULT_AUTH_USERPASS_STATE_RESPONSE_SCHEMA",
]

_USERNAME_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "^(?!\\.{1,2}$)[^/\\s]+$",
    "description": (
        "userpass username (no mount prefix, no '/'). The login must exist on the mount."
    ),
}

VAULT_AUTH_USERPASS_DISABLE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": _USERNAME_PROPERTY,
        "mount": _mount_property("userpass"),
        "revoke_tokens": {
            "type": "boolean",
            "default": False,
            "description": (
                "true also revokes the tokens the user already has. Without it, "
                "Vault keeps them but cannot use them while the login is off."
            ),
        },
    },
    "required": ["username"],
    "additionalProperties": False,
    "examples": [{"username": "alice", "mount": "userpass", "revoke_tokens": True}],
}

VAULT_AUTH_USERPASS_ENABLE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": _USERNAME_PROPERTY,
        "mount": _mount_property("userpass"),
    },
    "required": ["username"],
    "additionalProperties": False,
    "examples": [{"username": "alice", "mount": "userpass"}],
}

VAULT_AUTH_USERPASS_STATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "username": {"type": "string", "description": "The login that was switched."},
        "mount": {"type": "string", "description": "The mount the login lives on."},
        "disabled": {"type": "boolean", "description": "The state of the login after the call."},
        "changed": {
            "type": "boolean",
            "description": "False when the login was already in this state.",
        },
        "entity_id": {
            "type": ["string", "null"],
            "description": "Id of the identity entity that holds the state. Null when none.",
        },
        "entity_created": {
            "type": "boolean",
            "description": "True when this call had to create the entity and its alias.",
        },
        "revoke_requested": {
            "type": "boolean",
            "description": (
                "True when revoke_tokens was given. Vault revokes the tokens in the "
                "background, so this is a request, not a count."
            ),
        },
    },
    "required": ["username", "mount", "disabled", "changed"],
    "description": "The state of the login after the call. No password or token is returned.",
}

VAULT_AUTH_USERPASS_DISABLE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to take a person's userpass login away without deleting it, for example "
        "when they leave a project. Call vault.auth.userpass.enable to give it back."
    ),
    "preconditions": (
        "A person must approve the call. The login must exist on the mount. The Vault "
        "login of MEHO needs write access to the identity entity paths, and sudo on "
        "sys/leases/revoke-prefix when revoke_tokens is true."
    ),
    "output_shape": (
        "{username, mount, disabled, changed, entity_id, entity_created, "
        "revoke_requested}. changed is false on a repeat. No token or password."
    ),
    "next_step": (
        "Check with vault.identity.entity.read using entity_id. Use "
        "vault.auth.userpass.enable to restore the login."
    ),
    "when_to_use": (
        "Switch a userpass login off (reversible) instead of deleting it with "
        "vault.auth.userpass.delete."
    ),
    "parameter_hints": {
        "username": "Required. The userpass username, no mount prefix.",
        "mount": "Optional. Default 'userpass'.",
        "revoke_tokens": "Optional. true also revokes the tokens the user already has.",
    },
}

VAULT_AUTH_USERPASS_ENABLE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Call to give a userpass login back after vault.auth.userpass.disable. The "
        "password and policies stay as they were."
    ),
    "preconditions": (
        "A person must approve the call. The login must exist on the mount. The Vault "
        "login of MEHO needs write access to the identity entity paths."
    ),
    "output_shape": (
        "{username, mount, disabled, changed, entity_id, entity_created}. changed is "
        "false when the login was already on."
    ),
    "next_step": "Ask the user to sign in. Nothing else is needed.",
    "when_to_use": "Switch a disabled userpass login back on.",
    "parameter_hints": {
        "username": "Required. The userpass username, no mount prefix.",
        "mount": "Optional. Default 'userpass'.",
    },
}

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema and guidance for the Vault identity token role and key ops.

Pure data only: JSON Schema 2020-12 ``parameter_schema`` /
``response_schema`` documents and the ``llm_instructions`` payload the
meta-tools inline verbatim.

Vault uses an identity token **role** and a named **key** to sign identity
tokens (JWTs) that another system, such as a cloud provider, trusts. A
role names the key that signs its tokens, the token audience
(``client_id``), the token lifetime and a claims template. A key read
shows the signing algorithm and timing settings, never the private key.
Nothing in these schemas is a secret, so nothing is redacted.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "VAULT_IDENTITY_OIDC_KEY_DELETE_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_OIDC_KEY_DELETE_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_OIDC_KEY_DELETE_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_OIDC_KEY_LIST_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_OIDC_KEY_LIST_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_OIDC_KEY_LIST_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_OIDC_KEY_READ_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_OIDC_KEY_READ_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_OIDC_KEY_READ_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_OIDC_ROLE_DELETE_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_OIDC_ROLE_DELETE_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_OIDC_ROLE_DELETE_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_OIDC_ROLE_LIST_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_OIDC_ROLE_LIST_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_OIDC_ROLE_LIST_RESPONSE_SCHEMA",
    "VAULT_IDENTITY_OIDC_ROLE_READ_LLM_INSTRUCTIONS",
    "VAULT_IDENTITY_OIDC_ROLE_READ_PARAMETER_SCHEMA",
    "VAULT_IDENTITY_OIDC_ROLE_READ_RESPONSE_SCHEMA",
]

#: The name shape Vault itself accepts for a role or key name (a word
#: character first and last; letters, digits, ``_``, ``-`` and ``.`` in
#: between). A name can never carry a ``/`` that would point the call at
#: a different Vault path.
_NAME_PATTERN = "^[A-Za-z0-9_]([A-Za-z0-9_.-]*[A-Za-z0-9_])?$"


def _name_property(kind: str) -> dict[str, Any]:
    return {
        "type": "string",
        "minLength": 1,
        "pattern": _NAME_PATTERN,
        "description": (
            f"Name of the identity token {kind}. Letters, digits, '_', '-' and '.' "
            f"only. It becomes the last part of the Vault path identity/oidc/{kind}/<name>."
        ),
    }


def _name_schema(kind: str, example: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"name": _name_property(kind)},
        "required": ["name"],
        "additionalProperties": False,
        "examples": [{"name": example}],
    }


_EMPTY_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
    "examples": [{}],
}


def _list_response_schema(kind: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": f"The identity token {kind} names. Empty list when there are none.",
            },
        },
        "required": ["keys"],
        "description": f"The list of identity token {kind} names. Always carries a 'keys' list.",
    }


def _delete_response_schema(kind: str) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": f"The identity token {kind} deleted."},
            "deleted": {"type": "boolean", "description": "Always true on success."},
        },
        "required": ["name", "deleted"],
        "description": "Confirms the delete. The answer holds no secret.",
    }


# --- identity.oidc.role ----------------------------------------------------

VAULT_IDENTITY_OIDC_ROLE_LIST_PARAMETER_SCHEMA: dict[str, Any] = _EMPTY_PARAMETER_SCHEMA
VAULT_IDENTITY_OIDC_ROLE_LIST_RESPONSE_SCHEMA: dict[str, Any] = _list_response_schema("role")
VAULT_IDENTITY_OIDC_ROLE_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "List the names of the identity token roles (identity/oidc/role). A "
        "role tells Vault how to sign identity tokens (JWTs) that another "
        "system, such as a cloud provider, trusts. Read-only, safe, no "
        "approval. Names only."
    ),
    "parameter_hints": {},
    "output_shape": "On success: {'keys': [<role names>]}. No roles yet: {'keys': []}.",
    "next_step": (
        "Call vault.identity.oidc.role.read with a name to see which key signs "
        "the role's tokens and who the token audience is."
    ),
}

VAULT_IDENTITY_OIDC_ROLE_READ_PARAMETER_SCHEMA: dict[str, Any] = _name_schema("role", "ci-deployer")
VAULT_IDENTITY_OIDC_ROLE_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "key": {"type": "string", "description": "Name of the key that signs this role's tokens."},
        "client_id": {
            "type": "string",
            "description": (
                "Token audience ('aud' claim). The system that trusts the token checks it."
            ),
        },
        "ttl": {"type": "integer", "description": "Token lifetime in seconds."},
        "template": {
            "type": "string",
            "description": "Claims template added to each token; often empty.",
        },
    },
    "description": (
        "The role settings as Vault stores them, unwrapped from Vault's data "
        "envelope. None of them is a secret. Vault may add more fields."
    ),
}
VAULT_IDENTITY_OIDC_ROLE_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Read one identity token role: the key that signs its tokens, the "
        "token audience (client_id), the lifetime (ttl) and the claims "
        "template. Use it to see what a role is for before you delete it. "
        "Read-only, safe, no approval. Nothing in the answer is a secret."
    ),
    "parameter_hints": {"name": "Required. The role name from vault.identity.oidc.role.list."},
    "output_shape": (
        "On success: {'key': ..., 'client_id': ..., 'ttl': <seconds>, "
        "'template': ...}. An unknown role is a not-found error "
        "(connector_error with exception_class 'InvalidPath')."
    ),
    "next_step": (
        "Call vault.identity.oidc.key.read with the 'key' value to see the "
        "signing key, or vault.identity.oidc.role.delete to remove the role."
    ),
}

VAULT_IDENTITY_OIDC_ROLE_DELETE_PARAMETER_SCHEMA: dict[str, Any] = _name_schema(
    "role", "ci-deployer"
)
VAULT_IDENTITY_OIDC_ROLE_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _delete_response_schema("role")
VAULT_IDENTITY_OIDC_ROLE_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Delete one identity token role. After this, Vault no longer issues "
        "identity tokens for the role, so a system that trusts those tokens "
        "stops getting new ones. Dangerous and needs approval. Deleting a "
        "role that does not exist also succeeds."
    ),
    "preconditions": (
        "The target's Vault role needs 'delete' on identity/oidc/role/<name>. "
        "MEHO checks this before it asks for approval; when the right is "
        "missing the call is refused with error_code 'write_capability_denied' "
        "and no approval is requested. Read the role first "
        "(vault.identity.oidc.role.read) to be sure it is the right one."
    ),
    "parameter_hints": {"name": "Required. The role name."},
    "output_shape": (
        "On success: {'name': ..., 'deleted': true}. Before approval, a "
        "missing Vault right gives status 'denied' with error_code "
        "'write_capability_denied'. On failure after approval: a "
        "connector_error result."
    ),
    "next_step": (
        "If the role's key is no longer used, delete it with "
        "vault.identity.oidc.key.delete. Also remove the trust for this "
        "role's tokens on the system that accepted them."
    ),
}


# --- identity.oidc.key -----------------------------------------------------

VAULT_IDENTITY_OIDC_KEY_LIST_PARAMETER_SCHEMA: dict[str, Any] = _EMPTY_PARAMETER_SCHEMA
VAULT_IDENTITY_OIDC_KEY_LIST_RESPONSE_SCHEMA: dict[str, Any] = _list_response_schema("key")
VAULT_IDENTITY_OIDC_KEY_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "List the names of the keys that sign identity tokens "
        "(identity/oidc/key). Read-only, safe, no approval. Names only."
    ),
    "parameter_hints": {},
    "output_shape": "On success: {'keys': [<key names>]}. No keys yet: {'keys': []}.",
    "next_step": "Call vault.identity.oidc.key.read with a name to see that key's settings.",
}

VAULT_IDENTITY_OIDC_KEY_READ_PARAMETER_SCHEMA: dict[str, Any] = _name_schema("key", "ci-signing")
VAULT_IDENTITY_OIDC_KEY_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "algorithm": {"type": "string", "description": "Signing algorithm, for example 'RS256'."},
        "allowed_client_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Role client_ids allowed to use this key; '*' means any.",
        },
        "rotation_period": {"type": "integer", "description": "Seconds between key rotations."},
        "verification_ttl": {
            "type": "integer",
            "description": "Seconds an old public key stays valid for checking tokens.",
        },
    },
    "description": (
        "The key settings as Vault stores them, unwrapped from Vault's data "
        "envelope. Vault never returns the private key. Vault may add more fields."
    ),
}
VAULT_IDENTITY_OIDC_KEY_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Read the settings of one key that signs identity tokens: algorithm, "
        "allowed client ids, rotation period and verification ttl. Read-only, "
        "safe, no approval. Vault never returns the private key."
    ),
    "parameter_hints": {"name": "Required. The key name from vault.identity.oidc.key.list."},
    "output_shape": (
        "On success: {'algorithm': ..., 'allowed_client_ids': [...], "
        "'rotation_period': <seconds>, 'verification_ttl': <seconds>}. An "
        "unknown key is a not-found error (connector_error with "
        "exception_class 'InvalidPath')."
    ),
    "next_step": (
        "To delete the key, first delete or change every role that uses it, "
        "then call vault.identity.oidc.key.delete."
    ),
}

VAULT_IDENTITY_OIDC_KEY_DELETE_PARAMETER_SCHEMA: dict[str, Any] = _name_schema("key", "ci-signing")
VAULT_IDENTITY_OIDC_KEY_DELETE_RESPONSE_SCHEMA: dict[str, Any] = _delete_response_schema("key")
VAULT_IDENTITY_OIDC_KEY_DELETE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Delete one key that signs identity tokens. Tokens it signed can no "
        "longer be checked once its public key is gone. Dangerous and needs "
        "approval. Vault refuses while a role still uses the key, and never "
        "deletes the key named 'default'."
    ),
    "preconditions": (
        "The target's Vault role needs 'delete' on identity/oidc/key/<name>. "
        "MEHO checks this before it asks for approval; when the right is "
        "missing the call is refused with error_code 'write_capability_denied' "
        "and no approval is requested. Delete or change every role that uses "
        "the key first (vault.identity.oidc.role.delete)."
    ),
    "parameter_hints": {"name": "Required. The key name. 'default' cannot be deleted."},
    "output_shape": (
        "On success: {'name': ..., 'deleted': true}. When roles still use the "
        "key: connector_error with exception_class 'VaultOidcKeyInUseError'; "
        "its remediation names the roles. Other failures after approval: a "
        "connector_error result."
    ),
    "next_step": "Call vault.identity.oidc.key.list to confirm the key is gone.",
}

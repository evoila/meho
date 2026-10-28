# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema and guidance for the Vault *token role* ops.

Sibling of :mod:`meho_backplane.connectors.vault.ops_token_schemas`, split
out so the token group's schema data stays under the file-size budget.
Pure data only: JSON Schema 2020-12 ``parameter_schema`` /
``response_schema`` documents and the ``llm_instructions`` payload the
meta-tools inline verbatim.

A token role is a saved set of rules in Vault for making tokens: which
policies a token may get, whether it has a parent token, and how long it
lives between renewals. A role holds no secret, so nothing in these
schemas is redacted.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "VAULT_TOKEN_ROLE_LIST_LLM_INSTRUCTIONS",
    "VAULT_TOKEN_ROLE_LIST_PARAMETER_SCHEMA",
    "VAULT_TOKEN_ROLE_LIST_RESPONSE_SCHEMA",
    "VAULT_TOKEN_ROLE_READ_LLM_INSTRUCTIONS",
    "VAULT_TOKEN_ROLE_READ_PARAMETER_SCHEMA",
    "VAULT_TOKEN_ROLE_READ_RESPONSE_SCHEMA",
    "VAULT_TOKEN_ROLE_WRITE_LLM_INSTRUCTIONS",
    "VAULT_TOKEN_ROLE_WRITE_PARAMETER_SCHEMA",
    "VAULT_TOKEN_ROLE_WRITE_RESPONSE_SCHEMA",
]

#: The role name becomes the last part of the Vault path
#: ``auth/token/roles/<role_name>``. The pattern is the one Vault itself
#: accepts for a role name (a word character first and last; letters,
#: digits, ``_``, ``-`` and ``.`` in between), so a name can never carry a
#: ``/`` that would point the call at a different Vault path.
_ROLE_NAME_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "^[A-Za-z0-9_]([A-Za-z0-9_.-]*[A-Za-z0-9_])?$",
    "description": (
        "Name of the token role. Letters, digits, '_', '-' and '.' only. It "
        "becomes the last part of the Vault path auth/token/roles/<role_name>."
    ),
}

_POLICY_LIST_ITEMS: dict[str, Any] = {"type": "string", "minLength": 1}


def _duration_property(description: str) -> dict[str, Any]:
    """A Go duration string field (for example ``'24h'`` or ``'30m'``)."""
    return {"type": "string", "minLength": 1, "pattern": "\\S", "description": description}


# --- token.role.write ------------------------------------------------------

VAULT_TOKEN_ROLE_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_name": _ROLE_NAME_PROPERTY,
        "allowed_policies": {
            "type": "array",
            "items": _POLICY_LIST_ITEMS,
            "description": (
                "Policies a token made from this role may get. An empty list "
                "means any policy the caller holds."
            ),
        },
        "disallowed_policies": {
            "type": "array",
            "items": _POLICY_LIST_ITEMS,
            "description": "Policies a token made from this role may never get.",
        },
        "orphan": {
            "type": "boolean",
            "description": (
                "True: tokens made from this role have no parent token, so revoking "
                "the token that made them does not revoke them."
            ),
        },
        "renewable": {
            "type": "boolean",
            "description": (
                "Whether tokens made from this role can be renewed (Vault default: true)."
            ),
        },
        "token_period": _duration_property(
            "Period for periodic tokens, as a Go duration string (for example '24h'). "
            "Each renewal gives the token this much time again, with no maximum lifetime."
        ),
        "token_explicit_max_ttl": _duration_property(
            "Hard limit on a token's total lifetime, as a Go duration string (for "
            "example '720h'). A token cannot be renewed past it."
        ),
    },
    "required": ["role_name"],
    "additionalProperties": False,
    "examples": [
        {
            "role_name": "ci-runner",
            "allowed_policies": ["ci-read"],
            "orphan": True,
            "token_period": "24h",
        }
    ],
}

VAULT_TOKEN_ROLE_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_name": {"type": "string", "description": "The token role created or changed."},
        "written": {"type": "boolean", "description": "Always true on success."},
        "settings": {
            "type": "object",
            "description": (
                "The settings this call sent, by field name. A field the caller did "
                "not give is not listed and keeps its current value in Vault."
            ),
        },
    },
    "required": ["role_name", "written", "settings"],
    "description": (
        "Confirms the write. No token is made. Use vault.token.role.read to see "
        "the whole role as Vault stores it."
    ),
}

VAULT_TOKEN_ROLE_WRITE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Create a token role, or change one that exists. A token role is a "
        "saved set of rules for making tokens: which policies a token may "
        "get, whether it is an orphan, and how long it lives between "
        "renewals. Dangerous and needs approval: the allowed policies decide "
        "what a token made from this role can do. Only the fields you give "
        "are sent; the other settings keep their current value."
    ),
    "preconditions": (
        "The target's Vault role needs 'create' and 'update' on "
        "auth/token/roles/<role_name>. MEHO checks this before it asks for "
        "approval; when a right is missing the call is refused with "
        "error_code 'write_capability_denied' and no approval is requested. "
        "Every policy named in allowed_policies should exist "
        "(vault.sys.policy.list)."
    ),
    "parameter_hints": {
        "role_name": "Required. Letters, digits, '_', '-' and '.'.",
        "allowed_policies": "Optional. Policy names a token from this role may get.",
        "disallowed_policies": "Optional. Policy names a token from this role may never get.",
        "orphan": "Optional. True for tokens with no parent token.",
        "renewable": "Optional. False to forbid renewals.",
        "token_period": "Optional. Go duration string such as '24h'. Makes the tokens periodic.",
        "token_explicit_max_ttl": (
            "Optional. Go duration string such as '720h'. Hard lifetime limit."
        ),
    },
    "output_shape": (
        "On success: {'role_name': ..., 'written': true, 'settings': {<the "
        "fields sent>}}. Before approval, a missing Vault right gives status "
        "'denied' with error_code 'write_capability_denied', naming the path, "
        "the rights needed and the rights held. On failure after approval: a "
        "connector_error result."
    ),
    "next_step": (
        "Call vault.token.role.read to see the whole role as Vault stores it, "
        "or vault.token.role.list to see every role name."
    ),
}


# --- token.role.read -------------------------------------------------------

VAULT_TOKEN_ROLE_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"role_name": _ROLE_NAME_PROPERTY},
    "required": ["role_name"],
    "additionalProperties": False,
    "examples": [{"role_name": "ci-runner"}],
}

VAULT_TOKEN_ROLE_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "The role name."},
        "allowed_policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Policies a token made from this role may get.",
        },
        "disallowed_policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Policies a token made from this role may never get.",
        },
        "orphan": {
            "type": "boolean",
            "description": "True when tokens made from this role have no parent token.",
        },
        "renewable": {
            "type": "boolean",
            "description": "Whether tokens made from this role can be renewed.",
        },
        "token_period": {
            "type": "integer",
            "description": (
                "Period in seconds for periodic tokens; 0 when the tokens are not periodic."
            ),
        },
        "token_explicit_max_ttl": {
            "type": "integer",
            "description": "Hard lifetime limit in seconds; 0 when there is none.",
        },
        "token_type": {
            "type": "string",
            "description": "Vault token type the role makes, for example 'default' or 'service'.",
        },
        "path_suffix": {
            "type": "string",
            "description": "Extra path part Vault adds to tokens made from this role; often empty.",
        },
    },
    "description": (
        "The role settings as Vault stores them, unwrapped from Vault's data "
        "envelope. Durations come back as seconds. Vault may add more fields."
    ),
}

VAULT_TOKEN_ROLE_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Read the settings of one token role: allowed and disallowed "
        "policies, orphan, renewable, period and maximum lifetime. Use it to "
        "check a role before tokens are made from it, or after "
        "vault.token.role.write. Read-only, safe, no approval. A role holds "
        "no secret."
    ),
    "parameter_hints": {"role_name": "Required. The token role name."},
    "output_shape": (
        "On success: the role settings as a flat object, for example "
        "{'name': 'ci-runner', 'allowed_policies': ['ci-read'], 'orphan': "
        "true, 'renewable': true, 'token_period': 86400, "
        "'token_explicit_max_ttl': 0, ...}. Durations are seconds. An unknown "
        "role is a not-found error (connector_error with exception_class "
        "'InvalidPath')."
    ),
    "next_step": (
        "To change a setting, call vault.token.role.write with the role_name "
        "and only the fields to change. vault.token.role.list shows every "
        "role name."
    ),
}


# --- token.role.list -------------------------------------------------------

VAULT_TOKEN_ROLE_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
    "examples": [{}],
}

VAULT_TOKEN_ROLE_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The token role names. Empty list when there are none.",
        },
    },
    "required": ["keys"],
    "description": "The list of token role names. Always carries a 'keys' list.",
}

VAULT_TOKEN_ROLE_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "List the names of every token role in Vault. Use it to find out "
        "whether a role exists before you write or read it. Read-only, safe, "
        "no approval. Names only; no secret content."
    ),
    "parameter_hints": {},
    "output_shape": (
        "On success: {'keys': [<role names>]}. When there are no roles yet, {'keys': []}."
    ),
    "next_step": (
        "Call vault.token.role.read with a name from the list to see that "
        "role's settings, or vault.token.role.write to create a role that is "
        "missing."
    ),
}

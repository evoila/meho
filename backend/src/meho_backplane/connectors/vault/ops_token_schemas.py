# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema + ``llm_instructions`` for the Vault *token* ops (G3.15-T4).

Sibling of :mod:`meho_backplane.connectors.vault.ops_identity_schemas`;
split out so each group's schema data stays under the file-size budget.
Pure data only -- JSON Schema 2020-12 ``parameter_schema`` /
``response_schema`` documents and the structured ``llm_instructions``
payload the meta-tools (G0.6-T8) inline verbatim.

Secret-material discipline: the ``vault.token.create`` handler drops the
new client token from Vault's answer, so its response schema has only the
accessor and the policies. A token **accessor** (``revoke_accessor`` / ``lookup_accessor``
param, ``list_accessors`` response) is a reference handle, not the token
secret -- deliberately not redacted. Vault also blanks the token ``id``
in a ``lookup-accessor`` response, so the metadata read (#2844) exposes
no secret either.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "VAULT_TOKEN_CREATE_LLM_INSTRUCTIONS",
    "VAULT_TOKEN_CREATE_PARAMETER_SCHEMA",
    "VAULT_TOKEN_CREATE_RESPONSE_SCHEMA",
    "VAULT_TOKEN_LIST_ACCESSORS_LLM_INSTRUCTIONS",
    "VAULT_TOKEN_LIST_ACCESSORS_PARAMETER_SCHEMA",
    "VAULT_TOKEN_LIST_ACCESSORS_RESPONSE_SCHEMA",
    "VAULT_TOKEN_LOOKUP_ACCESSOR_LLM_INSTRUCTIONS",
    "VAULT_TOKEN_LOOKUP_ACCESSOR_PARAMETER_SCHEMA",
    "VAULT_TOKEN_LOOKUP_ACCESSOR_RESPONSE_SCHEMA",
    "VAULT_TOKEN_REVOKE_ACCESSOR_LLM_INSTRUCTIONS",
    "VAULT_TOKEN_REVOKE_ACCESSOR_PARAMETER_SCHEMA",
    "VAULT_TOKEN_REVOKE_ACCESSOR_RESPONSE_SCHEMA",
]


# --- token.create ----------------------------------------------------------

VAULT_TOKEN_CREATE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "policies": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Policy names attached to the new token -- the privilege "
                "grant. Omit to inherit the calling token's policies."
            ),
        },
        "ttl": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Token TTL as a Go duration string (e.g. '1h', '30m').",
        },
        "explicit_max_ttl": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Hard cap the token cannot be renewed past (Go duration string).",
        },
        "display_name": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Human-readable display name stamped on the token.",
        },
        "num_uses": {
            "type": "integer",
            "minimum": 0,
            "description": "Use-count limit (0 = unlimited). A one-shot token sets 1.",
        },
        "renewable": {
            "type": "boolean",
            "description": "Whether the token can be renewed (default true).",
        },
        "no_parent": {
            "type": "boolean",
            "description": "Create an orphan token with no parent (survives parent revocation).",
        },
        "ttl_period": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "Period for a periodic token (Go duration string). Forwarded as "
                "hvac's period= argument."
            ),
        },
        "entity_alias": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": "Name of an entity alias to associate the token with.",
        },
    },
    "required": [],
    "additionalProperties": False,
}

VAULT_TOKEN_CREATE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "accessor": {
            "type": "string",
            "description": (
                "The token accessor -- a reference handle (NOT the token "
                "secret) usable to look up or revoke the token via "
                "vault.token.revoke_accessor."
            ),
        },
        "policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Policies attached to the minted token.",
        },
    },
    "required": ["accessor"],
    "description": (
        "The new token's accessor and policies. MEHO never returns the new token itself."
    ),
}

VAULT_TOKEN_CREATE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Make a Vault client token with explicit policies/TTL and get "
        "back only its accessor. DANGEROUS, requires approval, and "
        "NON-IDEMPOTENT: every call makes a new token. MEHO never shows "
        "the new token. To get a token you can use, call "
        "vault.token.create_and_store instead: it makes an orphan token "
        "and writes it to a Vault KV path or a Kubernetes Secret. The "
        "accessor is a non-secret handle for vault.token.lookup_accessor "
        "and vault.token.revoke_accessor."
    ),
    "parameter_hints": {
        "policies": "Optional. Policy names attached to the token (the privilege grant).",
        "ttl": "Optional. Token TTL as a Go duration string ('1h').",
        "explicit_max_ttl": "Optional. Hard renewal cap (Go duration string).",
        "display_name": "Optional. Human-readable display name.",
        "num_uses": "Optional. Use-count limit (0 = unlimited; 1 = one-shot).",
        "renewable": "Optional. Whether the token can be renewed (default true).",
        "no_parent": "Optional. Create an orphan token.",
        "ttl_period": "Optional. Period for a periodic token (forwarded as hvac period=).",
        "entity_alias": "Optional. Entity alias name to associate.",
    },
    "output_shape": (
        "On success: {'accessor': ..., 'policies': [...]} -- no token. "
        "On failure: a connector_error OperationResult."
    ),
}


# --- token.revoke_accessor -------------------------------------------------

VAULT_TOKEN_REVOKE_ACCESSOR_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "accessor": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "The accessor of the single token to revoke (from a "
                "token.create response or token.list_accessors). A reference "
                "handle, not the token secret."
            ),
        },
    },
    "required": ["accessor"],
    "additionalProperties": False,
}

VAULT_TOKEN_REVOKE_ACCESSOR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "accessor": {"type": "string", "description": "The accessor revoked."},
        "revoked": {"type": "boolean", "description": "Always true on success."},
    },
    "required": ["accessor", "revoked"],
    "description": "Confirms the surgical, single-accessor revocation.",
}

VAULT_TOKEN_REVOKE_ACCESSOR_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Revoke ONE token by its accessor -- surgical, single-token "
        "revocation. DANGEROUS, requires approval. Use to revoke a "
        "specific leaked or stale token. There is intentionally NO "
        "bulk-revoke op: never revoke broadly to recover from one leak."
    ),
    "parameter_hints": {
        "accessor": "Required. The accessor of the single token to revoke.",
    },
    "output_shape": (
        "On success: {'accessor': ..., 'revoked': true}. On failure: a "
        "connector_error OperationResult."
    ),
}


# --- token.list_accessors --------------------------------------------------

VAULT_TOKEN_LIST_ACCESSORS_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}

VAULT_TOKEN_LIST_ACCESSORS_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Accessor handles for every active token. Accessors are "
                "references, not token secrets. Empty list when none exist."
            ),
        },
    },
    "required": ["keys"],
    "description": "The list of token accessors. Always carries a 'keys' list.",
}

VAULT_TOKEN_LIST_ACCESSORS_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Enumerate every active token by its accessor handle. Read-only; "
        "registered safe (no approval). Accessors are non-secret "
        "references usable to look up or surgically revoke one token "
        "(vault.token.lookup_accessor / vault.token.revoke_accessor) -- "
        "this never returns token secrets."
    ),
    "parameter_hints": {},
    "output_shape": (
        "On success: {'keys': [...]} -- accessor handles. An empty store yields {'keys': []}."
    ),
}


# --- token.lookup_accessor -------------------------------------------------

VAULT_TOKEN_LOOKUP_ACCESSOR_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "accessor": {
            "type": "string",
            "minLength": 1,
            "pattern": "\\S",
            "description": (
                "The accessor of the single token to look up (from a "
                "token.create response or token.list_accessors). A reference "
                "handle, not the token secret."
            ),
        },
    },
    "required": ["accessor"],
    "additionalProperties": False,
}

VAULT_TOKEN_LOOKUP_ACCESSOR_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "The token's metadata, unwrapped from Vault's {'data': {...}} "
        "envelope. Vault's lookup-accessor deliberately blanks the token id "
        "(the raw secret), so the accessor cannot recover the token. "
        "Carries at least policies / ttl / expire_time / period / "
        "display_name / creation_time / renewable; additional Vault fields "
        "(meta, path, orphan, entity_id, num_uses, ...) pass through."
    ),
    "properties": {
        "policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Policies attached to the token (the privilege grant).",
        },
        "ttl": {
            "type": "integer",
            "description": "Remaining time-to-live in seconds (0 for a non-expiring token).",
        },
        "expire_time": {
            "type": ["string", "null"],
            "description": "RFC3339 expiry timestamp; null for a non-expiring token.",
        },
        "period": {
            "type": ["integer", "null"],
            "description": (
                "Renewal period in seconds for a PERIODIC token (0/null "
                "otherwise) -- the field that answers 'when does the "
                "scheduler token's fuse blow' (evoila/meho#2328)."
            ),
        },
        "display_name": {
            "type": "string",
            "description": "Human-readable display name stamped on the token.",
        },
        "creation_time": {
            "type": "integer",
            "description": "Unix epoch seconds when the token was minted.",
        },
        "renewable": {
            "type": "boolean",
            "description": "Whether the token can be renewed.",
        },
    },
    "additionalProperties": True,
}

VAULT_TOKEN_LOOKUP_ACCESSOR_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Look up ONE token's metadata by its accessor -- its policies, "
        "remaining TTL, expiry timestamp, periodic-renewal period, display "
        "name, creation time, and renewable flag. Use to audit 'what "
        "policies does this token carry and when does it expire' (e.g. "
        "diagnosing a silently-dead periodic token) or an access review "
        "('any long-lived broad-policy tokens still active'). Read-only; "
        "registered safe (no approval) -- the accessor is a non-secret "
        "reference handle and Vault's lookup-accessor never returns the "
        "token secret. An unknown accessor surfaces as a connector_error "
        "(InvalidPath)."
    ),
    "parameter_hints": {
        "accessor": (
            "Required. The accessor of the token to look up (from "
            "list_accessors or a create response)."
        ),
    },
    "output_shape": (
        "On success: the token's metadata dict (policies, ttl, expire_time, "
        "period, display_name, creation_time, renewable, and additional "
        "Vault fields). On failure: a connector_error OperationResult "
        "(InvalidPath when the accessor does not exist)."
    ),
}

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema and guidance for ``vault.token.create_and_store``.

Sibling of :mod:`meho_backplane.connectors.vault.ops_token_schemas`, split
out so the token group's schema data stays under the file-size budget.
Pure data only.

The op makes a Vault token and writes it to the place the caller names.
The token is never a parameter and never in the result, so nothing in
these schemas is secret: the parameters hold the token settings and the
destination, the result holds the token's accessor (a handle that is not
secret), its expiry, its policies and where it was stored.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.vault.ops_token_role_schemas import _ROLE_NAME_PROPERTY

__all__ = [
    "DEFAULT_KV_FIELD",
    "VAULT_TOKEN_CREATE_AND_STORE_LLM_INSTRUCTIONS",
    "VAULT_TOKEN_CREATE_AND_STORE_PARAMETER_SCHEMA",
    "VAULT_TOKEN_CREATE_AND_STORE_RESPONSE_SCHEMA",
]

#: The KV field the token is written under when the caller names none.
DEFAULT_KV_FIELD = "token"


def _text(description: str) -> dict[str, Any]:
    """A non-empty string field."""
    return {"type": "string", "minLength": 1, "pattern": "\\S", "description": description}


_VAULT_KV_STORE: dict[str, Any] = {
    "type": "object",
    "description": (
        "Write the token to a KV v2 path on this same Vault target. Other "
        "fields at the path stay; the path is created when it does not exist."
    ),
    "properties": {
        "path": _text(
            "KV v2 path relative to the mount, for example 'apps/ci/vault'. "
            "No leading slash, no mount prefix."
        ),
        "field": _text(f"Field the token is written under. Default '{DEFAULT_KV_FIELD}'."),
        "mount": {
            "type": "string",
            "minLength": 1,
            "pattern": "^(?=.*\\S)[^/]+$",
            "description": "KV v2 mount (one path segment). Default 'secret'.",
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

_K8S_SECRET_STORE: dict[str, Any] = {
    "type": "object",
    "description": (
        "Write the token to one key of an existing Kubernetes Secret. The "
        "other keys stay. The Secret must already exist."
    ),
    "properties": {
        "target": _text("Name of the Kubernetes target (list_targets) the Secret is on."),
        "namespace": _text("Namespace of the Secret."),
        "name": _text("Name of the existing Secret."),
        "key": _text("Key of the Secret the token is written to."),
    },
    "required": ["target", "namespace", "name", "key"],
    "additionalProperties": False,
}

VAULT_TOKEN_CREATE_AND_STORE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_name": {
            **_ROLE_NAME_PROPERTY,
            "description": (
                "Vault token role to make the token from (recommended). The role's "
                "allowed_policies decide which policies the token may get, and the "
                "role must set orphan=true. Without it MEHO uses "
                "auth/token/create-orphan, and the token may only get policies "
                "MEHO's own Vault role has."
            ),
        },
        "policies": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "description": "Policy names for the token.",
        },
        "ttl": _text("Token TTL as a Go duration string, for example '24h'."),
        "explicit_max_ttl": _text("Hard limit the token cannot be renewed past."),
        "ttl_period": _text("Period for a periodic token (Go duration string)."),
        "num_uses": {
            "type": "integer",
            "minimum": 0,
            "description": "How many times the token may be used (0 = no limit).",
        },
        "display_name": _text("Display name stamped on the token."),
        "renewable": {
            "type": "boolean",
            "description": "Whether the token can be renewed (default true).",
        },
        "store": {
            "type": "object",
            "description": "Where to write the token. Exactly one of vault_kv or k8s_secret.",
            "properties": {"vault_kv": _VAULT_KV_STORE, "k8s_secret": _K8S_SECRET_STORE},
            "minProperties": 1,
            "maxProperties": 1,
            "additionalProperties": False,
        },
    },
    "required": ["store"],
    "additionalProperties": False,
    "examples": [
        {
            "role_name": "ci-runner",
            "ttl": "24h",
            "store": {"vault_kv": {"path": "apps/ci/vault", "field": "token"}},
        },
        {
            "role_name": "ci-runner",
            "ttl_period": "24h",
            "store": {
                "k8s_secret": {
                    "target": "cluster-a",
                    "namespace": "ci",
                    "name": "vault-token",
                    "key": "token",
                }
            },
        },
    ],
}

VAULT_TOKEN_CREATE_AND_STORE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "accessor": {
            "type": "string",
            "description": (
                "The token's accessor: a handle that is not secret. Use it with "
                "vault.token.lookup_accessor or vault.token.revoke_accessor."
            ),
        },
        "expire_time": {
            "type": ["string", "null"],
            "description": "When the token expires (RFC 3339, UTC); null when it does not.",
        },
        "ttl_seconds": {
            "type": "integer",
            "description": "The token's lifetime in seconds (0 = no expiry).",
        },
        "policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Policies the token has.",
        },
        "stored_to": {
            "type": "object",
            "description": "Where the token was written. Never the token itself.",
            "properties": {
                "kind": {"type": "string", "description": "'vault_kv' or 'k8s_secret'."},
            },
            "additionalProperties": True,
        },
    },
    "required": ["accessor", "expire_time", "ttl_seconds", "policies", "stored_to"],
}

VAULT_TOKEN_CREATE_AND_STORE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Make a new Vault token from a token role and write it straight to a "
        "Vault KV path or to one key of an existing Kubernetes Secret, so that "
        "nobody sees the token. Use this instead of vault.token.create when the "
        "token must end up stored somewhere. Dangerous; a person must approve."
    ),
    "preconditions": (
        "The token role exists and sets orphan=true (vault.token.role.write). "
        "This Vault target's role may call auth/token/create/<role_name>; for a "
        "vault_kv store it may also write the KV path. For a k8s_secret store "
        "the Secret exists and you may run k8s.secret.update on that target. "
        "MEHO checks all of this before it makes the token."
    ),
    "parameter_hints": {
        "role_name": "Recommended. A token role with orphan=true.",
        "store": (
            "Exactly one of {'vault_kv': {path, field?, mount?}} or "
            "{'k8s_secret': {target, namespace, name, key}}."
        ),
        "ttl": "Optional. Token TTL, for example '24h'.",
        "ttl_period": "Optional. Makes a periodic token that lives while it is renewed.",
    },
    "output_shape": (
        "{accessor, expire_time, ttl_seconds, policies, stored_to}. The token "
        "itself is never returned. When the write fails, MEHO revokes the new "
        "token; if that revoke also fails, the error names the accessor."
    ),
    "next_step": (
        "Check the token with vault.token.lookup_accessor using the accessor. "
        "Revoke it with vault.token.revoke_accessor when it is no longer needed."
    ),
    # Keep these fields in the inline answer even when a long policy list
    # is moved behind a result handle.
    "result_scalars": {"keys": ["accessor", "expire_time", "ttl_seconds"]},
    # The op is credential_mint, so the reducer copies no part of the answer
    # on its own. This hint keeps ``stored_to`` whole with the rows and its
    # place fields inline, so the caller still learns where the token went.
    "result_objects": {
        "objects": {
            "stored_to": ["kind", "mount", "path", "field", "target", "namespace", "name", "key"]
        }
    },
}

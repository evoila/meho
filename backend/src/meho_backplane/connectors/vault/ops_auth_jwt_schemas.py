# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""JSON Schema and guidance for the Vault JWT/OIDC login-method ops.

Sibling of :mod:`meho_backplane.connectors.vault.ops_auth_schemas`. Pure
data only: JSON Schema 2020-12 ``parameter_schema`` / ``response_schema``
documents and the ``llm_instructions`` payload the meta-tools inline
verbatim.

The JWT/OIDC login method is how people (and services) log in to Vault
with a token from an identity provider. One plugin serves both kinds of
mount, so a site often has an ``oidc`` mount and a ``jwt`` mount side by
side. Every op therefore takes the mount name as a parameter.

A login role (``auth/<mount>/role/<name>``) holds no secret. The mount
config can: ``oidc_client_secret`` and parts of ``provider_config``. The
config read returns only the fields listed in
:data:`VAULT_AUTH_JWT_CONFIG_FIELDS`, so those two are never returned.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "VAULT_AUTH_JWT_CONFIG_FIELDS",
    "VAULT_AUTH_JWT_CONFIG_READ_LLM_INSTRUCTIONS",
    "VAULT_AUTH_JWT_CONFIG_READ_PARAMETER_SCHEMA",
    "VAULT_AUTH_JWT_CONFIG_READ_RESPONSE_SCHEMA",
    "VAULT_AUTH_JWT_ROLE_LIST_LLM_INSTRUCTIONS",
    "VAULT_AUTH_JWT_ROLE_LIST_PARAMETER_SCHEMA",
    "VAULT_AUTH_JWT_ROLE_LIST_RESPONSE_SCHEMA",
    "VAULT_AUTH_JWT_ROLE_READ_LLM_INSTRUCTIONS",
    "VAULT_AUTH_JWT_ROLE_READ_PARAMETER_SCHEMA",
    "VAULT_AUTH_JWT_ROLE_READ_RESPONSE_SCHEMA",
    "VAULT_AUTH_JWT_ROLE_WRITE_FIELDS",
    "VAULT_AUTH_JWT_ROLE_WRITE_LLM_INSTRUCTIONS",
    "VAULT_AUTH_JWT_ROLE_WRITE_PARAMETER_SCHEMA",
    "VAULT_AUTH_JWT_ROLE_WRITE_RESPONSE_SCHEMA",
    "VAULT_AUTH_JWT_WRITE_NOTE",
]

#: The mount becomes the middle of the Vault path ``auth/<mount>/...``.
#: Each ``/``-separated part starts with a letter, digit or ``_``, so a
#: part can never be ``..`` and the call can never leave ``auth/``.
_MOUNT_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$",
    "default": "jwt",
    "description": (
        "Mount path of the JWT/OIDC login method, with no 'auth/' prefix and no "
        "slash at either end. Default 'jwt'. Use 'oidc' (or the site's own name) "
        "for the other mount; vault.sys.auth.list shows the mounts of type 'jwt' "
        "and 'oidc'."
    ),
}

#: The role name becomes the last part of ``auth/<mount>/role/<name>``.
#: Same rule Vault uses for a role name, so it can never carry a ``/``.
_ROLE_NAME_PROPERTY: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "^[A-Za-z0-9_]([A-Za-z0-9_.-]*[A-Za-z0-9_])?$",
    "description": (
        "Name of the login role. Letters, digits, '_', '-' and '.' only. It "
        "becomes the last part of the Vault path auth/<mount>/role/<name>."
    ),
}

_MOUNT_HINT = (
    "Optional. Default 'jwt'. Pass 'oidc' or the site's own mount name for the other mount."
)

_STRING_LIST: dict[str, Any] = {"type": "string", "minLength": 1}


def _duration(description: str) -> dict[str, Any]:
    """A duration: seconds as an integer, or a Go duration string such as ``'1h'``."""
    return {
        "type": ["string", "integer"],
        "minimum": 0,
        "pattern": "\\S",
        "description": description,
    }


def _string_list(description: str) -> dict[str, Any]:
    return {"type": "array", "items": _STRING_LIST, "description": description}


# --- auth.jwt.role.write ---------------------------------------------------

_ROLE_WRITE_PROPERTIES: dict[str, Any] = {
    "role_type": {
        "type": "string",
        "enum": ["jwt", "oidc"],
        "description": (
            "'oidc' for a browser login through the identity provider, 'jwt' for a "
            "login with a token the caller already has. Vault default: 'oidc'."
        ),
    },
    "user_claim": {
        "type": "string",
        "minLength": 1,
        "description": (
            "Token claim that names the user, for example 'sub' or 'email'. "
            "Vault needs it when the role is created."
        ),
    },
    "user_claim_json_pointer": {
        "type": "boolean",
        "description": "True: user_claim is a JSON pointer such as '/user/id'.",
    },
    "bound_audiences": _string_list(
        "The 'aud' values a token must carry. Needed for 'jwt' roles whose tokens "
        "carry an audience."
    ),
    "bound_subject": {
        "type": "string",
        "description": "The 'sub' value a token must carry. Empty means any subject.",
    },
    "bound_claims": {
        "type": "object",
        "description": (
            "Claims a token must carry, as claim name to expected value (a string, "
            "number, bool, or a list of allowed values)."
        ),
    },
    "bound_claims_type": {
        "type": "string",
        "enum": ["string", "glob"],
        "description": "'string' (exact match, Vault default) or 'glob' ('*' matches anything).",
    },
    "groups_claim": {
        "type": "string",
        "description": "Token claim that lists the user's groups. Must be a list of strings.",
    },
    "claim_mappings": {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "Token claim name to metadata key, copied onto the Vault token.",
    },
    "oidc_scopes": _string_list(
        "Extra OIDC scopes to ask the identity provider for. 'openid' is always added."
    ),
    "allowed_redirect_uris": _string_list(
        "Redirect URIs the browser may come back to after an 'oidc' login. Vault "
        "needs at least one for an 'oidc' role."
    ),
    "verbose_oidc_logging": {
        "type": "boolean",
        "description": (
            "True writes OIDC replies to the Vault log. They can hold personal data; "
            "use it only to debug, never in production."
        ),
    },
    "max_age": _duration(
        "Most time since the user last logged in at the identity provider, for "
        "'oidc' roles. Seconds or a Go duration string such as '1h'."
    ),
    "clock_skew_leeway": _duration("Allowed clock difference for 'jwt' roles. Vault default 60s."),
    "expiration_leeway": _duration(
        "Extra time a token is accepted after its 'exp', for 'jwt' roles. Vault default 150s."
    ),
    "not_before_leeway": _duration(
        "Extra time a token is accepted before its 'nbf', for 'jwt' roles. Vault default 150s."
    ),
    "token_ttl": _duration(
        "Lifetime of a Vault token from a new login, before renewal. Seconds or a Go "
        "duration string such as '1h'. 0 means the mount or system default."
    ),
    "token_max_ttl": _duration(
        "Most total lifetime of a Vault token from a new login, with renewals. "
        "0 means the mount or system default."
    ),
    "token_explicit_max_ttl": _duration(
        "Hard limit on the total lifetime of a Vault token from a new login. 0 means none."
    ),
    "token_period": _duration(
        "Period for periodic tokens: each renewal gives the token this much time again."
    ),
    "token_policies": _string_list("Policies a Vault token from a new login gets."),
    "token_bound_cidrs": _string_list(
        "Network ranges (CIDR) the Vault token may be used from, for example '10.0.0.0/8'."
    ),
    "token_no_default_policy": {
        "type": "boolean",
        "description": "True: the Vault token does not get the 'default' policy.",
    },
    "token_num_uses": {
        "type": "integer",
        "minimum": 0,
        "description": "How many times the Vault token may be used. 0 means no limit.",
    },
    "token_type": {
        "type": "string",
        "enum": ["service", "batch", "default"],
        "description": "Kind of Vault token a login makes: 'service', 'batch' or 'default'.",
    },
}

#: Role settings forwarded to Vault verbatim when the caller gives them.
VAULT_AUTH_JWT_ROLE_WRITE_FIELDS: tuple[str, ...] = tuple(_ROLE_WRITE_PROPERTIES)

#: Returned on every successful role write. Vault does not change tokens
#: that a login made before the write.
VAULT_AUTH_JWT_WRITE_NOTE = (
    "Only new logins get the new settings. Tokens issued before this change keep "
    "their old lifetime and policies until they expire. A read made with such an "
    "old token still shows that token's old lease, not a failed write."
)

VAULT_AUTH_JWT_ROLE_WRITE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": _MOUNT_PROPERTY,
        "role_name": _ROLE_NAME_PROPERTY,
        **_ROLE_WRITE_PROPERTIES,
    },
    "required": ["role_name"],
    "additionalProperties": False,
    "examples": [
        {"mount": "jwt", "role_name": "ci-login", "token_ttl": "4h"},
        {
            "mount": "oidc",
            "role_name": "operators",
            "role_type": "oidc",
            "user_claim": "sub",
            "groups_claim": "groups",
            "allowed_redirect_uris": [
                "https://vault.example.test/ui/vault/auth/oidc/oidc/callback"
            ],
            "token_policies": ["operator"],
            "token_ttl": "1h",
        },
    ],
}

VAULT_AUTH_JWT_ROLE_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": {"type": "string", "description": "The login-method mount written to."},
        "role_name": {"type": "string", "description": "The login role created or changed."},
        "written": {"type": "boolean", "description": "Always true on success."},
        "settings": {
            "type": "object",
            "description": (
                "The settings this call sent, by field name. A field the caller did "
                "not give is not listed and keeps its current value in Vault."
            ),
        },
        "note": {
            "type": "string",
            "description": "Says that only new logins get the new settings.",
        },
    },
    "required": ["mount", "role_name", "written", "settings", "note"],
    "description": "Confirms the write. Use vault.auth.jwt.role.read to see the whole role.",
}

VAULT_AUTH_JWT_ROLE_WRITE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Create a JWT/OIDC login role, or change one: for example the token "
        "lifetime (token_ttl), the policies a login gets (token_policies), or "
        "the accepted audiences and redirect URIs. Dangerous and needs approval: "
        "the role decides who can log in to Vault and what they can do. Only the "
        "fields you give are sent. Vault keeps every other field of an existing "
        "role as it is."
    ),
    "preconditions": (
        "The target's Vault role needs 'create' and 'update' on "
        "auth/<mount>/role/<role_name>. MEHO checks this before it asks for "
        "approval; when a right is missing the call is refused with error_code "
        "'write_capability_denied' and no approval is requested. Read the role "
        "first (vault.auth.jwt.role.read) so the approver sees the current "
        "values. A new role needs user_claim, and an 'oidc' role needs "
        "allowed_redirect_uris."
    ),
    "parameter_hints": {
        "mount": _MOUNT_HINT,
        "role_name": "Required. The login role name.",
        "token_ttl": "Optional. Seconds or a Go duration string such as '1h'.",
        "token_policies": "Optional. Replaces the whole policy list; send every policy.",
        "bound_audiences": "Optional. Replaces the whole list; send every audience.",
        "allowed_redirect_uris": "Optional. Replaces the whole list; send every URI.",
    },
    "output_shape": (
        "On success: {'mount': ..., 'role_name': ..., 'written': true, "
        "'settings': {<the fields sent>}, 'note': ...}. The note says that only "
        "new logins get the new settings; tokens issued earlier keep their old "
        "lifetime. Before approval, a missing Vault right gives status 'denied' "
        "with error_code 'write_capability_denied'. On failure after approval: a "
        "connector_error result."
    ),
    "next_step": (
        "Call vault.auth.jwt.role.read to check the role as Vault stores it. To "
        "see a new token_ttl on a real token, log in again; an old token keeps "
        "its old lease."
    ),
}


# --- auth.jwt.role.read ----------------------------------------------------

VAULT_AUTH_JWT_ROLE_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"mount": _MOUNT_PROPERTY, "role_name": _ROLE_NAME_PROPERTY},
    "required": ["role_name"],
    "additionalProperties": False,
    "examples": [{"mount": "jwt", "role_name": "ci-login"}],
}

VAULT_AUTH_JWT_ROLE_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "role_type": {"type": "string", "description": "'jwt' or 'oidc'."},
        "user_claim": {"type": "string", "description": "Token claim that names the user."},
        "groups_claim": {"type": "string", "description": "Token claim that lists the groups."},
        "bound_audiences": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The 'aud' values a token must carry.",
        },
        "bound_subject": {"type": "string", "description": "The 'sub' value a token must carry."},
        "bound_claims": {"type": ["object", "null"], "description": "Claims a token must carry."},
        "allowed_redirect_uris": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Redirect URIs allowed after an 'oidc' login.",
        },
        "token_ttl": {
            "type": "integer",
            "description": "Lifetime in seconds of a token from a new login; 0 means the default.",
        },
        "token_max_ttl": {
            "type": "integer",
            "description": "Most total lifetime in seconds; 0 means the default.",
        },
        "token_period": {"type": "integer", "description": "Period in seconds; 0 means none."},
        "token_policies": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Policies a token from a new login gets.",
        },
        "token_type": {"type": "string", "description": "Kind of token a login makes."},
    },
    "description": (
        "The role settings as Vault stores them, unwrapped from Vault's data "
        "envelope. Durations come back as seconds. Vault adds more fields; a role "
        "holds no secret."
    ),
}

VAULT_AUTH_JWT_ROLE_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Read one JWT/OIDC login role: token_ttl, token_max_ttl, token_period, "
        "token_policies, bound_audiences, allowed_redirect_uris, user_claim and "
        "groups_claim. This is where 'why does my Vault token expire so fast?' "
        "is answered: token_ttl on the role sets the lifetime of a new login. "
        "Read-only, safe, no approval."
    ),
    "parameter_hints": {"mount": _MOUNT_HINT, "role_name": "Required. The login role name."},
    "output_shape": (
        "On success: the role settings as a flat object, for example "
        "{'role_type': 'jwt', 'user_claim': 'sub', 'token_ttl': 3600, "
        "'token_policies': ['meho'], ...}. Durations are seconds; 0 means the "
        "mount or system default (see vault.sys.auth.tune). An unknown role, or "
        "a mount with no JWT/OIDC method, is a connector_error with "
        "exception_class 'InvalidPath'."
    ),
    "next_step": (
        "To change a setting, call vault.auth.jwt.role.write with the role_name "
        "and only the fields to change. vault.auth.jwt.role.list shows every "
        "role name on the mount."
    ),
}


# --- auth.jwt.role.list ----------------------------------------------------

VAULT_AUTH_JWT_ROLE_LIST_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"mount": _MOUNT_PROPERTY},
    "required": [],
    "additionalProperties": False,
    "examples": [{}, {"mount": "oidc"}],
}

VAULT_AUTH_JWT_ROLE_LIST_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": {"type": "string", "description": "The login-method mount listed."},
        "keys": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The login role names. Empty list when there are none.",
        },
    },
    "required": ["mount", "keys"],
    "description": "The login role names on one mount. Always carries a 'keys' list.",
}

VAULT_AUTH_JWT_ROLE_LIST_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "List the login role names on one JWT/OIDC mount. Use it to find a role "
        "before you read or change it. Read-only, safe, no approval."
    ),
    "parameter_hints": {"mount": _MOUNT_HINT},
    "output_shape": (
        "On success: {'mount': 'jwt', 'keys': [<role names>]}. Vault answers "
        "'not found' both for a mount with no roles and for a path where no "
        "JWT/OIDC method is mounted; both give {'keys': []}. Check the mount "
        "name with vault.sys.auth.list when the list is empty."
    ),
    "next_step": "Call vault.auth.jwt.role.read with a name from the list.",
}


# --- auth.jwt.config.read --------------------------------------------------

#: The only config fields the read returns. ``oidc_client_secret`` is not
#: here, and neither is ``provider_config`` (it can hold a provider key).
VAULT_AUTH_JWT_CONFIG_FIELDS: tuple[str, ...] = (
    "oidc_discovery_url",
    "oidc_discovery_ca_pem",
    "oidc_client_id",
    "oidc_response_mode",
    "oidc_response_types",
    "jwks_url",
    "jwks_ca_pem",
    "jwks_pairs",
    "jwt_validation_pubkeys",
    "jwt_supported_algs",
    "bound_issuer",
    "default_role",
    "namespace_in_state",
    "unsupported_critical_cert_extensions",
)

VAULT_AUTH_JWT_CONFIG_READ_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"mount": _MOUNT_PROPERTY},
    "required": [],
    "additionalProperties": False,
    "examples": [{}, {"mount": "oidc"}],
}

VAULT_AUTH_JWT_CONFIG_READ_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "mount": {"type": "string", "description": "The login-method mount read."},
        "oidc_discovery_url": {
            "type": "string",
            "description": "Identity provider address Vault finds its keys at.",
        },
        "oidc_client_id": {"type": "string", "description": "OIDC client id Vault uses."},
        "jwks_url": {"type": "string", "description": "Address of the key set, when used."},
        "bound_issuer": {"type": "string", "description": "The 'iss' value a token must carry."},
        "default_role": {"type": "string", "description": "Role used when a login names none."},
        "jwt_supported_algs": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Signing algorithms Vault accepts.",
        },
    },
    "required": ["mount"],
    "description": (
        "The mount config without secrets: only the fields named in the op "
        "description. oidc_client_secret and provider_config are never returned."
    ),
}

VAULT_AUTH_JWT_CONFIG_READ_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_call": (
        "Read how a JWT/OIDC mount checks tokens: the identity provider address "
        "(oidc_discovery_url or jwks_url), the client id, the accepted issuer "
        "and algorithms, and the default role. Read-only, safe, no approval. "
        "The OIDC client secret and provider_config are never returned."
    ),
    "parameter_hints": {"mount": _MOUNT_HINT},
    "output_shape": (
        "On success: {'mount': 'jwt', 'oidc_discovery_url': ..., "
        "'oidc_client_id': ..., 'bound_issuer': ..., 'default_role': ..., ...}. "
        "A mount with no JWT/OIDC method, or one not set up yet, is a "
        "connector_error with exception_class 'InvalidPath'."
    ),
    "next_step": (
        "Call vault.auth.jwt.role.list to see the login roles on the mount, or "
        "vault.sys.auth.tune for the mount's default token lifetimes."
    ),
}

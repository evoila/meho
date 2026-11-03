# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault JWT/OIDC login-method op handlers, spec table and registration.

The JWT/OIDC login method lets people and services log in to Vault with a
token from an identity provider. MEHO itself logs in this way. A login
role at ``auth/<mount>/role/<name>`` sets, among other things, how long a
token from a new login lives (``token_ttl``) and which policies it gets.

Four ops in the ``auth`` group:

* ``vault.auth.jwt.role.list`` -- ``LIST auth/<mount>/role`` (safe). Vault
  answers ``404`` both for an empty mount and for a missing mount; both
  give ``{"keys": []}``.
* ``vault.auth.jwt.role.read`` -- ``GET auth/<mount>/role/<name>`` (safe).
* ``vault.auth.jwt.role.write`` -- ``POST auth/<mount>/role/<name>``
  (dangerous, needs approval).
* ``vault.auth.jwt.config.read`` -- ``GET auth/<mount>/config`` (safe).
  Returns only the fields in
  :data:`~meho_backplane.connectors.vault.ops_auth_jwt_schemas.VAULT_AUTH_JWT_CONFIG_FIELDS`,
  so ``oidc_client_secret`` and ``provider_config`` never leave Vault
  through MEHO.

The mount is a parameter (default ``jwt``): one plugin serves both
``jwt`` and ``oidc`` mounts, and a site often runs one of each.

The write must keep every field the caller does not give. Vault's role
write keeps most fields that are not in the request body, with two
exceptions: when ``role_type`` or ``bound_claims_type`` is missing, Vault
sets it back to its default (``oidc`` / ``string``). So a ``token_ttl``
change alone would turn a ``jwt`` role into an ``oidc`` role. The handler
therefore reads the current role first and sends those two values again
when the caller did not give them. A new role (Vault ``404`` on the read)
gets only the given fields. hvac's ``auth.jwt.create_role`` is not used:
it always sends ``role_type``, ``bound_claims_type`` and
``verbose_oidc_logging`` with its own defaults. The handlers call
``client.adapter`` on the same API paths hvac uses. Tokens issued before
a write keep their old lease; the write's answer says so
(:data:`~meho_backplane.connectors.vault.ops_auth_jwt_schemas.VAULT_AUTH_JWT_WRITE_NOTE`).

Login goes through
:func:`~meho_backplane.connectors.vault.target_auth.vault_client_for_target`,
so the target's ``extras["vault_role"]`` decides which Vault role reads
or makes the change. Before MEHO asks for approval of a write,
``sys/capabilities-self`` must grant ``create``, ``read`` and ``update`` on
``auth/<mount>/role/<name>``; otherwise the call is refused with
``write_capability_denied``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import hvac.exceptions

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.ops import vault_capability_preflight
from meho_backplane.connectors.vault.ops_auth import (
    AUTH_WHEN_TO_USE,
    _extract_data,
    _extract_keys,
)
from meho_backplane.connectors.vault.ops_auth_jwt_schemas import (
    VAULT_AUTH_JWT_CONFIG_FIELDS,
    VAULT_AUTH_JWT_CONFIG_READ_LLM_INSTRUCTIONS,
    VAULT_AUTH_JWT_CONFIG_READ_PARAMETER_SCHEMA,
    VAULT_AUTH_JWT_CONFIG_READ_RESPONSE_SCHEMA,
    VAULT_AUTH_JWT_ROLE_LIST_LLM_INSTRUCTIONS,
    VAULT_AUTH_JWT_ROLE_LIST_PARAMETER_SCHEMA,
    VAULT_AUTH_JWT_ROLE_LIST_RESPONSE_SCHEMA,
    VAULT_AUTH_JWT_ROLE_READ_LLM_INSTRUCTIONS,
    VAULT_AUTH_JWT_ROLE_READ_PARAMETER_SCHEMA,
    VAULT_AUTH_JWT_ROLE_READ_RESPONSE_SCHEMA,
    VAULT_AUTH_JWT_ROLE_WRITE_FIELDS,
    VAULT_AUTH_JWT_ROLE_WRITE_LLM_INSTRUCTIONS,
    VAULT_AUTH_JWT_ROLE_WRITE_PARAMETER_SCHEMA,
    VAULT_AUTH_JWT_ROLE_WRITE_RESPONSE_SCHEMA,
    VAULT_AUTH_JWT_WRITE_NOTE,
)
from meho_backplane.connectors.vault.target_auth import vault_client_for_target
from meho_backplane.operations._preview import PreviewContext, register_permission_preflight
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "AUTH_JWT_OP_CONFIG_READ",
    "AUTH_JWT_OP_ROLE_LIST",
    "AUTH_JWT_OP_ROLE_READ",
    "AUTH_JWT_OP_ROLE_WRITE",
    "AUTH_JWT_OP_SPECS",
    "AUTH_JWT_ROLE_WRITE_CAPABILITIES",
    "register_vault_auth_jwt_operations",
    "vault_auth_jwt_config_read",
    "vault_auth_jwt_role_list",
    "vault_auth_jwt_role_read",
    "vault_auth_jwt_role_write",
    "vault_auth_jwt_role_write_capability_preflight",
]

AUTH_JWT_OP_ROLE_LIST = "vault.auth.jwt.role.list"
AUTH_JWT_OP_ROLE_READ = "vault.auth.jwt.role.read"
AUTH_JWT_OP_ROLE_WRITE = "vault.auth.jwt.role.write"
AUTH_JWT_OP_CONFIG_READ = "vault.auth.jwt.config.read"

#: Vault asks for ``create`` when the role does not exist yet and
#: ``update`` when it does, so an unconditional write needs both. The
#: write reads the current role first, so it needs ``read`` too.
AUTH_JWT_ROLE_WRITE_CAPABILITIES: frozenset[str] = frozenset({"create", "read", "update"})

#: Fields Vault sets back to their default when a role write leaves them
#: out. The write copies them from the current role when not given.
_FIELDS_VAULT_RESETS: tuple[str, ...] = ("role_type", "bound_claims_type")

_DEFAULT_MOUNT = "jwt"


def _mount(params: dict[str, Any]) -> str:
    return str(params.get("mount") or _DEFAULT_MOUNT).strip()


def _role_auth_path(mount: str, role_name: str) -> str:
    """The path Vault authorizes a login role on: ``auth/<mount>/role/<name>``."""
    return f"auth/{mount}/role/{role_name}"


async def vault_auth_jwt_role_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List the login role names on a JWT/OIDC mount (``LIST auth/<mount>/role``)."""
    mount = _mount(params)
    async with vault_client_for_target(operator, target) as client:
        try:
            payload = await asyncio.to_thread(client.adapter.list, f"/v1/auth/{mount}/role")
        except hvac.exceptions.InvalidPath:
            return {"mount": mount, "keys": []}
    return {"mount": mount, "keys": _extract_keys(payload)}


async def vault_auth_jwt_role_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read one login role's settings (``GET auth/<mount>/role/<name>``).

    An unknown role or mount surfaces as :class:`hvac.exceptions.InvalidPath`,
    which the dispatcher turns into a ``connector_error``.
    """
    path = _role_auth_path(_mount(params), str(params["role_name"]).strip())
    async with vault_client_for_target(operator, target) as client:
        payload = await asyncio.to_thread(client.adapter.get, f"/v1/{path}")
    return _extract_data(payload)


async def vault_auth_jwt_role_write(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Create a login role, or change one (``POST auth/<mount>/role/<name>``).

    Sends the settings the caller gave. For an existing role it also sends
    the current ``role_type`` / ``bound_claims_type`` when the caller did
    not give them, because Vault would reset those two (see the module
    docstring). Returns the given settings, the kept values, and a note
    that only new logins get the change.
    """
    mount = _mount(params)
    role_name = str(params["role_name"]).strip()
    api_path = f"/v1/{_role_auth_path(mount, role_name)}"
    settings: dict[str, Any] = {
        field: params[field]
        for field in VAULT_AUTH_JWT_ROLE_WRITE_FIELDS
        if params.get(field) is not None
    }
    missing = [field for field in _FIELDS_VAULT_RESETS if field not in settings]
    kept: dict[str, Any] = {}
    async with vault_client_for_target(operator, target) as client:
        if missing:
            try:
                current = _extract_data(await asyncio.to_thread(client.adapter.get, api_path))
            except hvac.exceptions.InvalidPath:
                current = {}
            kept = {field: current[field] for field in missing if current.get(field)}
        await asyncio.to_thread(client.adapter.post, api_path, json=settings | kept)
    return {
        "mount": mount,
        "role_name": role_name,
        "written": True,
        "settings": settings,
        "kept_from_current_role": kept,
        "note": VAULT_AUTH_JWT_WRITE_NOTE,
    }


async def vault_auth_jwt_config_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read a JWT/OIDC mount's config without secrets (``GET auth/<mount>/config``).

    Keeps only :data:`VAULT_AUTH_JWT_CONFIG_FIELDS`. A field Vault adds
    later is dropped until it is reviewed and added there.
    """
    mount = _mount(params)
    async with vault_client_for_target(operator, target) as client:
        payload = await asyncio.to_thread(client.adapter.get, f"/v1/auth/{mount}/config")
    data = _extract_data(payload)
    return {"mount": mount} | {k: data[k] for k in VAULT_AUTH_JWT_CONFIG_FIELDS if k in data}


async def vault_auth_jwt_role_write_capability_preflight(
    operator: Operator, params: dict[str, Any], target: Any = None
) -> dict[str, Any] | None:
    """Check whether the target's role may write ``auth/<mount>/role/<name>``.

    Runs at approval-park time through the shared
    :func:`~meho_backplane.connectors.vault.ops.vault_capability_preflight`
    probe. Returns its secret-free summary, or ``None`` when the probe
    itself fails.
    """
    path = _role_auth_path(_mount(params), str(params.get("role_name", "")).strip())
    return await vault_capability_preflight(
        operator, AUTH_JWT_OP_ROLE_WRITE, path, AUTH_JWT_ROLE_WRITE_CAPABILITIES, target
    )


async def _role_write_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    """Permission preflight hook for ``vault.auth.jwt.role.write``."""
    return await vault_auth_jwt_role_write_capability_preflight(
        ctx.operator, ctx.params, target=ctx.target
    )


register_permission_preflight(AUTH_JWT_OP_ROLE_WRITE, _role_write_preflight)


# --- spec table ------------------------------------------------------------

AUTH_JWT_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": AUTH_JWT_OP_ROLE_LIST,
        "handler": vault_auth_jwt_role_list,
        "summary": "List the login role names on a Vault JWT/OIDC mount.",
        "description": (
            "Lists the login role names on one JWT/OIDC login mount (LIST "
            "auth/<mount>/role). Mount is a parameter, default 'jwt'. Names "
            "only. Read-only; safe, no approval. No roles, or no such mount, "
            "gives {'keys': []}."
        ),
        "parameter_schema": VAULT_AUTH_JWT_ROLE_LIST_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_JWT_ROLE_LIST_RESPONSE_SCHEMA,
        "tags": ["read-only", "auth", "identity", "jwt", "oidc"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_AUTH_JWT_ROLE_LIST_LLM_INSTRUCTIONS,
    },
    {
        "op_id": AUTH_JWT_OP_ROLE_READ,
        "handler": vault_auth_jwt_role_read,
        "summary": "Read one Vault JWT/OIDC login role: token_ttl, policies, audiences.",
        "description": (
            "Reads one JWT/OIDC login role (GET auth/<mount>/role/<name>): "
            "token_ttl, token_max_ttl, token_policies, bound_audiences, "
            "allowed_redirect_uris, user_claim, groups_claim. Holds no secret. "
            "Read-only; safe, no approval."
        ),
        "parameter_schema": VAULT_AUTH_JWT_ROLE_READ_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_JWT_ROLE_READ_RESPONSE_SCHEMA,
        "tags": ["read-only", "auth", "identity", "jwt", "oidc"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_AUTH_JWT_ROLE_READ_LLM_INSTRUCTIONS,
    },
    {
        "op_id": AUTH_JWT_OP_ROLE_WRITE,
        "handler": vault_auth_jwt_role_write,
        "summary": "Create a Vault JWT/OIDC login role, or change one.",
        "description": (
            "Creates or changes a JWT/OIDC login role (POST "
            "auth/<mount>/role/<name>). Fields not given keep their value. "
            "Tokens issued before the change keep their old lifetime. "
            "Dangerous; needs approval."
        ),
        "parameter_schema": VAULT_AUTH_JWT_ROLE_WRITE_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_JWT_ROLE_WRITE_RESPONSE_SCHEMA,
        "tags": ["write", "auth", "identity", "jwt", "oidc"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VAULT_AUTH_JWT_ROLE_WRITE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": AUTH_JWT_OP_CONFIG_READ,
        "handler": vault_auth_jwt_config_read,
        "summary": "Read a Vault JWT/OIDC mount's config, without the client secret.",
        "description": (
            "Reads how a JWT/OIDC login mount checks tokens (GET "
            "auth/<mount>/config): discovery URL or JWKS URL, client id, "
            "issuer, algorithms, default role. oidc_client_secret and "
            "provider_config are never returned. Read-only; safe, no approval."
        ),
        "parameter_schema": VAULT_AUTH_JWT_CONFIG_READ_PARAMETER_SCHEMA,
        "response_schema": VAULT_AUTH_JWT_CONFIG_READ_RESPONSE_SCHEMA,
        "tags": ["read-only", "auth", "identity", "jwt", "oidc"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_AUTH_JWT_CONFIG_READ_LLM_INSTRUCTIONS,
    },
)


async def register_vault_auth_jwt_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the four JWT/OIDC login-method ops into ``endpoint_descriptor``.

    Called from
    :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`
    after the userpass / approle registrars, all in the ``auth`` group.
    Idempotent through the body-hash skip path in
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
    """
    for spec in AUTH_JWT_OP_SPECS:
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=spec["op_id"],
            handler=spec["handler"],
            summary=spec["summary"],
            description=spec["description"],
            parameter_schema=spec["parameter_schema"],
            response_schema=spec["response_schema"],
            group_key="auth",
            when_to_use=AUTH_WHEN_TO_USE,
            tags=spec["tags"],
            safety_level=spec["safety_level"],
            requires_approval=spec["requires_approval"],
            llm_instructions=spec["llm_instructions"],
            embedding_service=embedding_service,
        )

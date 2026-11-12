# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Switch a Vault userpass login off and on without deleting it.

* ``vault.auth.userpass.disable`` -- sets ``disabled`` on the identity entity
  of the login; with ``revoke_tokens`` it also revokes the tokens the user
  already has.
* ``vault.auth.userpass.enable`` -- sets ``disabled`` back to false.

Vault has no switch on the userpass login itself. The switch is on the
identity entity that the login's alias points to. The alias is found by its
name and the mount accessor. Vault writes the alias name in lower case, so
the name here is lower case too. When a login has no entity yet, ``disable``
creates a new entity (Vault picks its name) and the alias first, so the switch
holds from the next login on. It never reuses an entity found by name: a name
does not prove that the entity belongs to this login. ``enable`` does nothing
for a login without an entity: Vault makes one at the first login, enabled.

Vault's documentation says that the tokens of a disabled entity cannot be
used, but are not revoked. To revoke them, ``revoke_tokens`` calls
``sys/leases/revoke-prefix`` with the path ``auth/<mount>/login/<name>/``.
Vault stores each login token under the path of its login request, and
revoke-prefix revokes tokens by that path (Vault source:
``vault/request_handling.go`` and ``vault/expiration.go``; the docs name the
endpoint). A login typed with other capital letters (``ALICE``) has its own
path, so the op first lists ``sys/leases/lookup/auth/<mount>/login/`` and
revokes every spelling of the username it finds there. Both calls need
``sudo``, and Vault revokes in the background, so the result says
"requested".
"""

from __future__ import annotations

import asyncio
from typing import Any

import hvac.exceptions

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.ops_auth import VaultAuthBackendNotMountedError
from meho_backplane.connectors.vault.ops_userpass_lifecycle_schemas import (
    VAULT_AUTH_USERPASS_DISABLE_LLM_INSTRUCTIONS,
    VAULT_AUTH_USERPASS_DISABLE_PARAMETER_SCHEMA,
    VAULT_AUTH_USERPASS_ENABLE_LLM_INSTRUCTIONS,
    VAULT_AUTH_USERPASS_ENABLE_PARAMETER_SCHEMA,
    VAULT_AUTH_USERPASS_STATE_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vault.target_auth import vault_client_for_target
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "VaultUserpassUserNotFoundError",
    "register_vault_userpass_lifecycle_operations",
    "vault_auth_userpass_disable",
    "vault_auth_userpass_enable",
]


class VaultUserpassUserNotFoundError(Exception):
    """The userpass login does not exist on the mount."""


def _clean_inputs(params: dict[str, Any]) -> tuple[str, str]:
    """Return ``(username, mount)`` and refuse a value that could change a URL path."""
    username = str(params["username"]).strip()
    mount = str(params.get("mount", "userpass")).strip().strip("/")
    if not username or "/" in username or username in (".", ".."):
        raise ValueError("username must not be empty or contain '/'")
    if not mount or any(part in ("", ".", "..") for part in mount.split("/")):
        raise ValueError("mount must be a plain mount path such as 'userpass'")
    return username, mount


def _mount_accessor(client: Any, mount: str) -> str:
    payload = client.sys.list_auth_methods()
    methods = payload.get("data", payload) if isinstance(payload, dict) else {}
    entry = methods.get(f"{mount}/")
    if not isinstance(entry, dict) or not entry.get("accessor"):
        raise VaultAuthBackendNotMountedError(f"userpass auth backend not mounted at {mount!r}")
    return str(entry["accessor"])


def _find_entity(client: Any, alias_name: str, accessor: str) -> dict[str, Any] | None:
    """Return the entity (``id``, ``disabled``, ...) behind the alias, or ``None``."""
    payload = client.secrets.identity.lookup_entity(
        alias_name=alias_name, alias_mount_accessor=accessor
    )
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) and data.get("id") else None


def _create_entity(client: Any, alias_name: str, accessor: str, mount: str) -> str:
    """Create a new entity and its alias for a login that has none; return the id.

    No name is sent, so Vault picks a unique one. Sending a name would update
    an entity that already has it, which may belong to someone else.
    """
    created = client.secrets.identity.create_or_update_entity(
        name=None, metadata={"userpass_mount": mount, "userpass_username": alias_name}
    )
    entity_id = str(created["data"]["id"])
    client.secrets.identity.create_or_update_entity_alias(
        name=alias_name, canonical_id=entity_id, mount_accessor=accessor
    )
    return entity_id


def _login_spellings(client: Any, mount: str, username: str) -> list[str]:
    """Return every spelling of *username* that holds a login token on *mount*."""
    try:
        payload = client.sys.list_leases(prefix=f"auth/{mount}/login/")
    except hvac.exceptions.InvalidPath:
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    keys = data.get("keys") if isinstance(data, dict) else None
    wanted = username.lower()
    return [
        str(key).rstrip("/")
        for key in keys or []
        if str(key).endswith("/") and str(key).rstrip("/").lower() == wanted
    ]


def _revoke_tokens(client: Any, mount: str, username: str) -> None:
    names = (username, username.lower(), *_login_spellings(client, mount, username))
    for name in dict.fromkeys(names):
        client.sys.revoke_prefix(prefix=f"auth/{mount}/login/{name}/")


def _switch_sync(
    client: Any, *, username: str, mount: str, disabled: bool, revoke_tokens: bool
) -> dict[str, Any]:
    accessor = _mount_accessor(client, mount)
    try:
        client.auth.userpass.read_user(username=username, mount_point=mount)
    except hvac.exceptions.InvalidPath as exc:
        raise VaultUserpassUserNotFoundError(
            f"userpass login {username!r} does not exist on mount {mount!r}"
        ) from exc

    alias_name = username.lower()
    entity = _find_entity(client, alias_name, accessor)
    entity_created = False
    if entity is None:
        if not disabled:
            # Nothing to switch on: Vault makes an enabled entity at the first login.
            return {
                "username": username,
                "mount": mount,
                "disabled": False,
                "changed": False,
                "entity_id": None,
                "entity_created": False,
            }
        entity_id = _create_entity(client, alias_name, accessor, mount)
        entity_created = True
        was_disabled = False
    else:
        entity_id = str(entity["id"])
        was_disabled = bool(entity.get("disabled", False))

    changed = was_disabled != disabled
    if changed:
        client.secrets.identity.update_entity(entity_id=entity_id, disabled=disabled)
    if revoke_tokens:
        _revoke_tokens(client, mount, username)

    result: dict[str, Any] = {
        "username": username,
        "mount": mount,
        "disabled": disabled,
        "changed": changed or entity_created,
        "entity_id": entity_id,
        "entity_created": entity_created,
    }
    if disabled:
        result["revoke_requested"] = revoke_tokens
    return result


async def _switch(
    operator: Operator, target: Any, params: dict[str, Any], *, disabled: bool
) -> dict[str, Any]:
    username, mount = _clean_inputs(params)
    revoke = bool(params.get("revoke_tokens", False)) and disabled
    async with vault_client_for_target(operator, target) as client:
        return await asyncio.to_thread(
            _switch_sync,
            client,
            username=username,
            mount=mount,
            disabled=disabled,
            revoke_tokens=revoke,
        )


async def vault_auth_userpass_disable(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Op-id ``vault.auth.userpass.disable``: switch a login off (safe to run twice)."""
    return await _switch(operator, target, params, disabled=True)


async def vault_auth_userpass_enable(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Op-id ``vault.auth.userpass.enable``: switch a login back on (safe to run twice)."""
    return await _switch(operator, target, params, disabled=False)


_WHEN_TO_USE = (
    "Use to switch a userpass login off or on without deleting it: "
    "``vault.auth.userpass.disable`` (optionally revoking the user's tokens) "
    "and ``vault.auth.userpass.enable``. Both change the identity entity of "
    "the login and need approval. To remove a login for good, use "
    "``vault.auth.userpass.delete``."
)

_LIFECYCLE_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": "vault.auth.userpass.disable",
        "handler": vault_auth_userpass_disable,
        "summary": "Switch a userpass login off without deleting it.",
        "description": (
            "Sets the identity entity of a userpass login to disabled, so the login "
            "stops working. Creates the entity and alias when missing. With "
            "revoke_tokens it also revokes the user's tokens. Safe to run twice."
        ),
        "parameter_schema": VAULT_AUTH_USERPASS_DISABLE_PARAMETER_SCHEMA,
        "llm_instructions": VAULT_AUTH_USERPASS_DISABLE_LLM_INSTRUCTIONS,
        "tags": ["write", "auth", "identity", "access-removal"],
    },
    {
        "op_id": "vault.auth.userpass.enable",
        "handler": vault_auth_userpass_enable,
        "summary": "Switch a disabled userpass login back on.",
        "description": (
            "Sets the identity entity of a userpass login back to enabled. The "
            "password and policies stay as they were. Safe to run twice."
        ),
        "parameter_schema": VAULT_AUTH_USERPASS_ENABLE_PARAMETER_SCHEMA,
        "llm_instructions": VAULT_AUTH_USERPASS_ENABLE_LLM_INSTRUCTIONS,
        "tags": ["write", "auth", "identity"],
    },
)


async def register_vault_userpass_lifecycle_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the two userpass switch ops into ``endpoint_descriptor``.

    Both are ``dangerous`` and need approval. Idempotent, like the other
    Vault registrars.
    """
    for spec in _LIFECYCLE_OP_SPECS:
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=spec["op_id"],
            handler=spec["handler"],
            summary=spec["summary"],
            description=spec["description"],
            parameter_schema=spec["parameter_schema"],
            response_schema=VAULT_AUTH_USERPASS_STATE_RESPONSE_SCHEMA,
            group_key="auth",
            when_to_use=_WHEN_TO_USE,
            tags=spec["tags"],
            safety_level="dangerous",
            requires_approval=True,
            llm_instructions=spec["llm_instructions"],
            embedding_service=embedding_service,
        )

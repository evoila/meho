# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault *identity token* role and key ops, spec table and delete preflights.

Vault signs identity tokens (JWTs) that another system, such as a cloud
provider, trusts. A **role** (``identity/oidc/role/<name>``) says which
**key** (``identity/oidc/key/<name>``) signs its tokens, the token audience
(``client_id``), the lifetime and a claims template. These are different
from the login roles of the JWT/OIDC auth method (``auth/<mount>/role``).

Six ops in the ``identity`` group:

* ``vault.identity.oidc.role.list`` / ``.read`` and
  ``vault.identity.oidc.key.list`` / ``.read`` -- safe, no approval. An
  empty list (Vault ``404``) is ``{"keys": []}``; an unknown name on a read
  is the underlying :class:`hvac.exceptions.InvalidPath`.
* ``vault.identity.oidc.role.delete`` / ``vault.identity.oidc.key.delete``
  -- dangerous, need approval. Vault's delete of a missing name succeeds.
  Vault refuses to delete a key that a role (or an OIDC provider client or
  a mount) still uses; the handler turns that into
  :class:`VaultOidcKeyInUseError`, whose ``remediation`` names the users.

All six use hvac's ``client.secrets.identity`` helpers and log in through
:func:`~meho_backplane.connectors.vault.target_auth.vault_client_for_target`,
so the target's ``extras["vault_role"]`` decides which Vault role runs the
call. Nothing here is a secret (a key read never returns the private key).

Each delete registers a park-time permission preflight (imported at module
load, like the token-role write): before MEHO asks for approval,
``sys/capabilities-self`` must grant ``delete`` on the role or key path to
the target's role. When it does not, the call is refused with
``write_capability_denied`` and no approval request is made.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import hvac.exceptions

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.vault.ops import vault_capability_preflight
from meho_backplane.connectors.vault.ops_auth import _extract_data, _extract_keys
from meho_backplane.connectors.vault.ops_identity_oidc_schemas import (
    VAULT_IDENTITY_OIDC_KEY_DELETE_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_OIDC_KEY_DELETE_PARAMETER_SCHEMA,
    VAULT_IDENTITY_OIDC_KEY_DELETE_RESPONSE_SCHEMA,
    VAULT_IDENTITY_OIDC_KEY_LIST_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_OIDC_KEY_LIST_PARAMETER_SCHEMA,
    VAULT_IDENTITY_OIDC_KEY_LIST_RESPONSE_SCHEMA,
    VAULT_IDENTITY_OIDC_KEY_READ_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_OIDC_KEY_READ_PARAMETER_SCHEMA,
    VAULT_IDENTITY_OIDC_KEY_READ_RESPONSE_SCHEMA,
    VAULT_IDENTITY_OIDC_ROLE_DELETE_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_OIDC_ROLE_DELETE_PARAMETER_SCHEMA,
    VAULT_IDENTITY_OIDC_ROLE_DELETE_RESPONSE_SCHEMA,
    VAULT_IDENTITY_OIDC_ROLE_LIST_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_OIDC_ROLE_LIST_PARAMETER_SCHEMA,
    VAULT_IDENTITY_OIDC_ROLE_LIST_RESPONSE_SCHEMA,
    VAULT_IDENTITY_OIDC_ROLE_READ_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_OIDC_ROLE_READ_PARAMETER_SCHEMA,
    VAULT_IDENTITY_OIDC_ROLE_READ_RESPONSE_SCHEMA,
)
from meho_backplane.connectors.vault.target_auth import vault_client_for_target
from meho_backplane.operations._preview import PreviewContext, register_permission_preflight

__all__ = [
    "IDENTITY_OIDC_DELETE_CAPABILITIES",
    "IDENTITY_OIDC_OP_SPECS",
    "OIDC_KEY_OP_DELETE",
    "OIDC_KEY_OP_LIST",
    "OIDC_KEY_OP_READ",
    "OIDC_ROLE_OP_DELETE",
    "OIDC_ROLE_OP_LIST",
    "OIDC_ROLE_OP_READ",
    "VaultOidcKeyInUseError",
    "oidc_key_auth_path",
    "oidc_role_auth_path",
    "vault_identity_oidc_key_delete",
    "vault_identity_oidc_key_delete_capability_preflight",
    "vault_identity_oidc_key_list",
    "vault_identity_oidc_key_read",
    "vault_identity_oidc_role_delete",
    "vault_identity_oidc_role_delete_capability_preflight",
    "vault_identity_oidc_role_list",
    "vault_identity_oidc_role_read",
]

OIDC_ROLE_OP_LIST = "vault.identity.oidc.role.list"
OIDC_ROLE_OP_READ = "vault.identity.oidc.role.read"
OIDC_ROLE_OP_DELETE = "vault.identity.oidc.role.delete"
OIDC_KEY_OP_LIST = "vault.identity.oidc.key.list"
OIDC_KEY_OP_READ = "vault.identity.oidc.key.read"
OIDC_KEY_OP_DELETE = "vault.identity.oidc.key.delete"

#: Vault ACL capability a delete needs on the role or key path.
IDENTITY_OIDC_DELETE_CAPABILITIES: frozenset[str] = frozenset({"delete"})

#: Vault's answer when a key is still in use:
#: ``unable to delete key "<name>" because it is currently referenced by
#: these <roles|clients|mounts>: <a>, <b>``.
_KEY_IN_USE = re.compile(r"currently referenced by these (\w+): (.+)$")


class VaultOidcKeyInUseError(ConnectorUpstreamError):
    """Vault refused to delete an identity token key that is still in use.

    ``users_kind`` is what still uses the key (``roles``, ``clients`` or
    ``mounts``) and ``users`` their names. ``remediation`` says what to do
    first; the dispatcher copies it into the ``connector_error`` extras.
    """

    def __init__(self, key: str, users_kind: str, users: list[str], upstream: str) -> None:
        names = ", ".join(users)
        super().__init__(
            f"key {key!r} is still used by these {users_kind}: {names}",
            http_status=400,
            upstream_message=upstream,
        )
        self.users_kind = users_kind
        self.users = users
        if users_kind == "roles":
            self.remediation = (
                f"Key {key!r} is still used by these roles: {names}. Delete those "
                "roles first (vault.identity.oidc.role.delete), or change them to "
                "use another key. Then delete the key again."
            )
        else:
            self.remediation = (
                f"Key {key!r} is still used by these {users_kind}: {names}. Change "
                "them to use another key first. Then delete the key again."
            )


def oidc_role_auth_path(name: str) -> str:
    """Return the path Vault authorizes a token role on: ``identity/oidc/role/<name>``."""
    return f"identity/oidc/role/{name}"


def oidc_key_auth_path(name: str) -> str:
    """Return the path Vault authorizes a token key on: ``identity/oidc/key/<name>``."""
    return f"identity/oidc/key/{name}"


def _key_in_use_error(
    name: str, exc: hvac.exceptions.InvalidRequest
) -> VaultOidcKeyInUseError | None:
    """Build :class:`VaultOidcKeyInUseError` when *exc* is Vault's key-in-use answer."""
    if isinstance(exc.errors, list):
        messages = [str(error) for error in exc.errors]
    else:
        messages = [str(exc.args[0]) if exc.args else ""]
    for message in messages:
        match = _KEY_IN_USE.search(message.strip())
        if match:
            users = [part.strip() for part in match.group(2).split(",") if part.strip()]
            return VaultOidcKeyInUseError(name, match.group(1), users, message)
    return None


# --- handlers --------------------------------------------------------------


async def vault_identity_oidc_role_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List the identity token role names (``LIST identity/oidc/role``).

    Op-id: ``vault.identity.oidc.role.list``. Delegates to hvac's
    ``client.secrets.identity.list_roles()``. Vault answers ``404`` when
    there are no roles; that is ``{"keys": []}``.
    """
    async with vault_client_for_target(operator, target) as client:
        try:
            payload = await asyncio.to_thread(client.secrets.identity.list_roles)
        except hvac.exceptions.InvalidPath:
            return {"keys": []}
    return {"keys": _extract_keys(payload)}


async def vault_identity_oidc_role_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read one identity token role (``GET identity/oidc/role/<name>``).

    Op-id: ``vault.identity.oidc.role.read``. Delegates to hvac's
    ``client.secrets.identity.read_role(name=...)`` and returns the
    unwrapped ``data`` dict (``key``, ``client_id``, ``ttl``, ``template``).
    """
    name: str = str(params["name"]).strip()
    async with vault_client_for_target(operator, target) as client:
        payload = await asyncio.to_thread(client.secrets.identity.read_role, name=name)
    return _extract_data(payload)


async def vault_identity_oidc_role_delete(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Delete one identity token role (``DELETE identity/oidc/role/<name>``).

    Op-id: ``vault.identity.oidc.role.delete``. Delegates to hvac's
    ``client.secrets.identity.delete_role(name=...)``. Deleting a missing
    role succeeds in Vault.
    """
    name: str = str(params["name"]).strip()
    async with vault_client_for_target(operator, target) as client:
        await asyncio.to_thread(client.secrets.identity.delete_role, name=name)
    return {"name": name, "deleted": True}


async def vault_identity_oidc_key_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List the identity token key names (``LIST identity/oidc/key``).

    Op-id: ``vault.identity.oidc.key.list``. Delegates to hvac's
    ``client.secrets.identity.list_named_keys()``. No keys (``404``) is
    ``{"keys": []}``.
    """
    async with vault_client_for_target(operator, target) as client:
        try:
            payload = await asyncio.to_thread(client.secrets.identity.list_named_keys)
        except hvac.exceptions.InvalidPath:
            return {"keys": []}
    return {"keys": _extract_keys(payload)}


async def vault_identity_oidc_key_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read one identity token key's settings (``GET identity/oidc/key/<name>``).

    Op-id: ``vault.identity.oidc.key.read``. Delegates to hvac's
    ``client.secrets.identity.read_named_key(name=...)`` and returns the
    unwrapped ``data`` dict. Vault never returns the private key.
    """
    name: str = str(params["name"]).strip()
    async with vault_client_for_target(operator, target) as client:
        payload = await asyncio.to_thread(client.secrets.identity.read_named_key, name=name)
    return _extract_data(payload)


async def vault_identity_oidc_key_delete(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Delete one identity token key (``DELETE identity/oidc/key/<name>``).

    Op-id: ``vault.identity.oidc.key.delete``. Delegates to hvac's
    ``client.secrets.identity.delete_named_key(name=...)``. When Vault
    refuses because the key is still in use, raises
    :class:`VaultOidcKeyInUseError` naming what uses it.
    """
    name: str = str(params["name"]).strip()
    async with vault_client_for_target(operator, target) as client:
        try:
            await asyncio.to_thread(client.secrets.identity.delete_named_key, name=name)
        except hvac.exceptions.InvalidRequest as exc:
            in_use = _key_in_use_error(name, exc)
            if in_use is None:
                raise
            raise in_use from exc
    return {"name": name, "deleted": True}


# --- delete preflights -----------------------------------------------------


async def vault_identity_oidc_role_delete_capability_preflight(
    operator: Operator, params: dict[str, Any], target: Any = None
) -> dict[str, Any] | None:
    """Check whether the target's role may delete ``identity/oidc/role/<name>``."""
    name = str(params.get("name", "")).strip()
    return await vault_capability_preflight(
        operator,
        OIDC_ROLE_OP_DELETE,
        oidc_role_auth_path(name),
        IDENTITY_OIDC_DELETE_CAPABILITIES,
        target,
    )


async def vault_identity_oidc_key_delete_capability_preflight(
    operator: Operator, params: dict[str, Any], target: Any = None
) -> dict[str, Any] | None:
    """Check whether the target's role may delete ``identity/oidc/key/<name>``."""
    name = str(params.get("name", "")).strip()
    return await vault_capability_preflight(
        operator,
        OIDC_KEY_OP_DELETE,
        oidc_key_auth_path(name),
        IDENTITY_OIDC_DELETE_CAPABILITIES,
        target,
    )


async def _role_delete_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    return await vault_identity_oidc_role_delete_capability_preflight(
        ctx.operator, ctx.params, target=ctx.target
    )


async def _key_delete_preflight(ctx: PreviewContext) -> dict[str, Any] | None:
    return await vault_identity_oidc_key_delete_capability_preflight(
        ctx.operator, ctx.params, target=ctx.target
    )


register_permission_preflight(OIDC_ROLE_OP_DELETE, _role_delete_preflight)
register_permission_preflight(OIDC_KEY_OP_DELETE, _key_delete_preflight)


# --- spec table ------------------------------------------------------------


def _read_spec(op_id: str, handler: Any, summary: str, description: str) -> dict[str, Any]:
    return {
        "op_id": op_id,
        "handler": handler,
        "group_key": "identity",
        "summary": summary,
        "description": description,
        "tags": ["read-only", "identity", "oidc"],
        "safety_level": "safe",
        "requires_approval": False,
    }


def _delete_spec(op_id: str, handler: Any, summary: str, description: str) -> dict[str, Any]:
    return {
        "op_id": op_id,
        "handler": handler,
        "group_key": "identity",
        "summary": summary,
        "description": description,
        "tags": ["write", "destructive", "identity", "oidc"],
        "safety_level": "dangerous",
        "requires_approval": True,
    }


#: Per-op registration spec for the identity token role and key ops.
#: Consumed by ``ops_identity_token.register_vault_identity_token_operations``
#: next to ``IDENTITY_OP_SPECS``.
IDENTITY_OIDC_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        **_read_spec(
            OIDC_ROLE_OP_LIST,
            vault_identity_oidc_role_list,
            "List the names of the Vault identity token roles.",
            "Lists the identity token role names (LIST identity/oidc/role). A role "
            "tells Vault how to sign identity tokens another system trusts. Names "
            "only. Read-only; safe, no approval. No roles gives {'keys': []}.",
        ),
        "parameter_schema": VAULT_IDENTITY_OIDC_ROLE_LIST_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_OIDC_ROLE_LIST_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_IDENTITY_OIDC_ROLE_LIST_LLM_INSTRUCTIONS,
    },
    {
        **_read_spec(
            OIDC_ROLE_OP_READ,
            vault_identity_oidc_role_read,
            "Read one Vault identity token role: key, audience, lifetime.",
            "Reads one identity token role (GET identity/oidc/role/<name>): the "
            "signing key, the token audience (client_id), the ttl and the claims "
            "template. None is a secret. Read-only; safe, no approval.",
        ),
        "parameter_schema": VAULT_IDENTITY_OIDC_ROLE_READ_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_OIDC_ROLE_READ_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_IDENTITY_OIDC_ROLE_READ_LLM_INSTRUCTIONS,
    },
    {
        **_delete_spec(
            OIDC_ROLE_OP_DELETE,
            vault_identity_oidc_role_delete,
            "Delete one Vault identity token role.",
            "Deletes one identity token role (DELETE identity/oidc/role/<name>). "
            "Vault stops issuing tokens for it. Dangerous; needs approval. "
            "Deleting a missing role also succeeds.",
        ),
        "parameter_schema": VAULT_IDENTITY_OIDC_ROLE_DELETE_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_OIDC_ROLE_DELETE_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_IDENTITY_OIDC_ROLE_DELETE_LLM_INSTRUCTIONS,
    },
    {
        **_read_spec(
            OIDC_KEY_OP_LIST,
            vault_identity_oidc_key_list,
            "List the names of the keys that sign Vault identity tokens.",
            "Lists the identity token key names (LIST identity/oidc/key). Names "
            "only. Read-only; safe, no approval. No keys gives {'keys': []}.",
        ),
        "parameter_schema": VAULT_IDENTITY_OIDC_KEY_LIST_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_OIDC_KEY_LIST_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_IDENTITY_OIDC_KEY_LIST_LLM_INSTRUCTIONS,
    },
    {
        **_read_spec(
            OIDC_KEY_OP_READ,
            vault_identity_oidc_key_read,
            "Read the settings of one key that signs Vault identity tokens.",
            "Reads one identity token key (GET identity/oidc/key/<name>): "
            "algorithm, allowed client ids, rotation period, verification ttl. "
            "Vault never returns the private key. Read-only; safe, no approval.",
        ),
        "parameter_schema": VAULT_IDENTITY_OIDC_KEY_READ_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_OIDC_KEY_READ_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_IDENTITY_OIDC_KEY_READ_LLM_INSTRUCTIONS,
    },
    {
        **_delete_spec(
            OIDC_KEY_OP_DELETE,
            vault_identity_oidc_key_delete,
            "Delete one key that signs Vault identity tokens.",
            "Deletes one identity token key (DELETE identity/oidc/key/<name>). "
            "Dangerous; needs approval. Vault refuses while a role still uses "
            "the key; the error names those roles.",
        ),
        "parameter_schema": VAULT_IDENTITY_OIDC_KEY_DELETE_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_OIDC_KEY_DELETE_RESPONSE_SCHEMA,
        "llm_instructions": VAULT_IDENTITY_OIDC_KEY_DELETE_LLM_INSTRUCTIONS,
    },
)

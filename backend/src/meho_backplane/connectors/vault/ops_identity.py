# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Vault *identity* op handlers + spec table (G3.15-T4, #1412).

The identity group surfaces the core Vault ``identity/`` secrets engine
(always mounted): entity / entity-alias / group lifecycle. Group
membership is privilege plumbing; entity/group policy bindings are
privilege assignments. The four writes
(``vault.identity.entity.write`` / ``entity_alias.write`` /
``group.write`` / ``group.delete``) register ``safety_level="dangerous"``,
``requires_approval=True``. The read primitives
(``vault.identity.entity.read`` / ``group.read`` / ``list``) register
``safety_level="safe"``, ``requires_approval=False`` -- registered safe
even though the lookups are HTTP POST/LIST so a create-if-absent flow
does not stall on approval (the issue's explicit ask).

Handler shape mirrors :mod:`meho_backplane.connectors.vault.ops_auth`
verbatim: each is a module-level ``async def`` with the ``(operator,
target, params) -> dict`` typed-op contract, raises on failure (the
dispatcher's ``connector_error`` branch records the class name in
``extras["exception_class"]``), forwards the operator JWT via
``vault_client_for_operator``, and offloads the blocking hvac call with
``asyncio.to_thread``. The ``identity/`` engine is core (always
mounted), so there is no backend-not-mounted reclassification -- a
``404`` here means a missing entity/group and surfaces as the underlying
:class:`hvac.exceptions.InvalidPath`. ``list`` normalises an empty-store
``404`` to ``{"keys": []}``.

The spec table + ``when_to_use`` blurb are consumed by the package
composer :mod:`meho_backplane.connectors.vault.ops_identity_token`, which
upserts both this group and the token group under one lifespan registrar.
"""

from __future__ import annotations

import asyncio
from typing import Any

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.vault.ops_auth import _extract_data, _extract_keys
from meho_backplane.connectors.vault.ops_identity_schemas import (
    VAULT_IDENTITY_ENTITY_ALIAS_WRITE_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_ENTITY_ALIAS_WRITE_PARAMETER_SCHEMA,
    VAULT_IDENTITY_ENTITY_ALIAS_WRITE_RESPONSE_SCHEMA,
    VAULT_IDENTITY_ENTITY_READ_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_ENTITY_READ_PARAMETER_SCHEMA,
    VAULT_IDENTITY_ENTITY_READ_RESPONSE_SCHEMA,
    VAULT_IDENTITY_ENTITY_WRITE_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_ENTITY_WRITE_PARAMETER_SCHEMA,
    VAULT_IDENTITY_ENTITY_WRITE_RESPONSE_SCHEMA,
    VAULT_IDENTITY_GROUP_DELETE_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_GROUP_DELETE_PARAMETER_SCHEMA,
    VAULT_IDENTITY_GROUP_DELETE_RESPONSE_SCHEMA,
    VAULT_IDENTITY_GROUP_READ_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_GROUP_READ_PARAMETER_SCHEMA,
    VAULT_IDENTITY_GROUP_READ_RESPONSE_SCHEMA,
    VAULT_IDENTITY_GROUP_WRITE_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_GROUP_WRITE_PARAMETER_SCHEMA,
    VAULT_IDENTITY_GROUP_WRITE_RESPONSE_SCHEMA,
    VAULT_IDENTITY_LIST_LLM_INSTRUCTIONS,
    VAULT_IDENTITY_LIST_PARAMETER_SCHEMA,
    VAULT_IDENTITY_LIST_RESPONSE_SCHEMA,
)

__all__ = [
    "IDENTITY_OP_SPECS",
    "IDENTITY_WHEN_TO_USE",
    "vault_identity_entity_alias_write",
    "vault_identity_entity_read",
    "vault_identity_entity_write",
    "vault_identity_group_delete",
    "vault_identity_group_read",
    "vault_identity_group_write",
    "vault_identity_list",
]


def _maybe_id(payload: Any, key: str) -> str | None:
    """Pull a freshly-minted id out of Vault's ``{"data": {...}}`` envelope.

    A create returns ``{"data": {"id": ...}}``; a pure update returns
    ``204`` with no body, which hvac surfaces as a falsy payload. Return
    ``None`` in that case so the handler's value-free confirmation still
    holds ('written: true') for an update that minted no id.
    """
    if not payload:
        return None
    data = payload.get("data") or {}
    minted = data.get(key)
    return str(minted) if minted is not None else None


def _hvac_invalid_path() -> type[BaseException]:
    """Return :class:`hvac.exceptions.InvalidPath` (lazy import).

    Kept as a tiny indirection so the ``except`` clauses read uniformly
    and the module's top-level imports stay confined to the handler
    contract surface. hvac is a hard dependency (imported transitively
    via ``ops_auth``), so this never fails.
    """
    import hvac.exceptions

    invalid_path: type[BaseException] = hvac.exceptions.InvalidPath
    return invalid_path


# --- write handlers --------------------------------------------------------


async def vault_identity_entity_write(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Create or update an identity entity (name, policies, metadata).

    Op-id: ``vault.identity.entity.write``. Delegates to hvac's
    ``client.secrets.identity.create_or_update_entity(name=...,
    entity_id=..., policies=..., metadata=..., disabled=...)``. Only the
    keys the caller supplied are forwarded (each is optional on the Vault
    side). A create returns the minted ``entity_id``; an update returns
    ``204`` (no body), so ``entity_id`` is ``None`` in that case.
    """
    name: str = str(params["name"]).strip()
    kwargs: dict[str, Any] = {"name": name}
    for field in ("entity_id", "policies", "metadata", "disabled"):
        if field in params and params[field] is not None:
            kwargs[field] = params[field]

    async with _auth_vault.vault_client_for_operator(operator) as client:
        payload = await asyncio.to_thread(client.secrets.identity.create_or_update_entity, **kwargs)

    return {"name": name, "entity_id": _maybe_id(payload, "id"), "written": True}


async def vault_identity_entity_alias_write(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Create or update an entity alias (link an auth login to an entity).

    Op-id: ``vault.identity.entity_alias.write``. Delegates to hvac's
    ``client.secrets.identity.create_or_update_entity_alias(name=...,
    canonical_id=..., mount_accessor=..., alias_id=...)``. A create
    returns the minted ``alias_id``; an update returns ``204``.
    """
    name: str = str(params["name"]).strip()
    canonical_id: str = str(params["canonical_id"]).strip()
    mount_accessor: str = str(params["mount_accessor"]).strip()
    kwargs: dict[str, Any] = {
        "name": name,
        "canonical_id": canonical_id,
        "mount_accessor": mount_accessor,
    }
    if params.get("alias_id") is not None:
        kwargs["alias_id"] = str(params["alias_id"]).strip()

    async with _auth_vault.vault_client_for_operator(operator) as client:
        payload = await asyncio.to_thread(
            client.secrets.identity.create_or_update_entity_alias, **kwargs
        )

    return {
        "name": name,
        "canonical_id": canonical_id,
        "alias_id": _maybe_id(payload, "id"),
        "written": True,
    }


#: Identity-group config keys forwarded to hvac verbatim when present.
#: Each is optional on the Vault side (omitting leaves the field
#: unchanged on an update / at its default on a create).
_GROUP_WRITE_FIELDS: tuple[str, ...] = (
    "group_id",
    "group_type",
    "policies",
    "metadata",
    "member_entity_ids",
    "member_group_ids",
)


async def vault_identity_group_write(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Create or update an identity group (policies + membership).

    Op-id: ``vault.identity.group.write``. Delegates to hvac's
    ``client.secrets.identity.create_or_update_group(name=..., **config)``.
    Membership (``member_entity_ids`` / ``member_group_ids``) is privilege
    plumbing -- an entity in a policy-bearing group inherits that policy.
    A create returns the minted ``group_id``; an update returns ``204``.
    """
    name: str = str(params["name"]).strip()
    kwargs: dict[str, Any] = {"name": name}
    for field in _GROUP_WRITE_FIELDS:
        if field in params and params[field] is not None:
            kwargs[field] = params[field]

    async with _auth_vault.vault_client_for_operator(operator) as client:
        payload = await asyncio.to_thread(client.secrets.identity.create_or_update_group, **kwargs)

    return {"name": name, "group_id": _maybe_id(payload, "id"), "written": True}


async def vault_identity_group_delete(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Delete an identity group by name (NO bulk-delete by design).

    Op-id: ``vault.identity.group.delete``. Delegates to hvac's
    ``client.secrets.identity.delete_group_by_name(name=...)``. Vault's
    delete is idempotent (deleting a non-existent group is a no-op
    success). Removes the group's policy bindings from every member --
    an irreversible privilege change.
    """
    name: str = str(params["name"]).strip()

    async with _auth_vault.vault_client_for_operator(operator) as client:
        await asyncio.to_thread(client.secrets.identity.delete_group_by_name, name=name)

    return {"name": name, "deleted": True}


# --- read handlers ---------------------------------------------------------


async def vault_identity_entity_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read one identity entity's config by id.

    Op-id: ``vault.identity.entity.read``. Delegates to hvac's
    ``client.secrets.identity.read_entity(entity_id=...)`` and returns
    the unwrapped ``data`` config dict (name, policies, metadata,
    aliases, disabled). A missing id surfaces as the underlying
    :class:`hvac.exceptions.InvalidPath` (read-phase failure).
    """
    entity_id: str = str(params["entity_id"]).strip()

    async with _auth_vault.vault_client_for_operator(operator) as client:
        payload = await asyncio.to_thread(client.secrets.identity.read_entity, entity_id=entity_id)

    return _extract_data(payload)


async def vault_identity_group_read(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Read one identity group's config by name.

    Op-id: ``vault.identity.group.read``. Delegates to hvac's
    ``client.secrets.identity.read_group_by_name(name=...)`` and returns
    the unwrapped ``data`` config dict (type, policies, metadata, member
    entity/group ids). A missing group surfaces as the underlying
    :class:`hvac.exceptions.InvalidPath`.
    """
    name: str = str(params["name"]).strip()

    async with _auth_vault.vault_client_for_operator(operator) as client:
        payload = await asyncio.to_thread(client.secrets.identity.read_group_by_name, name=name)

    return _extract_data(payload)


async def vault_identity_list(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """List identity entity ids or group ids.

    Op-id: ``vault.identity.list``. ``kind`` selects the collection
    (``"groups"`` by default, or ``"entities"``); delegates to hvac's
    ``list_groups`` / ``list_entities`` and returns a normalised
    ``{"kind", "keys"}`` dict. An empty collection (LIST returns
    ``404``/no body) normalises to ``{"keys": []}`` rather than raising,
    so the op's 'always a list' contract holds.
    """
    kind: str = str(params.get("kind", "groups")).strip()

    async with _auth_vault.vault_client_for_operator(operator) as client:
        list_fn = (
            client.secrets.identity.list_entities
            if kind == "entities"
            else client.secrets.identity.list_groups
        )
        try:
            payload = await asyncio.to_thread(list_fn)
        except _hvac_invalid_path():
            # An identity store with zero entities/groups LISTs as a 404;
            # that is "empty", not an error.
            return {"kind": kind, "keys": []}

    return {"kind": kind, "keys": _extract_keys(payload)}


# --- spec table ------------------------------------------------------------

IDENTITY_WHEN_TO_USE: str = (
    "Use to inspect and manage Vault IDENTITY objects -- entities, entity "
    "aliases, and groups -- on the core identity/ engine. Reads "
    "(vault.identity.entity.read / group.read / list) are safe and "
    "approval-free; writes (vault.identity.entity.write / "
    "entity_alias.write / group.write / group.delete) require approval "
    "because policy bindings and group membership are privilege "
    "assignments. An entity is the canonical identity a human/service "
    "maps onto across auth backends; an alias links one auth-method login "
    "to it; a group bundles entities under shared policies. Pair with the "
    "'auth' group (per-backend userpass/approle roles) and the 'sys' "
    "group (auth-method mount accessors needed for entity_alias.write)."
)

#: Per-op registration spec for the identity surface. ``group_key`` is
#: ``identity`` for every row; ``safety_level`` / ``requires_approval``
#: are carried per-op (writes dangerous + approval; reads safe + no
#: approval). Consumed by the package composer
#: ``ops_identity_token.register_vault_identity_token_operations``.
IDENTITY_OP_SPECS: tuple[dict[str, Any], ...] = (
    {
        "op_id": "vault.identity.entity.write",
        "handler": vault_identity_entity_write,
        "group_key": "identity",
        "summary": "Create or update an identity entity (name, policies, metadata).",
        "description": (
            "Creates a new identity entity or updates an existing one's "
            "name, policies, metadata, or disabled state (POST "
            "/v1/identity/entity). Binding policies is a privilege "
            "assignment -- safety_level=dangerous, requires_approval=True. "
            "A create returns the minted entity_id; an update returns 204 "
            "(entity_id null). Value-free response (name, entity_id, "
            "written)."
        ),
        "parameter_schema": VAULT_IDENTITY_ENTITY_WRITE_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_ENTITY_WRITE_RESPONSE_SCHEMA,
        "tags": ["write", "identity"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VAULT_IDENTITY_ENTITY_WRITE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.identity.entity_alias.write",
        "handler": vault_identity_entity_alias_write,
        "group_key": "identity",
        "summary": "Create or update an entity alias (link an auth login to an entity).",
        "description": (
            "Creates or updates an entity alias linking an auth-method "
            "login (a username, an OIDC subject) to an identity entity "
            "(POST /v1/identity/entity-alias). The alias lets that login "
            "inherit the entity's policies -- safety_level=dangerous, "
            "requires_approval=True. Needs the source mount's accessor "
            "(from the sys group's auth-method listing). A create returns "
            "the minted alias_id; an update returns 204."
        ),
        "parameter_schema": VAULT_IDENTITY_ENTITY_ALIAS_WRITE_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_ENTITY_ALIAS_WRITE_RESPONSE_SCHEMA,
        "tags": ["write", "identity"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VAULT_IDENTITY_ENTITY_ALIAS_WRITE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.identity.group.write",
        "handler": vault_identity_group_write,
        "group_key": "identity",
        "summary": "Create or update an identity group (policies + membership).",
        "description": (
            "Creates or updates an identity group, including its policies "
            "and member entities/groups (POST /v1/identity/group). Group "
            "membership is privilege plumbing -- an entity in a "
            "policy-bearing group inherits that policy. "
            "safety_level=dangerous, requires_approval=True. A create "
            "returns the minted group_id; an update returns 204."
        ),
        "parameter_schema": VAULT_IDENTITY_GROUP_WRITE_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_GROUP_WRITE_RESPONSE_SCHEMA,
        "tags": ["write", "identity"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VAULT_IDENTITY_GROUP_WRITE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.identity.group.delete",
        "handler": vault_identity_group_delete,
        "group_key": "identity",
        "summary": "Delete an identity group by name.",
        "description": (
            "Deletes an identity group by name, removing its policy "
            "bindings from every member (DELETE "
            "/v1/identity/group/name/<name>). Irreversible privilege "
            "change -- safety_level=dangerous, requires_approval=True. "
            "Idempotent (deleting a non-existent group is a no-op "
            "success). Deletes ONE named group; there is no bulk-delete "
            "verb by design."
        ),
        "parameter_schema": VAULT_IDENTITY_GROUP_DELETE_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_GROUP_DELETE_RESPONSE_SCHEMA,
        "tags": ["write", "destructive", "identity"],
        "safety_level": "dangerous",
        "requires_approval": True,
        "llm_instructions": VAULT_IDENTITY_GROUP_DELETE_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.identity.entity.read",
        "handler": vault_identity_entity_read,
        "group_key": "identity",
        "summary": "Read one identity entity's config by id.",
        "description": (
            "Reads one identity entity's config by id -- name, policies, "
            "metadata, aliases, disabled state (GET "
            "/v1/identity/entity/id/<id>). Read-only; registered "
            "safety_level=safe, requires_approval=False even though the "
            "lookup is an HTTP read so create-if-absent flows do not "
            "stall on approval. A missing id surfaces as InvalidPath."
        ),
        "parameter_schema": VAULT_IDENTITY_ENTITY_READ_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_ENTITY_READ_RESPONSE_SCHEMA,
        "tags": ["read-only", "identity"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_IDENTITY_ENTITY_READ_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.identity.group.read",
        "handler": vault_identity_group_read,
        "group_key": "identity",
        "summary": "Read one identity group's config by name.",
        "description": (
            "Reads one identity group's config by name -- type, policies, "
            "metadata, member entity/group ids (GET "
            "/v1/identity/group/name/<name>). Read-only; registered "
            "safety_level=safe, requires_approval=False so create-if-absent "
            "flows do not stall on approval. A missing group surfaces as "
            "InvalidPath."
        ),
        "parameter_schema": VAULT_IDENTITY_GROUP_READ_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_GROUP_READ_RESPONSE_SCHEMA,
        "tags": ["read-only", "identity"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_IDENTITY_GROUP_READ_LLM_INSTRUCTIONS,
    },
    {
        "op_id": "vault.identity.list",
        "handler": vault_identity_list,
        "group_key": "identity",
        "summary": "List identity entity ids or group ids.",
        "description": (
            "Enumerates identity entity ids or group ids by id (LIST "
            "/v1/identity/{entity,group}/id). 'kind' selects the "
            "collection ('groups' default, or 'entities'). Read-only; "
            "registered safety_level=safe, requires_approval=False. An "
            "empty collection normalises to {'keys': []}."
        ),
        "parameter_schema": VAULT_IDENTITY_LIST_PARAMETER_SCHEMA,
        "response_schema": VAULT_IDENTITY_LIST_RESPONSE_SCHEMA,
        "tags": ["read-only", "identity"],
        "safety_level": "safe",
        "requires_approval": False,
        "llm_instructions": VAULT_IDENTITY_LIST_LLM_INSTRUCTIONS,
    },
)

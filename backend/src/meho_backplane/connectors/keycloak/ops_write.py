# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gated write ops for :class:`KeycloakConnector` (G3.13-T4 #1406).

Nine mutating ops that retire the consumer's five Keycloak bootstrap
scripts (``keycloak-bootstrap-meho-{admin,cli,mcp,web}.sh`` and
``keycloak-provision-meho-user.sh``). Every op registers
``requires_approval=True`` — no write dispatches without going through
the human approve-queue (G11.7-T1 #1401):

==================================  ===========  ===========================================
op_id                               safety       Admin REST API
==================================  ===========  ===========================================
``keycloak.realm.create``           dangerous    ``POST /admin/realms``
``keycloak.realm.update``           caution      ``PUT .../realms/{realm}``
``keycloak.client.create``          caution      ``POST .../realms/{realm}/clients``
``keycloak.client.update``          caution      ``PUT .../clients/{id}``
``keycloak.client_scope.create``    caution      ``POST .../client-scopes``
``keycloak.protocol_mapper.create`` caution      ``POST .../clients/{id}/protocol-mappers/models``
``keycloak.user.create``            caution      ``POST .../realms/{realm}/users``
``keycloak.user.reset_password``    caution      ``PUT .../users/{id}/reset-password``
``keycloak.role_mapping.assign``    dangerous    ``POST .../users/{id}/role-mappings/realm``
==================================  ===========  ===========================================

``keycloak.idp.create`` (identity-provider federation) is **deliberately
deferred**: the issue (#1406) notes it is "not exercised by current
scripts", so it stays out of this PR to keep scope on the script-retiring
ops. A future task can add it under the same registrar walk.

Name → UUID resolution (load-bearing)
=====================================

Keycloak addresses every object by an internal **UUID**, never its human
name. So every write that targets an existing object first resolves the
name to a UUID via a find-by-name dance on the connector
(:meth:`~meho_backplane.connectors.keycloak.connector.KeycloakConnector._find_client_uuid`
/ ``_find_user_uuid`` / ``_find_realm_role``). ``client.update`` /
``user.reset_password`` / ``role_mapping.assign`` accept either the human
name (``client_id`` / ``username`` / target ``username``) — resolved
here — or an explicit ``id`` UUID when the caller already has it. A create
returns the new object's UUID parsed from the ``Location`` header.

Idempotency (load-bearing)
==========================

Re-running a create must be a no-op-equivalent success, not an error.
``_write_admin`` swallows an HTTP **409 Conflict** (already-exists) and
surfaces ``conflict=True``; the create handlers map that to
``{created: false, conflict: true}`` with ``status="ok"`` so a re-run of a
bootstrap script's create step does not fail the dispatch. The handler
then resolves the existing object's UUID so the caller still gets the id
it would have gotten on a fresh create.

Password handling (critical security)
=====================================

``user.create`` and ``user.reset_password`` **never** carry the password
inline in op params. The password is read from Vault under the operator's
identity (``password_secret_ref`` — a KV-v2 path; optional
``password_secret_mount`` / ``password_secret_key``) via the same
``vault_client_for_operator`` primitive the Vault connector ops use. Two
layers of defence keep the password out of audit + broadcast:

* The **op params** carry only a Vault *path*, never the secret — so the
  audit row's ``params_hash`` (params are never stored verbatim) hashes a
  path, and the broadcast ``params`` view carries a path.
* Both ops are additionally pinned in
  :data:`meho_backplane.broadcast.events._CREDENTIAL_WRITE_OPS` so the
  broadcast classifier collapses them to aggregate-only — defence in
  depth for a future param-shape change.

The handlers return **value-free** confirmations (username / realm /
created flag), never the password.

References
----------

* Task: https://github.com/evoila/meho/issues/1406
* Parent initiative: https://github.com/evoila/meho/issues/1388
* Human approve-queue + write-op redaction: G11.7-T1 #1401.
* Keycloak 26.3 Admin REST API:
  https://www.keycloak.org/docs-api/26.3.3/rest-api/index.html
* RealmRepresentation / ClientRepresentation / ClientScopeRepresentation /
  ProtocolMapperRepresentation / UserRepresentation /
  CredentialRepresentation / RoleRepresentation:
  https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/representations/idm/package-summary.html
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.connectors.keycloak.session import resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "WHEN_TO_USE_WRITE_BY_GROUP",
    "WRITE_OPS",
    "KeycloakPasswordSecretError",
    "KeycloakRoleNotFoundError",
    "KeycloakUserNotFoundError",
    "keycloak_client_create",
    "keycloak_client_scope_create",
    "keycloak_client_update",
    "keycloak_protocol_mapper_create",
    "keycloak_realm_create",
    "keycloak_realm_update",
    "keycloak_role_mapping_assign",
    "keycloak_user_create",
    "keycloak_user_reset_password",
]

#: KV-v2 mount the password reader defaults to when the op omits
#: ``password_secret_mount``. Mirrors the Vault connector's default mount.
_DEFAULT_PASSWORD_MOUNT = "secret"

#: Default field name read out of the Vault secret payload when the op
#: omits ``password_secret_key``.
_DEFAULT_PASSWORD_KEY = "password"


class KeycloakPasswordSecretError(Exception):
    """The Vault secret backing a user password is missing or malformed.

    Raised when ``password_secret_ref`` resolves to a secret that lacks the
    requested key (default ``password``) or carries a non-string / empty
    value. The message names the path + key (never the value) so the
    operator can fix the secret.
    """


class KeycloakUserNotFoundError(Exception):
    """A user write targeted a username/UUID that does not exist."""


class KeycloakRoleNotFoundError(Exception):
    """A role-mapping assign referenced a realm role that does not exist."""


# ---------------------------------------------------------------------------
# Vault-sourced password reader (operator-context)
# ---------------------------------------------------------------------------


async def _read_password_from_vault(operator: Operator, params: dict[str, Any]) -> str:
    """Read a user password from Vault under the operator's identity.

    The password is **never** an inline param — only its Vault location is.
    ``password_secret_ref`` is the KV-v2 path; ``password_secret_mount``
    (default ``secret``) the mount; ``password_secret_key`` (default
    ``password``) the field within the secret payload. Forwards the
    operator's validated JWT to Vault's JWT/OIDC auth method via
    :func:`~meho_backplane.auth.vault.vault_client_for_operator` (the same
    operator-context read the connector's admin-credential loader uses) and
    offloads the blocking hvac call with ``asyncio.to_thread``.

    Raises :exc:`KeycloakPasswordSecretError` when the requested key is
    absent or its value is not a non-empty string.
    """
    secret_ref = str(params["password_secret_ref"]).strip()
    mount = str(params.get("password_secret_mount") or _DEFAULT_PASSWORD_MOUNT).strip()
    key = str(params.get("password_secret_key") or _DEFAULT_PASSWORD_KEY).strip()

    async with _auth_vault.vault_client_for_operator(operator) as client:
        payload = await asyncio.to_thread(
            client.secrets.kv.v2.read_secret_version,
            path=secret_ref,
            mount_point=mount,
            raise_on_deleted_version=False,
        )
    secret_data = payload["data"]["data"]
    value = secret_data.get(key) if isinstance(secret_data, dict) else None
    if not isinstance(value, str) or not value:
        raise KeycloakPasswordSecretError(
            f"keycloak_password_secret: Vault secret at mount={mount!r} "
            f"path={secret_ref!r} carries no usable string value under key "
            f"{key!r}. Store the password under that key (or set "
            f"password_secret_key) so the user write can source it without "
            f"the password ever appearing in op params."
        )
    return value


def _temporary_flag(params: dict[str, Any]) -> bool:
    """Resolve the ``temporary`` credential flag (default ``False``).

    Keycloak's CredentialRepresentation ``temporary=true`` forces a
    password change on first login. Defaults to ``False`` (a permanent
    password) — the bootstrap scripts provision service/operator accounts
    with a permanent password.
    """
    return bool(params.get("temporary", False))


# ---------------------------------------------------------------------------
# Realm writes
# ---------------------------------------------------------------------------


async def keycloak_realm_create(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Create a realm (``POST /admin/realms``).

    Op-id: ``keycloak.realm.create``. ``representation`` is the
    RealmRepresentation body; its ``realm`` field is the realm name. A
    409 (realm already exists) is treated as an idempotent success.
    Returns a value-free confirmation: the realm name + created/conflict
    flags.
    """
    representation = dict(params["representation"])
    realm_name = str(representation.get("realm") or params.get("realm") or "").strip()
    representation.setdefault("realm", realm_name)
    result = await self._write_admin(
        target, "POST", "/admin/realms", operator=operator, json=representation
    )
    return {
        "realm": realm_name,
        "created": not result.conflict,
        "conflict": result.conflict,
    }


async def keycloak_realm_update(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Update a realm's top-level config (``PUT /admin/realms/{realm}``).

    Op-id: ``keycloak.realm.update``. Defaults to the target's managed
    realm; ``realm`` overrides it. ``representation`` is the partial
    RealmRepresentation to merge. Returns a value-free confirmation.
    """
    realms = resolve_realm_config(target)
    realm_name = str(params.get("realm") or realms.managed_realm).strip()
    representation = dict(params["representation"])
    representation.setdefault("realm", realm_name)
    await self._write_admin(
        target,
        "PUT",
        f"/admin/realms/{realm_name}",
        operator=operator,
        json=representation,
        idempotent_conflict=False,
    )
    return {"realm": realm_name, "updated": True}


# ---------------------------------------------------------------------------
# Client writes
# ---------------------------------------------------------------------------


async def keycloak_client_create(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Create a client (``POST /admin/realms/{realm}/clients``).

    Op-id: ``keycloak.client.create``. ``representation`` is the
    ClientRepresentation (flows / redirect URIs / protocol mappers). A 409
    (clientId already exists) is treated as an idempotent success, and the
    existing client's UUID is resolved so the caller always gets an ``id``.
    Returns the internal UUID + created/conflict flags — never the client
    secret.
    """
    realms = resolve_realm_config(target)
    representation = dict(params["representation"])
    client_id = str(representation.get("clientId") or params.get("client_id") or "").strip()
    representation.setdefault("clientId", client_id)
    result = await self._write_admin(
        target,
        "POST",
        f"/admin/realms/{realms.managed_realm}/clients",
        operator=operator,
        json=representation,
    )
    uuid = result.created_uuid()
    if uuid is None and client_id:
        # 409 (no Location) or a create that returned no Location header:
        # resolve the existing client's UUID so the caller still gets an id.
        uuid = await self._find_client_uuid(
            target, realms.managed_realm, client_id, operator=operator
        )
    return {
        "client_id": client_id,
        "id": uuid,
        "created": not result.conflict,
        "conflict": result.conflict,
    }


async def keycloak_client_update(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Update a client (``PUT /admin/realms/{realm}/clients/{id}``).

    Op-id: ``keycloak.client.update``. Keys on the client's internal UUID;
    pass it directly via ``id``, or pass ``client_id`` (the human clientId)
    and the handler resolves the UUID via ``?clientId=`` lookup.
    ``representation`` is the partial ClientRepresentation. Raises
    :exc:`KeycloakUserNotFoundError`'s sibling — a clear error — when the
    clientId resolves to no client.
    """
    realms = resolve_realm_config(target)
    uuid = _opt_str(params.get("id"))
    client_id = _opt_str(params.get("client_id"))
    if uuid is None:
        if client_id is None:
            raise ValueError("keycloak.client.update requires either 'id' (UUID) or 'client_id'")
        uuid = await self._find_client_uuid(
            target, realms.managed_realm, client_id, operator=operator
        )
        if uuid is None:
            raise KeycloakUserNotFoundError(
                f"keycloak.client.update: no client with clientId={client_id!r} in realm "
                f"{realms.managed_realm!r}"
            )
    representation = dict(params["representation"])
    representation.setdefault("id", uuid)
    await self._write_admin(
        target,
        "PUT",
        f"/admin/realms/{realms.managed_realm}/clients/{uuid}",
        operator=operator,
        json=representation,
        idempotent_conflict=False,
    )
    return {"id": uuid, "client_id": client_id, "updated": True}


async def keycloak_client_scope_create(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Create a client scope (``POST /admin/realms/{realm}/client-scopes``).

    Op-id: ``keycloak.client_scope.create``. ``representation`` is the
    ClientScopeRepresentation (its ``protocolMappers`` ride in the body).
    A 409 (scope name already exists) is an idempotent success. Returns the
    scope name + created/conflict flags.
    """
    realms = resolve_realm_config(target)
    representation = dict(params["representation"])
    name = str(representation.get("name") or params.get("name") or "").strip()
    representation.setdefault("name", name)
    result = await self._write_admin(
        target,
        "POST",
        f"/admin/realms/{realms.managed_realm}/client-scopes",
        operator=operator,
        json=representation,
    )
    return {
        "name": name,
        "id": result.created_uuid(),
        "created": not result.conflict,
        "conflict": result.conflict,
    }


async def keycloak_protocol_mapper_create(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Add a protocol mapper to a client (``POST .../clients/{id}/protocol-mappers/models``).

    Op-id: ``keycloak.protocol_mapper.create``. This is the op that wires
    the ``tenant_id`` / ``tenant_role`` claims the backplane row-scopes on.
    Keys on the client's internal UUID; pass it via ``id`` or pass
    ``client_id`` (the human clientId) for name→UUID resolution.
    ``representation`` is the ProtocolMapperRepresentation. A 409 (a mapper
    with that name already exists on the client) is an idempotent success.
    """
    realms = resolve_realm_config(target)
    uuid = _opt_str(params.get("id"))
    client_id = _opt_str(params.get("client_id"))
    if uuid is None:
        if client_id is None:
            raise ValueError(
                "keycloak.protocol_mapper.create requires either 'id' (client UUID) or 'client_id'"
            )
        uuid = await self._find_client_uuid(
            target, realms.managed_realm, client_id, operator=operator
        )
        if uuid is None:
            raise KeycloakUserNotFoundError(
                f"keycloak.protocol_mapper.create: no client with clientId={client_id!r} "
                f"in realm {realms.managed_realm!r}"
            )
    representation = dict(params["representation"])
    mapper_name = _opt_str(representation.get("name"))
    result = await self._write_admin(
        target,
        "POST",
        f"/admin/realms/{realms.managed_realm}/clients/{uuid}/protocol-mappers/models",
        operator=operator,
        json=representation,
    )
    return {
        "client_uuid": uuid,
        "client_id": client_id,
        "mapper_name": mapper_name,
        "created": not result.conflict,
        "conflict": result.conflict,
    }


# ---------------------------------------------------------------------------
# User writes (password sourced from Vault, never inline)
# ---------------------------------------------------------------------------


async def keycloak_user_create(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Create a user with a Vault-sourced password (``POST .../users``).

    Op-id: ``keycloak.user.create``. ``representation`` is the
    UserRepresentation (username / email / enabled / attributes). The
    password is read from Vault (``password_secret_ref``) and set as a
    permanent credential in the create body — it is **never** an inline
    param. A 409 (username already exists) is an idempotent success; the
    existing user's UUID is then resolved. Returns the UUID + value-free
    flags, never the password.
    """
    realms = resolve_realm_config(target)
    representation = dict(params["representation"])
    username = str(representation.get("username") or params.get("username") or "").strip()
    representation.setdefault("username", username)
    representation.setdefault("enabled", True)

    if params.get("password_secret_ref"):
        password = await _read_password_from_vault(operator, params)
        representation["credentials"] = [
            {"type": "password", "value": password, "temporary": _temporary_flag(params)}
        ]

    result = await self._write_admin(
        target,
        "POST",
        f"/admin/realms/{realms.managed_realm}/users",
        operator=operator,
        json=representation,
    )
    uuid = result.created_uuid()
    if uuid is None and username:
        uuid = await self._find_user_uuid(target, realms.managed_realm, username, operator=operator)
    return {
        "username": username,
        "id": uuid,
        "created": not result.conflict,
        "conflict": result.conflict,
    }


async def keycloak_user_reset_password(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Reset a user's password from Vault (``PUT .../users/{id}/reset-password``).

    Op-id: ``keycloak.user.reset_password``. Keys on the user's internal
    UUID; pass it via ``id`` or pass ``username`` for name→UUID resolution.
    The new password is read from Vault (``password_secret_ref``) and PUT
    as a CredentialRepresentation — **never** an inline param. Returns a
    value-free confirmation; the response never carries the password.
    """
    realms = resolve_realm_config(target)
    uuid = _opt_str(params.get("id"))
    username = _opt_str(params.get("username"))
    if uuid is None:
        if username is None:
            raise ValueError(
                "keycloak.user.reset_password requires either 'id' (UUID) or 'username'"
            )
        uuid = await self._find_user_uuid(target, realms.managed_realm, username, operator=operator)
        if uuid is None:
            raise KeycloakUserNotFoundError(
                f"keycloak.user.reset_password: no user with username={username!r} in realm "
                f"{realms.managed_realm!r}"
            )
    password = await _read_password_from_vault(operator, params)
    credential = {"type": "password", "value": password, "temporary": _temporary_flag(params)}
    await self._write_admin(
        target,
        "PUT",
        f"/admin/realms/{realms.managed_realm}/users/{uuid}/reset-password",
        operator=operator,
        json=credential,
        idempotent_conflict=False,
    )
    return {"id": uuid, "username": username, "password_reset": True}


# ---------------------------------------------------------------------------
# Role-mapping assign (privilege grant)
# ---------------------------------------------------------------------------


async def keycloak_role_mapping_assign(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Assign realm roles to a user (``POST .../users/{id}/role-mappings/realm``).

    Op-id: ``keycloak.role_mapping.assign``. A privilege grant
    (``safety_level="dangerous"``). Keys on the user's internal UUID (pass
    ``id`` or ``username``). ``roles`` is a list of **realm role names**;
    each is resolved to its full RoleRepresentation (the endpoint requires
    ``{id, name}`` in the body). The realm role-mappings endpoint is
    idempotent server-side — re-assigning an already-held role is a no-op —
    so this op is naturally re-runnable. Raises
    :exc:`KeycloakRoleNotFoundError` when a named role does not exist.
    """
    realms = resolve_realm_config(target)
    uuid = _opt_str(params.get("id"))
    username = _opt_str(params.get("username"))
    if uuid is None:
        if username is None:
            raise ValueError(
                "keycloak.role_mapping.assign requires either 'id' (UUID) or 'username'"
            )
        uuid = await self._find_user_uuid(target, realms.managed_realm, username, operator=operator)
        if uuid is None:
            raise KeycloakUserNotFoundError(
                f"keycloak.role_mapping.assign: no user with username={username!r} in realm "
                f"{realms.managed_realm!r}"
            )
    role_names = [str(r).strip() for r in params["roles"] if str(r).strip()]
    role_reprs: list[dict[str, Any]] = []
    for name in role_names:
        role = await self._find_realm_role(target, realms.managed_realm, name, operator=operator)
        if role is None:
            raise KeycloakRoleNotFoundError(
                f"keycloak.role_mapping.assign: realm role {name!r} does not exist in realm "
                f"{realms.managed_realm!r}"
            )
        role_reprs.append(role)
    await self._write_admin(
        target,
        "POST",
        f"/admin/realms/{realms.managed_realm}/users/{uuid}/role-mappings/realm",
        operator=operator,
        json=role_reprs,  # type: ignore[arg-type]  # KC accepts a JSON array body here
        idempotent_conflict=False,
    )
    return {"id": uuid, "username": username, "assigned_roles": role_names}


def _opt_str(value: Any) -> str | None:
    """Return a trimmed non-empty string, or ``None`` for absent/blank input."""
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return None


# The op-metadata table + curated blurbs + JSON-schema fragments live in a
# sibling module so this handler module stays under the file-size budget.
# Re-exported here so existing importers (and the connector's
# ``register_operations`` walk) keep importing from ``ops_write``.
from meho_backplane.connectors.keycloak.ops_write_schemas import (  # noqa: E402
    WHEN_TO_USE_WRITE_BY_GROUP,
    WRITE_OPS,
)

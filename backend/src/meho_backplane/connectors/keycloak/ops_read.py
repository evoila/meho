# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing module size (>600 lines before
# this change); #2843 appends two role read ops to the established READ_OPS
# pattern this file owns, so splitting the read-op module is out of scope here.

"""Curated read ops for :class:`KeycloakConnector` (G3.13-T2 #1394).

Eight ``safety_level="safe"`` / ``requires_approval=False`` read ops that
surface the realm / client / client-scope / user / role-mapping / role
config an operator would otherwise read with ``kcadm.sh get`` or the admin
console:

================================  =================================================
op_id                             Admin REST API
================================  =================================================
``keycloak.realm.get``            ``GET /admin/realms/{realm}``
``keycloak.client.list``          ``GET /admin/realms/{realm}/clients`` (``?clientId=``)
``keycloak.client.get``           ``GET /admin/realms/{realm}/clients/{id}``
``keycloak.client_scope.list``    ``GET /admin/realms/{realm}/client-scopes``
``keycloak.user.list``            ``GET /admin/realms/{realm}/users`` (``?username=``/``?max=``)
``keycloak.role_mapping.get``     ``GET /admin/realms/{realm}/users/{id}/role-mappings``
``keycloak.role.list``            ``GET /admin/realms/{realm}/roles``
``keycloak.role.users``           ``GET /admin/realms/{realm}/roles/{role-name}/users``
================================  =================================================

All ops dispatch via the **admin-auth** path the T1 substrate built
(``auth_headers`` → admin token Bearer), never the operator-OIDC path.
The realm is resolved from the target's ``managed_realm`` (``extras``
override, default ``evba``) so an op never has to be told which realm to
operate on — it operates on the realm the connector manages.

Secret redaction
================

Every handler runs its response through
:func:`~meho_backplane.connectors.keycloak.redaction.redact_secret_fields`
before returning, so confidential-client secrets
(``ClientRepresentation.secret``) and user credential material
(``UserRepresentation.credentials``) never reach the
:class:`~meho_backplane.connectors.schemas.OperationResult`. The scrub is
recursive — a secret nested inside a protocol mapper or identity-provider
config is caught too.

Handler signature
=================

The dispatcher introspects the handler signature by parameter **name**:
a handler declaring ``operator`` is invoked as
``handler(operator=, target=, params=)`` (see
:func:`meho_backplane.operations._branches.dispatch_typed`). The keycloak
read ops need the operator to authorise the operator-context Vault read
that backs the admin-token mint, so every handler takes ``operator`` —
unlike the bind9 / pfsense handlers, whose SSH transport carries no
per-operator credential.

The op-metadata dataclass + ``READ_OPS`` tuple mirror the bind9
(:mod:`~meho_backplane.connectors.bind9.ops`) and pfSense
(:mod:`~meho_backplane.connectors.pfsense.ops_read`) precedents so the
registration walk in
:meth:`KeycloakConnector.register_operations` reads identically.

References
----------

* Task: https://github.com/evoila/meho/issues/1394
* Parent initiative: https://github.com/evoila/meho/issues/1388
* Read-core precedents: G3.5 Harbor (#620) / SDDC Manager (#617).
* Keycloak 26.3 Admin REST API:
  https://www.keycloak.org/docs-api/26.3.3/rest-api/index.html
* RealmRepresentation / ClientRepresentation / UserRepresentation /
  MappingsRepresentation:
  https://www.keycloak.org/docs-api/latest/javadocs/org/keycloak/representations/idm/package-summary.html
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _CLIENT_PATH,
    _CLIENT_SCOPES_PATH,
    _CLIENTS_PATH,
    _ROLE_USERS_PATH,
    _ROLES_PATH,
    _USER_ROLE_MAPPINGS_PATH,
    _USERS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.redaction import redact_secret_fields
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "READ_OPS",
    "WHEN_TO_USE_BY_GROUP",
    "KeycloakOp",
    "keycloak_client_get",
    "keycloak_client_list",
    "keycloak_client_scope_list",
    "keycloak_realm_get",
    "keycloak_role_list",
    "keycloak_role_mapping_get",
    "keycloak_role_users",
    "keycloak_user_list",
]


@dataclass(frozen=True)
class KeycloakOp:
    """Metadata for one keycloak op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod can
    splat the dataclass into the helper without per-op boilerplate.
    ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.keycloak.connector.KeycloakConnector`
    exposing the async handler; the connector resolves the bound method
    against itself at registration time so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler`
    walk recovers the callable from the persisted
    ``module.ClassName.method`` dotted path.
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Handler functions (bound-method shims on KeycloakConnector)
# ---------------------------------------------------------------------------


async def keycloak_realm_get(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return the managed realm's top-level config (``GET /admin/realms/{realm}``).

    Op-id: ``keycloak.realm.get``. The realm is the target's
    ``managed_realm``; no params. The :class:`RealmRepresentation` carries
    realm-wide policy (login settings, token lifespans, SMTP, themes). Any
    nested secret is scrubbed before return.
    """
    del params  # declared empty; intentionally ignored
    realms = resolve_realm_config(target)
    realm = await self._get_admin_json(
        target, fill_path(_ADMIN_REALM_PATH, {"realm": realms.managed_realm}), operator=operator
    )
    return {"realm": redact_secret_fields(realm)}


async def keycloak_client_list(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """List clients in the managed realm (``GET /admin/realms/{realm}/clients``).

    Op-id: ``keycloak.client.list``. Optional ``client_id`` param maps to
    Keycloak's ``?clientId=`` exact-match filter; ``max`` caps the result
    count. Returns ``{rows, total}``; confidential-client secrets are
    scrubbed from every row.
    """
    realms = resolve_realm_config(target)
    query: dict[str, Any] = {}
    client_id = params.get("client_id")
    if isinstance(client_id, str) and client_id:
        query["clientId"] = client_id
    max_results = params.get("max")
    if isinstance(max_results, int):
        query["max"] = max_results
    rows = await self._get_admin_list(
        target,
        fill_path(_CLIENTS_PATH, {"realm": realms.managed_realm}),
        operator=operator,
        params=query or None,
    )
    scrubbed = [redact_secret_fields(row) for row in rows]
    return {"rows": scrubbed, "total": len(scrubbed)}


async def keycloak_client_get(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return one client by internal id (``GET /admin/realms/{realm}/clients/{id}``).

    Op-id: ``keycloak.client.get``. ``id`` is the client's **internal
    UUID** (the ``id`` field from ``keycloak.client.list``, not the
    human ``clientId``). The :class:`ClientRepresentation` carries the
    flows (``authenticationFlowBindingOverrides``), redirect URIs
    (``redirectUris``), and protocol mappers (``protocolMappers``) an
    operator reads from the admin console. The client ``secret`` is
    scrubbed.
    """
    realms = resolve_realm_config(target)
    client_uuid = quote_segment(params["id"])
    client = await self._get_admin_json(
        target,
        fill_path(_CLIENT_PATH, {"realm": realms.managed_realm, "client-uuid": client_uuid}),
        operator=operator,
    )
    return {"client": redact_secret_fields(client)}


async def keycloak_client_scope_list(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """List client scopes (``GET /admin/realms/{realm}/client-scopes``).

    Op-id: ``keycloak.client_scope.list``. No params. Each
    :class:`ClientScopeRepresentation` carries the scope's protocol
    mappers and attributes. Returns ``{rows, total}``.
    """
    del params  # declared empty; intentionally ignored
    realms = resolve_realm_config(target)
    rows = await self._get_admin_list(
        target,
        fill_path(_CLIENT_SCOPES_PATH, {"realm": realms.managed_realm}),
        operator=operator,
    )
    scrubbed = [redact_secret_fields(row) for row in rows]
    return {"rows": scrubbed, "total": len(scrubbed)}


async def keycloak_user_list(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """List users in the managed realm (``GET /admin/realms/{realm}/users``).

    Op-id: ``keycloak.user.list``. Optional ``username`` maps to
    Keycloak's ``?username=`` filter; ``max`` caps the result count.
    Returns ``{rows, total}``. User credential material
    (``UserRepresentation.credentials``) is scrubbed from every row — the
    op never surfaces credentials.
    """
    realms = resolve_realm_config(target)
    query: dict[str, Any] = {}
    username = params.get("username")
    if isinstance(username, str) and username:
        query["username"] = username
    max_results = params.get("max")
    if isinstance(max_results, int):
        query["max"] = max_results
    rows = await self._get_admin_list(
        target,
        fill_path(_USERS_PATH, {"realm": realms.managed_realm}),
        operator=operator,
        params=query or None,
    )
    scrubbed = [redact_secret_fields(row) for row in rows]
    return {"rows": scrubbed, "total": len(scrubbed)}


async def keycloak_role_mapping_get(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Return a user's role mappings (``GET .../users/{id}/role-mappings``).

    Op-id: ``keycloak.role_mapping.get``. ``id`` is the user's internal
    UUID. The :class:`MappingsRepresentation` carries ``realmMappings``
    (realm-level roles) and ``clientMappings`` (per-client roles). No
    secret material is present, but the response is run through the
    scrubber for defence-in-depth consistency with the sibling ops.
    """
    realms = resolve_realm_config(target)
    user_uuid = quote_segment(params["id"])
    mappings = await self._get_admin_json(
        target,
        fill_path(
            _USER_ROLE_MAPPINGS_PATH,
            {"realm": realms.managed_realm, "user-id": user_uuid},
        ),
        operator=operator,
    )
    return {"role_mappings": redact_secret_fields(mappings)}


async def keycloak_role_list(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """List the managed realm's roles (``GET /admin/realms/{realm}/roles``).

    Op-id: ``keycloak.role.list``. No params. Returns ``{rows, total}``;
    each row is a :class:`RoleRepresentation` (``id``, ``name``,
    ``description``, ``composite``, ``clientRole``, ``containerId``) — the
    realm's role catalogue an operator would list with
    ``kcadm.sh get roles``. This is the entry point for an access review:
    discover a role's ``name`` here, then read its members with
    ``keycloak.role.users``. No credential material is present, but the
    response is run through the scrubber for defence-in-depth consistency
    with the sibling ops.
    """
    del params  # declared empty; intentionally ignored
    realms = resolve_realm_config(target)
    rows = await self._get_admin_list(
        target,
        fill_path(_ROLES_PATH, {"realm": realms.managed_realm}),
        operator=operator,
    )
    scrubbed = [redact_secret_fields(row) for row in rows]
    return {"rows": scrubbed, "total": len(scrubbed)}


async def keycloak_role_users(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """List the users holding a realm role (``GET .../roles/{role-name}/users``).

    Op-id: ``keycloak.role.users``. ``name`` is the realm role's **name**
    (from ``keycloak.role.list``), not a UUID — Keycloak keys this endpoint
    on the role name, so the segment is only ``quote_segment``-encoded (no
    UUID-pattern gate). Returns ``{rows, total}``; each row is a
    :class:`UserRepresentation` with its ``credentials`` scrubbed — the
    same discipline ``keycloak.user.list`` applies, non-optional for any op
    returning ``UserRepresentation`` rows. Keycloak sorts the members by
    username and returns its default first page (100 users); a very large
    role is capped at that page.
    """
    realms = resolve_realm_config(target)
    role_name = quote_segment(params["name"])
    rows = await self._get_admin_list(
        target,
        fill_path(_ROLE_USERS_PATH, {"realm": realms.managed_realm, "role-name": role_name}),
        operator=operator,
    )
    scrubbed = [redact_secret_fields(row) for row in rows]
    return {"rows": scrubbed, "total": len(scrubbed)}


# ---------------------------------------------------------------------------
# Curated when_to_use blurbs (one per op group)
# ---------------------------------------------------------------------------

_WHEN_TO_USE_REALM = (
    "Use to read the managed realm's top-level configuration — login "
    "settings, token lifespans, themes, SMTP, and realm-wide policy — the "
    "same view ``kcadm.sh get realms/<realm>`` or the admin console's "
    "Realm Settings page shows. Call ``keycloak.realm.get`` when the "
    "operator wants to inspect or audit realm-level config."
)

_WHEN_TO_USE_CLIENT = (
    "Use for Keycloak client (OIDC/SAML relying-party) reads: list "
    "clients in the realm (``keycloak.client.list``, optionally filtered "
    "by ``client_id``) or fetch one client's full configuration by its "
    "internal UUID (``keycloak.client.get`` — flows, redirect URIs, "
    "protocol mappers). Confidential-client secrets are redacted. Call "
    "``keycloak.client.list`` first to discover a client's internal ``id``, "
    "then ``keycloak.client.get`` for its full representation."
)

_WHEN_TO_USE_CLIENT_SCOPE = (
    "Use to list the realm's client scopes "
    "(``keycloak.client_scope.list``) — the reusable bundles of protocol "
    "mappers and role scope-mappings clients attach as default/optional "
    "scopes. Call when the operator wants to audit which scopes (and "
    "their mappers) exist before assigning them to a client."
)

_WHEN_TO_USE_USER = (
    "Use to list realm users (``keycloak.user.list``, optionally filtered "
    "by ``username``) or read a user's effective role mappings "
    "(``keycloak.role_mapping.get`` by internal UUID — realm roles + "
    "per-client roles). User credential material is never returned. Call "
    "``keycloak.user.list`` to discover a user's internal ``id``, then "
    "``keycloak.role_mapping.get`` for that user's role assignments."
)

_WHEN_TO_USE_ROLE = (
    "Use for realm-role access review: list the realm's role catalogue "
    "(``keycloak.role.list`` — the roles ``kcadm.sh get roles`` shows) or "
    "read which users currently hold a given realm role "
    "(``keycloak.role.users`` by role **name**, not UUID). Call "
    "``keycloak.role.list`` to discover role names, then "
    "``keycloak.role.users`` with a role ``name`` to audit its members. "
    'This answers "which realm roles exist?" and "who holds '
    'operator/admin?" directly — the reverse of '
    "``keycloak.role_mapping.get``, which needs a user UUID and only "
    "reports that one user's roles."
)

#: Curated ``when_to_use`` blurb per op group, consumed by
#: :meth:`KeycloakConnector.register_operations` (a group_key without an
#: entry is a hard registration error). Co-located with the ops so the
#: curation and the metadata stay together.
WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "realm": _WHEN_TO_USE_REALM,
    "client": _WHEN_TO_USE_CLIENT,
    "client_scope": _WHEN_TO_USE_CLIENT_SCOPE,
    "user": _WHEN_TO_USE_USER,
    "role": _WHEN_TO_USE_ROLE,
}

#: UUID pattern used as a defence-in-depth constraint on every ``id``/``uuid``
#: parameter that is interpolated into an Admin REST path. The dispatcher's
#: JSON-schema gate rejects traversal-shaped inputs (e.g. ``../..``) before
#: the handler runs, complementing the ``quote_segment`` encoding applied at
#: the path-interpolation site. Pattern mirrors the canonical UUID v4 form
#: Keycloak uses for internal object identifiers.
_UUID_PATTERN: str = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

_EMPTY_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

_REALM_REPR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"realm": {"type": "object"}},
    "required": ["realm"],
    "additionalProperties": True,
}

_ROWS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "rows": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
    },
    "required": ["rows", "total"],
    "additionalProperties": True,
}


READ_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.realm.get",
        handler_attr="realm_get",
        summary="Read the managed Keycloak realm's top-level configuration.",
        description=(
            "GETs ``/admin/realms/{realm}`` against the target's managed "
            "realm and returns the RealmRepresentation under ``realm`` — "
            "login settings, token lifespans, themes, SMTP, and realm-wide "
            "policy. The same view ``kcadm.sh get realms/<realm>`` shows. "
            "Any nested secret is redacted. No params; dispatches via the "
            "admin-auth path; safe to call on any reachable target."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_REALM_REPR_SCHEMA,
        group_key="realm",
        tags=("read-only", "realm", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_REALM,
            "parameter_hints": {},
            "output_shape": (
                "``{realm: {<RealmRepresentation>}}``. The realm dict is the "
                "raw Keycloak realm config with secrets redacted."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.client.list",
        handler_attr="client_list",
        summary="List Keycloak clients in the managed realm (secrets redacted).",
        description=(
            "GETs ``/admin/realms/{realm}/clients`` and returns the "
            "clients as ``{rows, total}``. Optional ``client_id`` maps to "
            "Keycloak's ``?clientId=`` exact-match filter; ``max`` caps the "
            "result count. Each row is a ClientRepresentation with its "
            "``secret`` redacted. Use ``keycloak.client.get`` for one "
            "client's full config by internal UUID. Dispatches via the "
            "admin-auth path."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "client_id": {
                    "type": "string",
                    "description": "Exact clientId filter (Keycloak ?clientId=).",
                },
                "max": {
                    "type": "integer",
                    "description": "Cap on the number of clients returned.",
                },
            },
            "additionalProperties": False,
        },
        response_schema=_ROWS_SCHEMA,
        group_key="client",
        tags=("read-only", "client", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_CLIENT,
            "parameter_hints": {
                "client_id": "Pass to fetch a single client by its human clientId.",
                "max": "Pass to limit large realms.",
            },
            "output_shape": (
                "``{rows: [{<ClientRepresentation, secret redacted>}], "
                "total: N}``. Each row's ``id`` is the internal UUID for "
                "``keycloak.client.get``."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.client.get",
        handler_attr="client_get",
        summary="Read one Keycloak client's full config by internal UUID (secret redacted).",
        description=(
            "GETs ``/admin/realms/{realm}/clients/{id}`` where ``id`` is "
            "the client's internal UUID (the ``id`` from "
            "``keycloak.client.list`` — NOT the human ``clientId``). "
            "Returns the full ClientRepresentation under ``client``: "
            "authentication flow bindings, redirect URIs, web origins, and "
            "protocol mappers — the view an operator reads from the admin "
            "console. The client ``secret`` is redacted. Dispatches via "
            "the admin-auth path."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "The client's internal UUID (from keycloak.client.list).",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {"client": {"type": "object"}},
            "required": ["client"],
            "additionalProperties": True,
        },
        group_key="client",
        tags=("read-only", "client", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_CLIENT,
            "parameter_hints": {
                "id": "The internal UUID, not the clientId. Get it from keycloak.client.list."
            },
            "output_shape": (
                "``{client: {<ClientRepresentation>}}`` with "
                "``redirectUris``, ``protocolMappers``, and "
                "``authenticationFlowBindingOverrides`` present; ``secret`` "
                "redacted."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.client_scope.list",
        handler_attr="client_scope_list",
        summary="List Keycloak client scopes in the managed realm.",
        description=(
            "GETs ``/admin/realms/{realm}/client-scopes`` and returns the "
            "scopes as ``{rows, total}``. Each row is a "
            "ClientScopeRepresentation carrying the scope's protocol "
            "mappers and attributes — the reusable mapper/role bundles "
            "clients attach as default or optional scopes. No params; "
            "dispatches via the admin-auth path."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_ROWS_SCHEMA,
        group_key="client_scope",
        tags=("read-only", "client-scope", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_CLIENT_SCOPE,
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{<ClientScopeRepresentation>}], total: N}`` with "
                "each scope's ``protocolMappers`` present."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.user.list",
        handler_attr="user_list",
        summary="List Keycloak users in the managed realm (no credentials).",
        description=(
            "GETs ``/admin/realms/{realm}/users`` and returns the users as "
            "``{rows, total}``. Optional ``username`` maps to Keycloak's "
            "``?username=`` filter; ``max`` caps the result count. Each row "
            "is a UserRepresentation with its ``credentials`` redacted — "
            "the op never surfaces credential material. Use "
            "``keycloak.role_mapping.get`` for a user's role assignments. "
            "Dispatches via the admin-auth path."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Username filter (Keycloak ?username=).",
                },
                "max": {
                    "type": "integer",
                    "description": "Cap on the number of users returned.",
                },
            },
            "additionalProperties": False,
        },
        response_schema=_ROWS_SCHEMA,
        group_key="user",
        tags=("read-only", "user", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_USER,
            "parameter_hints": {
                "username": "Pass to fetch a single user by username.",
                "max": "Pass to limit large realms.",
            },
            "output_shape": (
                "``{rows: [{<UserRepresentation, credentials redacted>}], "
                "total: N}``. Each row's ``id`` is the internal UUID for "
                "``keycloak.role_mapping.get``."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.role_mapping.get",
        handler_attr="role_mapping_get",
        summary="Read a Keycloak user's realm + client role mappings by UUID.",
        description=(
            "GETs ``/admin/realms/{realm}/users/{id}/role-mappings`` where "
            "``id`` is the user's internal UUID (from "
            "``keycloak.user.list``). Returns the MappingsRepresentation "
            "under ``role_mappings``: ``realmMappings`` (realm-level roles) "
            "and ``clientMappings`` (per-client roles). Dispatches via the "
            "admin-auth path."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "The user's internal UUID (from keycloak.user.list).",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {"role_mappings": {"type": "object"}},
            "required": ["role_mappings"],
            "additionalProperties": True,
        },
        group_key="user",
        tags=("read-only", "role-mapping", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_USER,
            "parameter_hints": {"id": "The user's internal UUID, from keycloak.user.list."},
            "output_shape": ("``{role_mappings: {realmMappings: [...], clientMappings: {...}}}``."),
        },
    ),
    KeycloakOp(
        op_id="keycloak.role.list",
        handler_attr="role_list",
        summary="List the managed Keycloak realm's roles (the realm role catalogue).",
        description=(
            "GETs ``/admin/realms/{realm}/roles`` and returns the realm's "
            "role catalogue as ``{rows, total}``. Each row is a "
            "RoleRepresentation (``id``, ``name``, ``description``, "
            "``composite``, ``clientRole``, ``containerId``) — the realm "
            "roles ``kcadm.sh get roles`` lists. No params. Use "
            "``keycloak.role.users`` to read a role's members by name. "
            "Dispatches via the admin-auth path."
        ),
        parameter_schema=_EMPTY_PARAMS,
        response_schema=_ROWS_SCHEMA,
        group_key="role",
        tags=("read-only", "role", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_ROLE,
            "parameter_hints": {},
            "output_shape": (
                "``{rows: [{<RoleRepresentation>}], total: N}``. Each row's "
                "``name`` is the role name for ``keycloak.role.users``."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.role.users",
        handler_attr="role_users",
        summary="List the Keycloak users holding a realm role by role name (no credentials).",
        description=(
            "GETs ``/admin/realms/{realm}/roles/{role-name}/users`` where "
            "``name`` is the realm role's **name** (from "
            "``keycloak.role.list`` — NOT a UUID) and returns the users "
            "holding that role as ``{rows, total}``. Each row is a "
            "UserRepresentation with its ``credentials`` redacted — the op "
            "never surfaces credential material. Keycloak sorts by username "
            "and returns its default first page (100 users); a very large "
            "role is capped at that page. Dispatches via the admin-auth path."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The realm role's name (NOT a UUID; from keycloak.role.list).",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_ROWS_SCHEMA,
        group_key="role",
        tags=("read-only", "role", "keycloak"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_ROLE,
            "parameter_hints": {
                "name": "The realm role's name (not a UUID), from keycloak.role.list."
            },
            "output_shape": (
                "``{rows: [{<UserRepresentation, credentials redacted>}], "
                "total: N}`` — the realm-role members."
            ),
        },
    ),
)

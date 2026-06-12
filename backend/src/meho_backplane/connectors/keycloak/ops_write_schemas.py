# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op-metadata table + curated blurbs for the keycloak write ops (G3.13-T4 #1406).

Split out of :mod:`~meho_backplane.connectors.keycloak.ops_write` so the
handler module stays under the code-quality file-size budget (mirrors the
Vault connector's ``ops_auth_write`` / ``ops_auth_write_schemas`` split).
Carries the ``WRITE_OPS`` registration table, the per-group
``when_to_use`` blurbs (``WHEN_TO_USE_WRITE_BY_GROUP``), and the reusable
JSON-schema fragments. The handlers live in ``ops_write``; the connector
imports ``WRITE_OPS`` + ``WHEN_TO_USE_WRITE_BY_GROUP`` (re-exported from
``ops_write``) for its ``register_operations`` walk.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.ops_read import _UUID_PATTERN, KeycloakOp

__all__ = ["WHEN_TO_USE_WRITE_BY_GROUP", "WRITE_OPS"]


# ---------------------------------------------------------------------------
# Curated when_to_use blurbs (one per write op group)
# ---------------------------------------------------------------------------

_WHEN_TO_USE_REALM_WRITE = (
    "Use to create a new Keycloak realm (``keycloak.realm.create``) or "
    "update the managed realm's top-level config "
    "(``keycloak.realm.update``). Both require human approval. Pass the "
    "RealmRepresentation under ``representation``. Re-running a create is "
    "idempotent (a 409 already-exists is treated as success)."
)

_WHEN_TO_USE_CLIENT_WRITE = (
    "Use to create a Keycloak client (``keycloak.client.create``) or "
    "update one (``keycloak.client.update``) — flows, redirect URIs, web "
    "origins, mappers. Both require approval. Pass the "
    "ClientRepresentation under ``representation``. Updates key on the "
    "internal UUID — pass ``id`` directly, or pass ``client_id`` (the "
    "human clientId) for name→UUID resolution. Create is idempotent on a "
    "409 already-exists."
)

_WHEN_TO_USE_CLIENT_SCOPE_WRITE = (
    "Use to create a reusable client scope "
    "(``keycloak.client_scope.create``) — the bundle of protocol mappers "
    "and role scope-mappings clients attach as default/optional scopes. "
    "Requires approval. Pass the ClientScopeRepresentation under "
    "``representation``. Idempotent on a 409 already-exists."
)

_WHEN_TO_USE_PROTOCOL_MAPPER_WRITE = (
    "Use to add a protocol mapper to a client "
    "(``keycloak.protocol_mapper.create``) — e.g. the ``tenant_id`` / "
    "``tenant_role`` claim mappers the backplane row-scopes on. Requires "
    "approval. Key on the client UUID (``id``) or pass ``client_id`` for "
    "resolution; pass the ProtocolMapperRepresentation under "
    "``representation``. Idempotent on a 409 already-exists."
)

_WHEN_TO_USE_USER_WRITE = (
    "Use to create a user (``keycloak.user.create``) or reset a user's "
    "password (``keycloak.user.reset_password``). Both require approval. "
    "The password is NEVER passed inline — supply ``password_secret_ref`` "
    "(a Vault KV-v2 path; optional ``password_secret_mount`` / "
    "``password_secret_key``) and the connector reads it from Vault under "
    "your identity. Create keys on ``username`` (idempotent on a 409); "
    "reset keys on the user UUID (``id``) or ``username`` for resolution."
)

_WHEN_TO_USE_ROLE_MAPPING_WRITE = (
    "Use to grant realm roles to a user "
    "(``keycloak.role_mapping.assign``). A privilege grant — dangerous, "
    "requires approval. Key on the user UUID (``id``) or ``username``; "
    "pass ``roles`` as a list of realm role names. Each name is resolved "
    "to its role representation; an unknown role errors. Re-assigning an "
    "already-held role is a server-side no-op."
)

#: Curated ``when_to_use`` blurb per write op group. The group keys carry
#: a ``_write`` suffix so they never collide with the read-op group keys
#: in :data:`~meho_backplane.connectors.keycloak.ops_read.WHEN_TO_USE_BY_GROUP`
#: when the connector merges both maps in ``register_operations``.
WHEN_TO_USE_WRITE_BY_GROUP: dict[str, str] = {
    "realm_write": _WHEN_TO_USE_REALM_WRITE,
    "client_write": _WHEN_TO_USE_CLIENT_WRITE,
    "client_scope_write": _WHEN_TO_USE_CLIENT_SCOPE_WRITE,
    "protocol_mapper_write": _WHEN_TO_USE_PROTOCOL_MAPPER_WRITE,
    "user_write": _WHEN_TO_USE_USER_WRITE,
    "role_mapping_write": _WHEN_TO_USE_ROLE_MAPPING_WRITE,
}

# ---------------------------------------------------------------------------
# Reusable JSON-schema fragments
# ---------------------------------------------------------------------------

_REPRESENTATION_PROP: dict[str, Any] = {
    "type": "object",
    "description": "The Keycloak Admin REST representation body for the object.",
    "additionalProperties": True,
}

_PASSWORD_SECRET_PROPS: dict[str, Any] = {
    "password_secret_ref": {
        "type": "string",
        "description": (
            "Vault KV-v2 path the password is read from under the operator's "
            "identity. The password is NEVER passed inline."
        ),
    },
    "password_secret_mount": {
        "type": "string",
        "description": "Vault KV-v2 mount point (default 'secret').",
    },
    "password_secret_key": {
        "type": "string",
        "description": "Field within the Vault secret payload (default 'password').",
    },
    "temporary": {
        "type": "boolean",
        "description": "When true, forces a password change on first login (default false).",
    },
}

_WRITE_CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


WRITE_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.realm.create",
        handler_attr="realm_create",
        summary="Create a Keycloak realm (approval-gated).",
        description=(
            "POSTs ``/admin/realms`` with the RealmRepresentation under "
            "``representation``. The realm name is ``representation.realm`` "
            "(or the ``realm`` param). A 409 already-exists is treated as an "
            "idempotent success. requires_approval=True; dispatches via the "
            "admin-auth path."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "representation": _REPRESENTATION_PROP,
                "realm": {
                    "type": "string",
                    "description": "Realm name (if not in representation).",
                },
            },
            "required": ["representation"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="realm_write",
        tags=("write", "realm", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_REALM_WRITE,
            "parameter_hints": {
                "representation": "Full RealmRepresentation; at minimum {realm, enabled}.",
            },
            "output_shape": "``{realm, created, conflict}`` — no secret material.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.realm.update",
        handler_attr="realm_update",
        summary="Update a Keycloak realm's top-level config (approval-gated).",
        description=(
            "PUTs ``/admin/realms/{realm}`` (default the target's managed "
            "realm; ``realm`` overrides) with the partial "
            "RealmRepresentation under ``representation``. "
            "requires_approval=True; dispatches via the admin-auth path."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "representation": _REPRESENTATION_PROP,
                "realm": {
                    "type": "string",
                    "description": "Realm to update (default managed_realm).",
                },
            },
            "required": ["representation"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="realm_write",
        tags=("write", "realm", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_REALM_WRITE,
            "parameter_hints": {
                "representation": "Partial RealmRepresentation to merge.",
            },
            "output_shape": "``{realm, updated}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.client.create",
        handler_attr="client_create",
        summary="Create a Keycloak client (approval-gated).",
        description=(
            "POSTs ``/admin/realms/{realm}/clients`` with the "
            "ClientRepresentation (flows / redirect URIs / mappers) under "
            "``representation``. A 409 already-exists is idempotent; the "
            "existing client's UUID is resolved. Returns the internal UUID "
            "+ flags, never the client secret. requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "representation": _REPRESENTATION_PROP,
                "client_id": {
                    "type": "string",
                    "description": "clientId (if not in representation).",
                },
            },
            "required": ["representation"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="client_write",
        tags=("write", "client", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_CLIENT_WRITE,
            "parameter_hints": {
                "representation": "ClientRepresentation; at minimum {clientId}.",
            },
            "output_shape": "``{client_id, id, created, conflict}`` — secret never returned.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.client.update",
        handler_attr="client_update",
        summary="Update a Keycloak client by UUID or clientId (approval-gated).",
        description=(
            "PUTs ``/admin/realms/{realm}/clients/{id}`` with the partial "
            "ClientRepresentation under ``representation``. Keys on the "
            "internal UUID — pass ``id`` directly, or pass ``client_id`` "
            "(human clientId) for name→UUID resolution. "
            "requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "representation": _REPRESENTATION_PROP,
                "id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "Client internal UUID.",
                },
                "client_id": {
                    "type": "string",
                    "description": "Human clientId (resolved to UUID if id absent).",
                },
            },
            "required": ["representation"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="client_write",
        tags=("write", "client", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_CLIENT_WRITE,
            "parameter_hints": {
                "id": "Client internal UUID (from keycloak.client.list).",
                "client_id": "Human clientId; resolved to UUID when id is absent.",
            },
            "output_shape": "``{id, client_id, updated}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.client_scope.create",
        handler_attr="client_scope_create",
        summary="Create a Keycloak client scope (approval-gated).",
        description=(
            "POSTs ``/admin/realms/{realm}/client-scopes`` with the "
            "ClientScopeRepresentation (its ``protocolMappers`` ride in the "
            "body) under ``representation``. A 409 already-exists is "
            "idempotent. requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "representation": _REPRESENTATION_PROP,
                "name": {"type": "string", "description": "Scope name (if not in representation)."},
            },
            "required": ["representation"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="client_scope_write",
        tags=("write", "client-scope", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_CLIENT_SCOPE_WRITE,
            "parameter_hints": {
                "representation": "ClientScopeRepresentation; at minimum {name, protocol}.",
            },
            "output_shape": "``{name, id, created, conflict}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.protocol_mapper.create",
        handler_attr="protocol_mapper_create",
        summary="Add a protocol mapper to a client (approval-gated).",
        description=(
            "POSTs ``.../clients/{id}/protocol-mappers/models`` with the "
            "ProtocolMapperRepresentation under ``representation`` — wires "
            "claims like ``tenant_id`` / ``tenant_role``. Keys on the "
            "client UUID (``id``) or ``client_id`` for resolution. A 409 "
            "already-exists is idempotent. requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "representation": _REPRESENTATION_PROP,
                "id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "Client internal UUID.",
                },
                "client_id": {
                    "type": "string",
                    "description": "Human clientId (resolved to UUID if id absent).",
                },
            },
            "required": ["representation"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="protocol_mapper_write",
        tags=("write", "protocol-mapper", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_PROTOCOL_MAPPER_WRITE,
            "parameter_hints": {
                "representation": "ProtocolMapperRepresentation: {name, protocol, protocolMapper}.",
                "client_id": "Human clientId; resolved to UUID when id is absent.",
            },
            "output_shape": "``{client_uuid, client_id, mapper_name, created, conflict}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.user.create",
        handler_attr="user_create",
        summary="Create a user with a Vault-sourced password (approval-gated).",
        description=(
            "POSTs ``/admin/realms/{realm}/users`` with the "
            "UserRepresentation under ``representation``. The password is "
            "read from Vault (``password_secret_ref``) and set as a "
            "credential — NEVER passed inline. A 409 already-exists is "
            "idempotent; the existing user's UUID is resolved. Returns the "
            "UUID + flags, never the password. requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "representation": _REPRESENTATION_PROP,
                "username": {
                    "type": "string",
                    "description": "Username (if not in representation).",
                },
                **_PASSWORD_SECRET_PROPS,
            },
            "required": ["representation"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="user_write",
        tags=("write", "user", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_USER_WRITE,
            "parameter_hints": {
                "representation": "UserRepresentation; at minimum {username, enabled}.",
                "password_secret_ref": "Vault KV-v2 path to the password. Never pass it inline.",
            },
            "output_shape": "``{username, id, created, conflict}`` — password never returned.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.user.reset_password",
        handler_attr="user_reset_password",
        summary="Reset a user's password from Vault (approval-gated).",
        description=(
            "PUTs ``.../users/{id}/reset-password`` with a "
            "CredentialRepresentation whose value is read from Vault "
            "(``password_secret_ref``) — NEVER passed inline. Keys on the "
            "user UUID (``id``) or ``username`` for resolution. Returns a "
            "value-free confirmation. requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "User internal UUID.",
                },
                "username": {
                    "type": "string",
                    "description": "Username (resolved to UUID if id absent).",
                },
                **_PASSWORD_SECRET_PROPS,
            },
            "required": ["password_secret_ref"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="user_write",
        tags=("write", "user", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_USER_WRITE,
            "parameter_hints": {
                "password_secret_ref": "Vault KV-v2 path to the new password. Never inline.",
                "username": "Username; resolved to UUID when id is absent.",
            },
            "output_shape": "``{id, username, password_reset}`` — password never returned.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.role_mapping.assign",
        handler_attr="role_mapping_assign",
        summary="Grant realm roles to a user — privilege grant (approval-gated).",
        description=(
            "POSTs ``.../users/{id}/role-mappings/realm`` with the resolved "
            "RoleRepresentations for the named ``roles``. A privilege grant "
            "(dangerous). Keys on the user UUID (``id``) or ``username``. "
            "Each role name is resolved to its representation; an unknown "
            "role errors. Re-assigning a held role is a server-side no-op. "
            "requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "User internal UUID.",
                },
                "username": {
                    "type": "string",
                    "description": "Username (resolved to UUID if id absent).",
                },
                "roles": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Realm role names to grant.",
                },
            },
            "required": ["roles"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_CONFIRM_SCHEMA,
        group_key="role_mapping_write",
        tags=("write", "role-mapping", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_ROLE_MAPPING_WRITE,
            "parameter_hints": {
                "roles": "List of realm role names (e.g. ['tenant_admin']).",
                "username": "Username; resolved to UUID when id is absent.",
            },
            "output_shape": "``{id, username, assigned_roles}``.",
        },
    ),
)

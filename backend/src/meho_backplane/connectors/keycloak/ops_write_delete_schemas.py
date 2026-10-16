# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op-metadata rows for the governed Keycloak deletes (#3944, #3886).

Kept apart from the handlers in
:mod:`~meho_backplane.connectors.keycloak.ops_write_delete` so both modules
stay under the file-size budget (the ``ops_write`` / ``ops_write_schemas``
split). :mod:`~meho_backplane.connectors.keycloak.ops_write_schemas` folds
:data:`DELETE_WRITE_OPS` into the connector's ``WRITE_OPS``. Each delete sits
in the operation group of the object it removes (``user_write``,
``group_write``, ``client_write``, ``realm_write``), the way other connectors
group their deletes with the object. The realm delete (#3886) has its handler
in :mod:`~meho_backplane.connectors.keycloak.ops_write_realm_delete`.

All four rows are ``safety_level="destructive"`` + ``requires_approval=True``
and carry the ``destructive`` tag, so they take the governed-delete path: a
second person must approve, the caller must first run ``preview_operation``
and pass its ``preview_hash``, and the approval card shows what is deleted.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.ops_read import _UUID_PATTERN, KeycloakOp

__all__ = ["DELETE_WRITE_OPS"]

_HOW_TO_RUN = (
    "Destructive: a second person must approve. First call preview_operation "
    "with the same params and pass its preview_hash to call_operation. "
    "Safe to run twice: an object that is already gone returns "
    "already_absent=true. deleted=true is reported only after a read of the "
    "object answers 404."
)

_DELETE_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string", "description": "The realm the object was in."},
        "id": {"type": ["string", "null"], "description": "The object's internal id."},
        "deleted": {
            "type": "boolean",
            "description": "True only after a read of the object answered 404.",
        },
        "already_absent": {
            "type": "boolean",
            "description": "True when the object was already gone; nothing was deleted.",
        },
    },
    "additionalProperties": True,
}

_USER_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": "The user's internal UUID (from keycloak.user.list).",
        },
        "username": {
            "type": "string",
            "description": "The exact username. Used when id is absent.",
        },
    },
    "anyOf": [{"required": ["id"]}, {"required": ["username"]}],
    "additionalProperties": False,
}

_GROUP_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": "The group's internal UUID (from keycloak.group.list).",
        },
        "name": {
            "type": "string",
            "description": (
                "The exact group name, searched at every level of the group tree. "
                "Used when id is absent. A name that matches more than one group "
                "is refused."
            ),
        },
        "delete_subgroups": {
            "type": "boolean",
            "description": (
                "Default false: a group that has sub-groups is refused. Set true "
                "to delete the group together with all its sub-groups."
            ),
        },
    },
    "anyOf": [{"required": ["id"]}, {"required": ["name"]}],
    "additionalProperties": False,
}

_CLIENT_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": "The client's internal UUID (from keycloak.client.list).",
        },
        "client_id": {
            "type": "string",
            "description": "The exact clientId (the client's name). Used when id is absent.",
        },
    },
    "anyOf": [{"required": ["id"]}, {"required": ["client_id"]}],
    "additionalProperties": False,
}


_REALM_DELETE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {
            "type": "string",
            "minLength": 1,
            "description": (
                "The name of the realm to delete. Required: the target's managed "
                "realm is never used as a default."
            ),
        },
    },
    "required": ["realm"],
    "additionalProperties": False,
}

_REALM_DELETE_RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "realm": {"type": "string", "description": "The realm that was deleted."},
        "deleted": _DELETE_RESULT_SCHEMA["properties"]["deleted"],
        "already_absent": _DELETE_RESULT_SCHEMA["properties"]["already_absent"],
    },
    "additionalProperties": True,
}


DELETE_WRITE_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.user.delete",
        handler_attr="user_delete",
        summary="Delete a Keycloak user (destructive, a second person must approve).",
        description=(
            "DELETEs ``/admin/realms/{realm}/users/{id}`` in the target's managed "
            "realm. Takes the user's ``id`` or exact ``username``; a username that "
            "matches more than one user is refused. Refuses the user this MEHO "
            "target logs in with. The approval card shows the user name, whether "
            "the user is enabled, and its groups. " + _HOW_TO_RUN
        ),
        parameter_schema=_USER_DELETE_SCHEMA,
        response_schema=_DELETE_RESULT_SCHEMA,
        group_key="user_write",
        tags=("write", "destructive", "user", "keycloak"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Use to remove a person's or a service's Keycloak account for good, "
                "for example when an event, a contract or a job ends. " + _HOW_TO_RUN
            ),
            "parameter_hints": {
                "id": "The user's internal UUID (from keycloak.user.list).",
                "username": "The exact username, when you do not have the id.",
            },
            "output_shape": "``{realm, id, username, deleted, already_absent}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.group.delete",
        handler_attr="group_delete",
        summary="Delete a Keycloak group (destructive, a second person must approve).",
        description=(
            "DELETEs ``/admin/realms/{realm}/groups/{id}`` in the target's managed "
            "realm. Keycloak deletes the group's sub-groups with it, so a group that "
            "has sub-groups is refused unless ``delete_subgroups`` is true. Takes the "
            "group's ``id`` or exact ``name`` (searched at every level); a name that "
            "matches more than one group is refused. Members are not deleted; they "
            "lose this membership. The approval card shows the group name, the "
            "number of members and the number of sub-groups. " + _HOW_TO_RUN
        ),
        parameter_schema=_GROUP_DELETE_SCHEMA,
        response_schema=_DELETE_RESULT_SCHEMA,
        group_key="group_write",
        tags=("write", "destructive", "group", "keycloak"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Use to remove a Keycloak group for good, for example a role group "
                "for an event that has ended. Its members keep their accounts but "
                "lose the group's tenant claims. To remove one person from a group, "
                "use keycloak.group.member.remove instead. " + _HOW_TO_RUN
            ),
            "parameter_hints": {
                "id": "The group's internal UUID (from keycloak.group.list).",
                "name": "The exact group name, when you do not have the id.",
                "delete_subgroups": "Pass true only when the sub-groups must go too.",
            },
            "output_shape": "``{realm, id, name, path, deleted, already_absent}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.client.delete",
        handler_attr="client_delete",
        summary="Delete a Keycloak client (destructive, a second person must approve).",
        description=(
            "DELETEs ``/admin/realms/{realm}/clients/{id}`` in the target's managed "
            "realm. Takes the client's internal ``id`` or exact ``client_id``. "
            "Refuses Keycloak's built-in clients (admin-cli, account, "
            "account-console, broker, realm-management, security-admin-console) and "
            "the client this MEHO target logs in with. The approval card shows the "
            "client id, whether it is enabled, and whether it is public or "
            "confidential; never the client secret. " + _HOW_TO_RUN
        ),
        parameter_schema=_CLIENT_DELETE_SCHEMA,
        response_schema=_DELETE_RESULT_SCHEMA,
        group_key="client_write",
        tags=("write", "destructive", "client", "keycloak"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Use to remove a Keycloak client (an app registration people or "
                "services log in through) for good. Every token and session issued "
                "to it stops working. To stop logins but keep the client, use "
                "keycloak.client.update with enabled=false instead. " + _HOW_TO_RUN
            ),
            "parameter_hints": {
                "id": "The client's internal UUID (from keycloak.client.list).",
                "client_id": "The exact clientId, when you do not have the id.",
            },
            "output_shape": "``{realm, id, client_id, deleted, already_absent}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.realm.delete",
        handler_attr="realm_delete",
        summary="Delete a whole Keycloak realm (destructive, a second person must approve).",
        description=(
            "DELETEs ``/admin/realms/{realm}``. Every user, client, group and "
            "session in the realm goes with it. ``realm`` is required; the op "
            "never falls back to the target's managed realm. Refuses "
            "(``protected_realm``) the target's admin realm, its managed realm, "
            "and the realm MEHO's own login uses. The login realm is refused on "
            "every Keycloak target, because MEHO cannot tell for sure that a "
            "target is a different Keycloak. The approval card shows whether the "
            "realm is enabled and how many users, clients and groups it has. " + _HOW_TO_RUN
        ),
        parameter_schema=_REALM_DELETE_SCHEMA,
        response_schema=_REALM_DELETE_RESULT_SCHEMA,
        group_key="realm_write",
        tags=("write", "destructive", "realm", "keycloak"),
        safety_level="destructive",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Use to remove a short-lived realm for good, for example a test or "
                "demo realm, or a failed set-up that must be cleaned before a retry. "
                "To stop logins but keep the realm, use keycloak.realm.update with "
                "representation={enabled: false} instead; that can be undone. "
                "The realm MEHO's own login uses is refused on every Keycloak "
                "target, even when the target is a different Keycloak. " + _HOW_TO_RUN
            ),
            "parameter_hints": {
                "realm": "The exact name of the realm to delete. Required.",
            },
            "output_shape": "``{realm, deleted, already_absent}``.",
        },
    ),
)

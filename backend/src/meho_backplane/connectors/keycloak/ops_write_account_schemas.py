# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op-metadata rows for the Keycloak user account writes.

Four approval-gated ops:

* ``keycloak.role_mapping.client.assign`` and
  ``keycloak.role_mapping.client.remove`` (handlers in
  :mod:`~meho_backplane.connectors.keycloak.ops_write_client_role`),
* ``keycloak.user.update`` (handler in
  :mod:`~meho_backplane.connectors.keycloak.ops_write_user_update`),
* ``keycloak.user.credential.delete`` (handler in
  :mod:`~meho_backplane.connectors.keycloak.ops_write_user_credential`).

:mod:`~meho_backplane.connectors.keycloak.ops_write_schemas` folds
:data:`ACCOUNT_WRITE_OPS` into the connector's ``WRITE_OPS`` and
:data:`WHEN_TO_USE_ACCOUNT_WRITE` into ``WHEN_TO_USE_WRITE_BY_GROUP`` under
the ``account_write`` group key.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.ops_read import _UUID_PATTERN, KeycloakOp

__all__ = ["ACCOUNT_WRITE_OPS", "WHEN_TO_USE_ACCOUNT_WRITE"]

WHEN_TO_USE_ACCOUNT_WRITE = (
    "Use to change one user's access or details. "
    "``keycloak.role_mapping.client.assign`` / ``keycloak.role_mapping.client.remove`` "
    "give or take roles of one client (for example ``realm-admin`` of the "
    "``realm-management`` client, so a person can use the admin console of one "
    "realm). ``keycloak.user.update`` changes the user name, email, first name "
    "and last name. ``keycloak.user.credential.delete`` removes one stored "
    "credential, for example a lost passkey or a one-time code app on a lost "
    "phone; the other credentials stay. All four wait for approval. Giving a "
    "client role and removing a credential are dangerous: a second person "
    "must approve."
)

_USER_PROPS: dict[str, Any] = {
    "id": {
        "type": "string",
        "pattern": _UUID_PATTERN,
        "description": "User internal UUID (from keycloak.user.list).",
    },
    "username": {
        "type": "string",
        "description": "Exact current username (resolved to the UUID if id is absent).",
    },
}

_USER_ANY: list[dict[str, Any]] = [{"required": ["id"]}, {"required": ["username"]}]

_PRECONDITIONS = (
    "The user exists in the target's managed realm. A second person with the "
    "right to approve this risk level approves the call."
)

_ROLE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_USER_PROPS,
        "client_id": {
            "type": "string",
            "description": "The client's name, for example realm-management (not its UUID).",
        },
        "roles": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string"},
            "description": "Names of roles of that client, for example realm-admin.",
        },
    },
    "required": ["client_id", "roles"],
    "anyOf": _USER_ANY,
    "additionalProperties": False,
    "examples": [
        {"username": "jane.example", "client_id": "realm-management", "roles": ["realm-admin"]}
    ],
}

_ROLE_RESULT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "The user's internal UUID."},
        "username": {
            "type": ["string", "null"],
            "description": "The username from the request; null when only id was passed.",
        },
        "client_id": {"type": "string", "description": "The client's name from the request."},
        "roles": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The role names that were sent.",
        },
        "read_back_checked": {
            "type": "boolean",
            "description": "False when the read after the write failed. The write itself ran.",
        },
        "not_applied": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Role names not in the requested state after the write. Null if unread.",
        },
    },
    "additionalProperties": True,
}

_UPDATE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_USER_PROPS,
        "changes": {
            "type": "object",
            "minProperties": 1,
            "description": "The fields to change. Only these change; every other field stays.",
            "properties": {
                "username": {
                    "type": "string",
                    "description": "New user name. Works only if the realm allows it.",
                },
                "email": {"type": "string", "description": "New email address."},
                "firstName": {"type": "string", "description": "New first name."},
                "lastName": {"type": "string", "description": "New last name."},
            },
            "additionalProperties": False,
        },
    },
    "required": ["changes"],
    "anyOf": _USER_ANY,
    "additionalProperties": False,
    "examples": [{"username": "jane.example", "changes": {"email": "jane@example.com"}}],
}

_UPDATE_RESULT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "The user's internal UUID."},
        "username": {
            "type": ["string", "null"],
            "description": "The username from the request; null when only id was passed.",
        },
        "requested_fields": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The field names that were sent.",
        },
        "applied_fields": {
            "type": ["array", "null"],
            "items": {"type": "string"},
            "description": "Fields that now hold the requested value. Null when not checked.",
        },
        "not_applied": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string", "description": "The field that did not change."},
                    "reason": {"type": "string", "description": "Why Keycloak kept the old value."},
                },
            },
            "description": "Fields Keycloak did not change, with the reason. Null if not checked.",
        },
        "unrequested_changes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Names of fields that changed without being asked. Never values.",
        },
        "read_back_checked": {
            "type": "boolean",
            "description": "False when the read after the write failed. The write itself ran.",
        },
    },
    "additionalProperties": True,
}

_CREDENTIAL_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "The credential id."},
        "type": {"type": "string", "description": "The kind, for example otp or webauthn."},
        "userLabel": {"type": "string", "description": "The label the user gave it."},
        "createdDate": {"type": "integer", "description": "When it was created, in ms."},
    },
}

_CREDENTIAL_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_USER_PROPS,
        "credential_id": {
            "type": "string",
            "description": "Id of the one credential to remove.",
        },
        "credential_type": {
            "type": "string",
            "description": "Kind to remove, for example otp. Removes every credential of it.",
        },
    },
    "anyOf": _USER_ANY,
    "oneOf": [{"required": ["credential_id"]}, {"required": ["credential_type"]}],
    "additionalProperties": False,
    "examples": [{"username": "jane.example", "credential_type": "webauthn-passwordless"}],
}

_CREDENTIAL_RESULT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "The user's internal UUID."},
        "username": {
            "type": ["string", "null"],
            "description": "The username from the request; null when only id was passed.",
        },
        "removed": {
            "type": "array",
            "items": _CREDENTIAL_ITEM,
            "description": "The credentials removed: id, type, label, date. No secret data.",
        },
        "remaining": {
            "type": ["array", "null"],
            "items": _CREDENTIAL_ITEM,
            "description": "The credentials the user still has. Null when not checked.",
        },
        "read_back_checked": {
            "type": "boolean",
            "description": "False when the read after the deletes failed. The deletes ran.",
        },
    },
    "additionalProperties": True,
}

_ROLE_HINTS = {
    "id": "User internal UUID (from keycloak.user.list).",
    "username": "Username; resolved to UUID when id is absent.",
    "client_id": "The client's name, for example realm-management.",
    "roles": "Role names of that client. Read them with keycloak.client.get.",
}

ACCOUNT_WRITE_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.role_mapping.client.assign",
        handler_attr="role_mapping_client_assign",
        summary="Give a user roles of one client, a privilege grant (approval-gated).",
        description=(
            "Gives a user roles of one client (for example realm-admin of "
            "realm-management). Reads each role first; an unknown client or "
            "role is an error and nothing is sent. Safe to run twice. "
            "requires_approval=True."
        ),
        parameter_schema=_ROLE_PARAMS,
        response_schema=_ROLE_RESULT,
        group_key="account_write",
        tags=("write", "role-mapping", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": WHEN_TO_USE_ACCOUNT_WRITE,
            "when_to_call": "When a person needs a client role, such as the admin console role.",
            "preconditions": _PRECONDITIONS,
            "next_step": (
                "Check the result with keycloak.role_mapping.get for the same user. "
                "To undo, run keycloak.role_mapping.client.remove."
            ),
            "parameter_hints": _ROLE_HINTS,
            "output_shape": "``{id, username, client_id, roles, read_back_checked, not_applied}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.role_mapping.client.remove",
        handler_attr="role_mapping_client_remove",
        summary="Take roles of one client from a user (approval-gated).",
        description=(
            "Takes roles of one client from a user. Reads each role first; an "
            "unknown client or role is an error and nothing is sent. Safe to "
            "run twice. requires_approval=True."
        ),
        parameter_schema=_ROLE_PARAMS,
        response_schema=_ROLE_RESULT,
        group_key="account_write",
        tags=("write", "role-mapping", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": WHEN_TO_USE_ACCOUNT_WRITE,
            "when_to_call": "When a person must lose a role of a client.",
            "preconditions": _PRECONDITIONS,
            "next_step": (
                "Check the result with keycloak.role_mapping.get for the same user. "
                "To undo, run keycloak.role_mapping.client.assign."
            ),
            "parameter_hints": _ROLE_HINTS,
            "output_shape": "``{id, username, client_id, roles, read_back_checked, not_applied}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.user.update",
        handler_attr="user_update",
        summary="Change a Keycloak user's name, email, first name or last name (approval-gated).",
        description=(
            "Reads the user, puts only the given fields on top, writes the user "
            "back and reads it again. Says which field did not change and why; "
            "a user name changes only if the realm sets editUsernameAllowed. "
            "requires_approval=True."
        ),
        parameter_schema=_UPDATE_PARAMS,
        response_schema=_UPDATE_RESULT,
        group_key="account_write",
        tags=("write", "user", "keycloak"),
        safety_level="caution",
        requires_approval=True,
        llm_instructions={
            "when_to_use": WHEN_TO_USE_ACCOUNT_WRITE,
            "when_to_call": "When a person's user name, email or name is wrong or has changed.",
            "preconditions": _PRECONDITIONS,
            "next_step": (
                "Read not_applied. If the user name did not change, the realm does "
                "not allow it: set editUsernameAllowed with keycloak.realm.update, "
                "then run this again."
            ),
            "parameter_hints": {
                "id": "User internal UUID (from keycloak.user.list).",
                "username": "Current username; resolved to UUID when id is absent.",
                "changes": "Only the fields to change: username, email, firstName, lastName.",
            },
            "output_shape": (
                "``{id, username, requested_fields, applied_fields, not_applied, "
                "unrequested_changes, read_back_checked}``. Field names, never values."
            ),
        },
    ),
    KeycloakOp(
        op_id="keycloak.user.credential.delete",
        handler_attr="user_credential_delete",
        summary="Remove one stored credential of a Keycloak user (approval-gated).",
        description=(
            "Removes one stored credential by id, or every one of a type, for "
            "example a lost passkey or a one-time code app. The other "
            "credentials stay. Returns type, label and date only, never secret "
            "data. requires_approval=True."
        ),
        parameter_schema=_CREDENTIAL_PARAMS,
        response_schema=_CREDENTIAL_RESULT,
        group_key="account_write",
        tags=("write", "user", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": WHEN_TO_USE_ACCOUNT_WRITE,
            "when_to_call": "When a person lost a passkey or a phone with a one-time code app.",
            "preconditions": _PRECONDITIONS,
            "next_step": (
                "Check what is left in remaining. The person may need to set up "
                "a new one-time code or passkey the next time they sign in."
            ),
            "parameter_hints": {
                "id": "User internal UUID (from keycloak.user.list).",
                "username": "Username; resolved to UUID when id is absent.",
                "credential_id": "One credential id.",
                "credential_type": "A kind such as otp or webauthn-passwordless. Removes all.",
            },
            "output_shape": "``{id, username, removed, remaining, read_back_checked}``.",
        },
    ),
)

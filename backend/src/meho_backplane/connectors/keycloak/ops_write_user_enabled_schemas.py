# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op-metadata rows for ``keycloak.user.disable`` and ``keycloak.user.enable``.

The handlers are in
:mod:`~meho_backplane.connectors.keycloak.ops_write_user_enabled`.
:mod:`~meho_backplane.connectors.keycloak.ops_write_schemas` folds
:data:`USER_ENABLED_OPS` into the connector's ``WRITE_OPS``. Both rows are
``dangerous`` with ``requires_approval=True``: enabling a user lets that
person sign in again, and disabling one blocks a person from every app of
the realm.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.ops_read import _UUID_PATTERN, KeycloakOp

__all__ = ["USER_ENABLED_OPS"]

_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "pattern": _UUID_PATTERN,
            "description": "User internal UUID (from keycloak.user.list).",
        },
        "username": {
            "type": "string",
            "description": "Exact username (resolved to UUID if id absent).",
        },
    },
    "anyOf": [{"required": ["id"]}, {"required": ["username"]}],
    "additionalProperties": False,
    "examples": [{"username": "jane.example"}],
}

_RESULT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "The user's internal UUID."},
        "username": {
            "type": ["string", "null"],
            "description": "The username from the request; null when only id was passed.",
        },
        "enabled": {"type": "boolean", "description": "The value of enabled after the run."},
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

_HINTS = {
    "id": "User internal UUID (from keycloak.user.list).",
    "username": "Username; resolved to UUID when id is absent.",
}

_WHEN_TO_USE_DISABLE = (
    "Use to turn a person's account off without deleting it "
    "(``keycloak.user.disable``), so they can no longer sign in to any app "
    "that uses this realm, and the account and its history stay. Dangerous: "
    "a MEHO-SA-Operator or a tenant admin must approve. Key on the user UUID "
    "(``id``) or ``username``. Disabling does NOT end sessions that are "
    "already open. To cut access at once, run ``keycloak.user.disable`` and "
    "then ``keycloak.user.logout``. Safe to run twice."
)

_WHEN_TO_USE_ENABLE = (
    "Use to turn a disabled account on again (``keycloak.user.enable``), so "
    "the person can sign in again. Dangerous: a MEHO-SA-Operator or a tenant "
    "admin must approve. Key on the user UUID (``id``) or ``username``. "
    "Safe to run twice."
)

_NEXT_DISABLE = (
    "Disabling does not end open sessions. To cut access at once, run "
    "keycloak.user.logout for the same user next. To undo, run "
    "keycloak.user.enable."
)

_NEXT_ENABLE = "The person can sign in again. To turn the account off, run keycloak.user.disable."

_PRECONDITIONS = (
    "The user exists in the target's managed realm. A second person with the "
    "right to approve a dangerous change approves the call."
)

USER_ENABLED_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.user.disable",
        handler_attr="user_disable",
        summary="Turn a Keycloak user off without deleting it (approval-gated).",
        description=(
            "Reads the user, sets ``enabled`` to false and writes the whole "
            "user back, so every other field stays. Keys on the user UUID "
            "(``id``) or ``username``. Does not end open sessions: run "
            "``keycloak.user.logout`` after it to cut access at once. Safe to "
            "run twice. requires_approval=True."
        ),
        parameter_schema=_PARAMS,
        response_schema=_RESULT,
        group_key="user_write",
        tags=("write", "user", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_DISABLE,
            "when_to_call": "When a person leaves, and their account must stop working but stay.",
            "preconditions": _PRECONDITIONS,
            "next_step": _NEXT_DISABLE,
            "parameter_hints": _HINTS,
            "output_shape": "``{id, username, enabled, unrequested_changes, read_back_checked}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.user.enable",
        handler_attr="user_enable",
        summary="Turn a disabled Keycloak user on again (approval-gated).",
        description=(
            "Reads the user, sets ``enabled`` to true and writes the whole "
            "user back, so every other field stays. Keys on the user UUID "
            "(``id``) or ``username``. Safe to run twice. "
            "requires_approval=True."
        ),
        parameter_schema=_PARAMS,
        response_schema=_RESULT,
        group_key="user_write",
        tags=("write", "user", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE_ENABLE,
            "when_to_call": "When a person returns, and their disabled account must work again.",
            "preconditions": _PRECONDITIONS,
            "next_step": _NEXT_ENABLE,
            "parameter_hints": _HINTS,
            "output_shape": "``{id, username, enabled, unrequested_changes, read_back_checked}``.",
        },
    ),
)

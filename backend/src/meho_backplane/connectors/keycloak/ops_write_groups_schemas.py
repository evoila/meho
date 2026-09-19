# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op-metadata table + curated blurb for the keycloak group write ops (#3280).

Split out of
:mod:`~meho_backplane.connectors.keycloak.ops_write_groups` so the handler
module stays under the code-quality file-size budget (mirrors the
``ops_write`` / ``ops_write_schemas`` split). Carries ``GROUP_WRITE_OPS``
(the four approval-gated group-lifecycle op rows) and the curated
``WHEN_TO_USE_GROUP_WRITE`` blurb; the handlers live in
``ops_write_groups``. ``ops_write_schemas`` folds ``GROUP_WRITE_OPS`` into
the connector's ``WRITE_OPS`` and ``WHEN_TO_USE_GROUP_WRITE`` into
``WHEN_TO_USE_WRITE_BY_GROUP`` under the ``group_write`` group key.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.keycloak.ops_read import _UUID_PATTERN, KeycloakOp

__all__ = ["GROUP_WRITE_OPS", "WHEN_TO_USE_GROUP_WRITE"]


WHEN_TO_USE_GROUP_WRITE = (
    "Use to bootstrap or fix a realm's role groups — the backplane's "
    "tenant-claim primitive. ``keycloak.group.create`` creates a group and "
    "sets its ``attributes`` (the ``tenant_id`` / ``tenant_role`` an "
    "aggregated attribute mapper mints into the token); "
    "``keycloak.group.update_attributes`` merges or replaces attributes on "
    "an existing group to fix a mis-attributed one without recreating it; "
    "``keycloak.group.member.add`` / ``keycloak.group.member.remove`` "
    "attach or detach a user (so the user's token carries the group's "
    "tenant claims). All four are privilege-adjacent (a tenant-access "
    "grant), so all are dangerous and require human approval. The typical "
    "flow: create the role group with attributes → add the user → the "
    "user's minted token then carries the group-derived claims."
)

_ATTRIBUTES_PROP: dict[str, Any] = {
    "type": "object",
    "description": (
        "Group attributes as Keycloak's Map<String, List<String>> "
        '(e.g. {"tenant_id": ["t-2"], "tenant_role": ["admin"]}). A plain '
        "string value is wrapped into a single-element list."
    ),
    "additionalProperties": True,
}

_GROUP_WRITE_CONFIRM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


GROUP_WRITE_OPS: tuple[KeycloakOp, ...] = (
    KeycloakOp(
        op_id="keycloak.group.create",
        handler_attr="group_create",
        summary="Create a realm group with attributes (approval-gated).",
        description=(
            "POSTs ``/admin/realms/{realm}/groups`` (top-level) or "
            "``.../groups/{group-id}/children`` when ``parent_id`` is set, "
            "with a GroupRepresentation carrying ``name`` + ``attributes`` "
            "(the ``tenant_id`` / ``tenant_role`` a role group carries). "
            "Idempotent by (parent, name): a 409 already-exists returns "
            "``already_exists=True`` with the existing group's id. Returns "
            "the group id + path. requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Group name (required)."},
                "parent_id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "Parent group UUID to nest under (omit for a top-level group).",
                },
                "attributes": _ATTRIBUTES_PROP,
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        response_schema=_GROUP_WRITE_CONFIRM_SCHEMA,
        group_key="group_write",
        tags=("write", "group", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": WHEN_TO_USE_GROUP_WRITE,
            "parameter_hints": {
                "name": "The group name (e.g. a role-group name).",
                "attributes": "e.g. {'tenant_id': ['t-2'], 'tenant_role': ['admin']}.",
                "parent_id": "Parent UUID for a subgroup; omit for top-level.",
            },
            "output_shape": "``{name, parent_id, id, path, created, already_exists}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.group.update_attributes",
        handler_attr="group_update_attributes",
        summary="Merge or replace a realm group's attributes (approval-gated).",
        description=(
            "PUTs ``/admin/realms/{realm}/groups/{group-id}`` with the "
            "group's current representation and the merged (default) or "
            "replaced (``replace=true``) ``attributes``. Keys on the group "
            "UUID (``id``) or resolves ``name`` (+ optional ``parent_id``). "
            "Fixes a mis-attributed role group without recreating it. "
            "requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "Group internal UUID.",
                },
                "name": {
                    "type": "string",
                    "description": "Group name (resolved to UUID when id is absent).",
                },
                "parent_id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "Parent UUID to scope name resolution to a subgroup.",
                },
                "attributes": _ATTRIBUTES_PROP,
                "replace": {
                    "type": "boolean",
                    "description": (
                        "Default false (merge onto existing attributes). "
                        "true replaces the attribute map wholesale."
                    ),
                },
            },
            "required": ["attributes"],
            "additionalProperties": False,
        },
        response_schema=_GROUP_WRITE_CONFIRM_SCHEMA,
        group_key="group_write",
        tags=("write", "group", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": WHEN_TO_USE_GROUP_WRITE,
            "parameter_hints": {
                "id": "Group internal UUID (from keycloak.group.list).",
                "attributes": "Attribute map to merge (or replace when replace=true).",
                "replace": "Pass true to set the attributes wholesale.",
            },
            "output_shape": "``{id, name, attribute_keys, replaced}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.group.member.add",
        handler_attr="group_member_add",
        summary="Add a user to a realm group (approval-gated, privilege grant).",
        description=(
            "PUTs ``/admin/realms/{realm}/users/{user-id}/groups/{groupId}`` "
            "to add the user to the group — a tenant-access grant. Keys on "
            "the group UUID (``group_id`` or ``group_name`` + optional "
            "``parent_id``) and the user UUID (``user_id`` or ``username``). "
            "Idempotent: an existing member returns ``unchanged=true``. "
            "requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "group_id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "Group internal UUID.",
                },
                "group_name": {
                    "type": "string",
                    "description": "Group name (resolved to UUID when group_id is absent).",
                },
                "parent_id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "Parent UUID to scope group_name resolution to a subgroup.",
                },
                "user_id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "User internal UUID.",
                },
                "username": {
                    "type": "string",
                    "description": "Username (resolved to UUID when user_id is absent).",
                },
            },
            "additionalProperties": False,
        },
        response_schema=_GROUP_WRITE_CONFIRM_SCHEMA,
        group_key="group_write",
        tags=("write", "group", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": WHEN_TO_USE_GROUP_WRITE,
            "parameter_hints": {
                "group_id": "Group UUID (from keycloak.group.list).",
                "username": "Username; resolved to UUID when user_id is absent.",
            },
            "output_shape": "``{user_id, group_id, added, unchanged}``.",
        },
    ),
    KeycloakOp(
        op_id="keycloak.group.member.remove",
        handler_attr="group_member_remove",
        summary="Remove a user from a realm group (approval-gated).",
        description=(
            "DELETEs "
            "``/admin/realms/{realm}/users/{user-id}/groups/{groupId}`` to "
            "remove the user from the group. Keys on the group UUID "
            "(``group_id`` or ``group_name`` + optional ``parent_id``) and "
            "the user UUID (``user_id`` or ``username``). Idempotent: a "
            "non-member returns ``unchanged=true``. requires_approval=True."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "group_id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "Group internal UUID.",
                },
                "group_name": {
                    "type": "string",
                    "description": "Group name (resolved to UUID when group_id is absent).",
                },
                "parent_id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "Parent UUID to scope group_name resolution to a subgroup.",
                },
                "user_id": {
                    "type": "string",
                    "pattern": _UUID_PATTERN,
                    "description": "User internal UUID.",
                },
                "username": {
                    "type": "string",
                    "description": "Username (resolved to UUID when user_id is absent).",
                },
            },
            "additionalProperties": False,
        },
        response_schema=_GROUP_WRITE_CONFIRM_SCHEMA,
        group_key="group_write",
        tags=("write", "group", "keycloak"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": WHEN_TO_USE_GROUP_WRITE,
            "parameter_hints": {
                "group_id": "Group UUID (from keycloak.group.list).",
                "username": "Username; resolved to UUID when user_id is absent.",
            },
            "output_shape": "``{user_id, group_id, removed, unchanged}``.",
        },
    ),
)

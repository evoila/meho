# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Approval-gated group-lifecycle write handlers for :class:`KeycloakConnector` (#3280).

Four mutating ops that let an identity-bootstrap flow stand up a tenant's
role groups through the backplane's policy / audit / approval path instead
of shelling out to the Admin REST API out-of-band:

====================================  ===========  ===========================================
op_id                                 safety       Admin REST API
====================================  ===========  ===========================================
``keycloak.group.create``             dangerous    ``POST .../groups`` (or ``.../children``)
``keycloak.group.update_attributes``  dangerous    ``PUT .../groups/{group-id}``
``keycloak.group.member.add``         dangerous    ``PUT .../users/{user-id}/groups/{groupId}``
``keycloak.group.member.remove``      dangerous    ``DELETE .../users/{user-id}/groups/{groupId}``
====================================  ===========  ===========================================

The op-metadata table (``GROUP_WRITE_OPS``) + curated blurb live in the
sibling :mod:`~meho_backplane.connectors.keycloak.ops_write_groups_schemas`
(the ``ops_write`` / ``ops_write_schemas`` split precedent) so both modules
stay under the code-quality file-size budget.

Why the group surface is privilege-adjacent (``dangerous``)
===========================================================

The backplane derives its tenant claims (``tenant_id`` / ``tenant_role``)
from **Keycloak group membership**: a role group carries ``tenant_id`` /
``tenant_role`` as group **attributes**, which an aggregated
``oidc-usermodel-attribute-mapper`` mints into the token, and a user in no
role group fails closed with ``401 missing_tenant_claim``. So creating a
role group with those attributes and assigning membership is effectively a
tenant-access grant — the same blast-radius class as
``keycloak.role_mapping.assign``. Every op here is
``requires_approval=True`` and ``safety_level="dangerous"``.

Attributes shape
================

Keycloak stores group attributes as ``Map<String, List<String>>``. The
``attributes`` param accepts that shape verbatim, and — for ergonomics —
coerces a plain scalar value into a single-element list
(:func:`_normalise_attributes`). Attributes are **not** secret material and
classify as plain ``write`` on the broadcast feed; the shared
``redact_secret_fields`` scrub still covers a stray ``password``-keyed
attribute for defence in depth (see ``ops_write_preview``).

Idempotency (load-bearing)
==========================

* ``group.create`` is idempotent by ``(parent, name)``: an HTTP 409
  already-exists is swallowed by ``_write_admin`` and reported as
  ``already_exists=True`` with the existing group's id (resolved via
  :meth:`KeycloakConnector._find_group`) rather than creating a duplicate.
* ``group.member.add`` / ``group.member.remove`` pre-read the user's group
  membership (:meth:`KeycloakConnector._user_in_group`): an add whose user
  is already a member — or a remove whose user is not — is reported
  ``unchanged=True`` without re-issuing the mutation.

References
----------

* Task: https://github.com/evoila/meho/issues/3280
* Parent initiative: https://github.com/evoila/meho/issues/3659
* Sibling write ops (the shape this mirrors): G3.13-T4 #1406 (``ops_write``).
* Keycloak 26.3 Admin REST API:
  https://www.keycloak.org/docs-api/26.3.3/rest-api/index.html
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak._paths import (
    _GROUP_CHILDREN_PATH,
    _GROUP_PATH,
    _GROUPS_PATH,
    _USER_GROUP_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write import KeycloakUserNotFoundError
from meho_backplane.connectors.keycloak.ops_write_groups_schemas import (
    GROUP_WRITE_OPS,
    WHEN_TO_USE_GROUP_WRITE,
)
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "GROUP_WRITE_OPS",
    "WHEN_TO_USE_GROUP_WRITE",
    "KeycloakGroupNotFoundError",
    "keycloak_group_create",
    "keycloak_group_member_add",
    "keycloak_group_member_remove",
    "keycloak_group_update_attributes",
]


class KeycloakGroupNotFoundError(Exception):
    """A group write targeted a name/UUID that resolves to no group."""


def _opt_str(value: Any) -> str | None:
    """Return a trimmed non-empty string, or ``None`` for absent/blank input.

    Duplicated from ``ops_write`` (a five-line helper) to keep this module
    free of a heavier import — the same small-helper duplication
    ``ops_write_preview`` uses.
    """
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return None


def _normalise_attributes(raw: Any) -> dict[str, list[str]]:
    """Coerce a caller-supplied ``attributes`` map to Keycloak's shape.

    Keycloak group attributes are ``Map<String, List<String>>``. Each value
    is normalised to a ``list[str]``: a list is coerced element-wise to
    strings; ``None`` becomes an empty list (clears the attribute); any
    other scalar is wrapped as a single-element list — so the ergonomic
    ``{"tenant_id": "t-2"}`` and the canonical ``{"tenant_id": ["t-2"]}``
    both yield ``{"tenant_id": ["t-2"]}``. A non-dict input yields ``{}``.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        if isinstance(value, list):
            out[key] = [str(item) for item in value]
        elif value is None:
            out[key] = []
        else:
            out[key] = [str(value)]
    return out


async def _resolve_group_id(
    connector: KeycloakConnector,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    operator: Operator,
) -> str:
    """Resolve the target group's UUID from ``group_id`` or ``group_name``.

    Prefers an explicit ``group_id`` (UUID); otherwise resolves
    ``group_name`` (+ optional ``parent_id`` scoping the lookup to a
    parent's children) via :meth:`KeycloakConnector._find_group`. The op-id
    for error messages is read from ``params['_op_id']`` (set by the caller).
    """
    op_id = params["_op_id"]
    group_id = _opt_str(params.get("group_id"))
    if group_id is not None:
        return group_id
    group_name = _opt_str(params.get("group_name"))
    if group_name is None:
        raise ValueError(f"{op_id} requires either 'group_id' (UUID) or 'group_name'")
    managed_realm = resolve_realm_config(target).managed_realm
    parent_id = _opt_str(params.get("parent_id"))
    row = await connector._find_group(
        target, managed_realm, group_name, parent_id, operator=operator
    )
    resolved = row.get("id") if isinstance(row, dict) else None
    if not isinstance(resolved, str) or not resolved:
        raise KeycloakGroupNotFoundError(
            f"{op_id}: no group named {group_name!r} "
            f"({'under parent ' + parent_id if parent_id else 'top-level'}) in realm "
            f"{managed_realm!r}"
        )
    return resolved


async def _resolve_user_id(
    connector: KeycloakConnector,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    operator: Operator,
) -> str:
    """Resolve the target user's UUID from ``user_id`` or ``username``.

    Prefers an explicit ``user_id`` (UUID); otherwise resolves ``username``
    via :meth:`KeycloakConnector._find_user_uuid`. The op-id for error
    messages is read from ``params['_op_id']`` (set by the caller).
    """
    op_id = params["_op_id"]
    user_id = _opt_str(params.get("user_id"))
    if user_id is not None:
        return user_id
    username = _opt_str(params.get("username"))
    if username is None:
        raise ValueError(f"{op_id} requires either 'user_id' (UUID) or 'username'")
    managed_realm = resolve_realm_config(target).managed_realm
    resolved = await connector._find_user_uuid(target, managed_realm, username, operator=operator)
    if resolved is None:
        raise KeycloakUserNotFoundError(
            f"{op_id}: no user with username={username!r} in realm {managed_realm!r}"
        )
    return resolved


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def keycloak_group_create(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Create a realm group (``POST .../groups`` or ``.../groups/{group-id}/children``).

    Op-id: ``keycloak.group.create``. ``name`` is required; ``parent_id``
    nests the new group under an existing one; ``attributes`` sets the
    group's ``Map<String, List<String>>`` attributes (the ``tenant_id`` /
    ``tenant_role`` a role group carries). Idempotent by ``(parent, name)``:
    a 409 already-exists is reported ``already_exists=True`` with the
    existing group's id rather than creating a duplicate. Returns the group
    id + path.
    """
    realms = resolve_realm_config(target)
    name = str(params["name"]).strip()
    parent_id = _opt_str(params.get("parent_id"))
    attributes = _normalise_attributes(params.get("attributes"))
    representation: dict[str, Any] = {"name": name}
    if attributes:
        representation["attributes"] = attributes
    if parent_id:
        path = fill_path(
            _GROUP_CHILDREN_PATH,
            {"realm": realms.managed_realm, "group-id": quote_segment(parent_id)},
        )
    else:
        path = fill_path(_GROUPS_PATH, {"realm": realms.managed_realm})
    result = await self._write_admin(target, "POST", path, operator=operator, json=representation)
    # Resolve the group (fresh or pre-existing) so the caller always gets an
    # id + the canonical path — the find is authoritative and covers both a
    # 409 conflict and a create whose response omitted a usable Location.
    row = await self._find_group(target, realms.managed_realm, name, parent_id, operator=operator)
    group_id = (row.get("id") if isinstance(row, dict) else None) or result.created_uuid()
    group_path = row.get("path") if isinstance(row, dict) else None
    return {
        "name": name,
        "parent_id": parent_id,
        "id": group_id,
        "path": group_path,
        "created": not result.conflict,
        "already_exists": result.conflict,
    }


async def keycloak_group_update_attributes(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Merge or replace a group's attributes (``PUT .../groups/{group-id}``).

    Op-id: ``keycloak.group.update_attributes``. Keys on the group UUID
    (``id``) or resolves ``name`` (+ optional ``parent_id``). ``attributes``
    is merged onto the group's current attributes by default (a targeted fix
    of ``tenant_id`` / ``tenant_role``); ``replace=true`` sets them
    wholesale. The group ``name`` is preserved. Returns the id + name + the
    attribute keys now set + the replace flag (never attribute values).
    """
    realms = resolve_realm_config(target)
    # update_attributes keys on ``id``/``name`` (mirroring client.update);
    # map those onto the shared group-id resolver's ``group_id``/``group_name``.
    group_id = await _resolve_group_id(
        self,
        target,
        {
            "_op_id": "keycloak.group.update_attributes",
            "group_id": params.get("id"),
            "group_name": params.get("name"),
            "parent_id": params.get("parent_id"),
        },
        operator,
    )
    attributes = _normalise_attributes(params.get("attributes"))
    replace = bool(params.get("replace", False))
    quoted = quote_segment(group_id)
    current = await self._get_admin_json(
        target,
        fill_path(_GROUP_PATH, {"realm": realms.managed_realm, "group-id": quoted}),
        operator=operator,
    )
    current_attrs = current.get("attributes")
    base = current_attrs if isinstance(current_attrs, dict) else {}
    merged = dict(attributes) if replace else {**base, **attributes}
    group_name = current.get("name")
    await self._write_admin(
        target,
        "PUT",
        fill_path(_GROUP_PATH, {"realm": realms.managed_realm, "group-id": quoted}),
        operator=operator,
        json={"id": group_id, "name": group_name, "attributes": merged},
        idempotent_conflict=False,
    )
    return {
        "id": group_id,
        "name": group_name,
        "attribute_keys": sorted(merged.keys()),
        "replaced": replace,
    }


async def keycloak_group_member_add(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Add a user to a group (``PUT .../users/{user-id}/groups/{groupId}``).

    Op-id: ``keycloak.group.member.add``. Keys on the group UUID
    (``group_id`` or ``group_name`` + optional ``parent_id``) and the user
    UUID (``user_id`` or ``username``). Idempotent: a user already in the
    group is reported ``unchanged=True`` without re-issuing the PUT.
    """
    realms = resolve_realm_config(target)
    params = {**params, "_op_id": "keycloak.group.member.add"}
    group_id = await _resolve_group_id(self, target, params, operator)
    user_id = await _resolve_user_id(self, target, params, operator)
    already_member = await self._user_in_group(
        target, realms.managed_realm, user_id, group_id, operator=operator
    )
    if already_member:
        return {"user_id": user_id, "group_id": group_id, "added": False, "unchanged": True}
    await self._write_admin(
        target,
        "PUT",
        fill_path(
            _USER_GROUP_PATH,
            {
                "realm": realms.managed_realm,
                "user-id": quote_segment(user_id),
                "groupId": quote_segment(group_id),
            },
        ),
        operator=operator,
        idempotent_conflict=False,
    )
    return {"user_id": user_id, "group_id": group_id, "added": True, "unchanged": False}


async def keycloak_group_member_remove(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Remove a user from a group (``DELETE .../users/{user-id}/groups/{groupId}``).

    Op-id: ``keycloak.group.member.remove``. Same resolution as
    ``member.add``. Idempotent: a user not in the group is reported
    ``unchanged=True`` without issuing the DELETE.
    """
    realms = resolve_realm_config(target)
    params = {**params, "_op_id": "keycloak.group.member.remove"}
    group_id = await _resolve_group_id(self, target, params, operator)
    user_id = await _resolve_user_id(self, target, params, operator)
    if not await self._user_in_group(
        target, realms.managed_realm, user_id, group_id, operator=operator
    ):
        return {"user_id": user_id, "group_id": group_id, "removed": False, "unchanged": True}
    await self._write_admin(
        target,
        "DELETE",
        fill_path(
            _USER_GROUP_PATH,
            {
                "realm": realms.managed_realm,
                "user-id": quote_segment(user_id),
                "groupId": quote_segment(group_id),
            },
        ),
        operator=operator,
        idempotent_conflict=False,
    )
    return {"user_id": user_id, "group_id": group_id, "removed": True, "unchanged": False}

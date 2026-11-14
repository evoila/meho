# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Governed deletes of Keycloak users, groups and clients (#3944).

Each op removes one Keycloak object. All three are on the ``destructive``
risk level, so a second person must approve every call:

==========================  ==============================================
op_id                       Admin REST API
==========================  ==============================================
``keycloak.user.delete``    ``DELETE /admin/realms/{realm}/users/{id}``
``keycloak.group.delete``   ``DELETE /admin/realms/{realm}/groups/{id}``
``keycloak.client.delete``  ``DELETE /admin/realms/{realm}/clients/{id}``
==========================  ==============================================

How one delete runs
===================

1. **Find the object.** The caller passes the internal id, or a name
   (``username`` / group ``name`` / ``client_id``) that is looked up in the
   target's managed realm. A name that matches more than one object is
   refused: the caller must pass the id of the one to delete.
2. **Check the guards.** Some objects are never deleted through MEHO:

   * Keycloak's built-in clients (``BUILTIN_CLIENT_IDS``). Every realm
     needs them for login and the admin console.
   * The client and the user this MEHO target itself logs in with. Deleting
     them breaks the target. They live in the target's ``admin_realm``, so
     this guard applies when the managed realm is that realm.
   * A group that still has sub-groups, unless the caller passes
     ``delete_subgroups=true``. Keycloak deletes a group's sub-groups with it.
3. **Delete, then read again.** The handler sends the DELETE and reports
   ``deleted: true`` only after a read of the object answers 404.

Steps 1 and 2 live in
:mod:`~meho_backplane.connectors.keycloak.ops_write_delete_lookup` and run
twice. The preview builders in
:mod:`~meho_backplane.connectors.keycloak.ops_write_preview` run them before
the request waits for approval, so a refused delete never reaches an
approver. The handlers run them again when the approved call executes, which
catches a change made while the request waited (for example a new sub-group).

A user delete does not look the user up again after approval. It deletes
only the user on the approval card: the user id and the realm the card
shows. The run is refused when the card shows no user id, when the card
said the user does not exist, or when the username stored for that id is
not the one on the card (the user was renamed). A user who was deleted and
made again with the same name has a new id, so the run finds nothing to
delete.

Safe to run twice
=================

If the object is already gone (the id reads 404, or the name matches
nothing), the handler sends no DELETE and returns ``already_absent: true``.
An approved user delete whose card said the user does not exist is refused
instead: the approver saw no user to delete.
A DELETE that answers 404 (someone removed the object in the meantime) is
reported the same way.

References
----------

* Task: https://github.com/evoila/meho/issues/3944
* Governed-delete tier: ``docs/decisions/governed-delete-operations.md``.
* Keycloak 26.3 Admin REST API:
  https://www.keycloak.org/docs-api/26.3.3/rest-api/index.html
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.keycloak._paths import (
    _CLIENT_PATH,
    _GROUP_CHILDREN_PATH,
    _GROUP_MEMBERS_PATH,
    _GROUP_PATH,
    _USER_GROUPS_PATH,
    _USER_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    CLIENT_DELETE_OP,
    GROUP_DELETE_OP,
    SERVICE_ACCOUNT_PREFIX,
    USER_DELETE_OP,
    FoundObject,
    check_client_guards,
    check_group_guards,
    check_user_guards,
    find_client,
    find_group,
    find_user,
    get_or_none,
    str_field,
    subgroup_count,
)
from meho_backplane.connectors.keycloak.ops_write_user_identity import (
    card_user_id,
    check_card_username,
)
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.operations._preview import approved_preview

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "KeycloakDeleteVerificationError",
    "client_blast_radius",
    "group_blast_radius",
    "keycloak_client_delete",
    "keycloak_group_delete",
    "keycloak_user_delete",
    "user_blast_radius",
]

#: Irreversibility class on the approval card when the object exists. A
#: re-created user / group / client gets a new id, and loses its sessions,
#: role mappings, memberships and (for a client) its secret.
IRREVERSIBLE: Final = "permanent"

#: Irreversibility class when the object is already gone (nothing to delete).
ALREADY_ABSENT: Final = "none-object-already-absent"

#: The group preview counts members up to this number, then stops.
MEMBER_COUNT_CAP: Final = 1000

#: The group preview lists at most this many direct sub-groups by name.
SUBGROUP_LIST_CAP: Final = 100


class KeycloakDeleteVerificationError(RuntimeError):
    """Keycloak answered the DELETE, but the object can still be read."""


# ---------------------------------------------------------------------------
# The approval-card block (used by the preview builders)
# ---------------------------------------------------------------------------


def _blast_radius(
    found: FoundObject, obj: dict[str, Any], children: list[dict[str, Any]]
) -> dict[str, Any]:
    """Wrap the card fields in the destructive tier's ``blast_radius`` block."""
    return {
        "blast_radius": {
            "object": {
                **obj,
                "realm": found.realm,
                "id": found.object_id,
                "present": found.present,
            },
            "children": children,
            "irreversibility": IRREVERSIBLE if found.present else ALREADY_ABSENT,
        }
    }


def _user_name_fields(found: FoundObject, params: dict[str, Any]) -> dict[str, Any]:
    """The user's name for the card and the result of a user delete.

    ``username`` is the name stored in Keycloak. When the user does not exist,
    the name the caller sent is shown only as ``requested_username``.
    """
    if found.name is not None:
        return {"username": found.name}
    requested = params.get("username")
    if isinstance(requested, str) and requested.strip():
        return {"requested_username": requested.strip()}
    return {}


async def user_blast_radius(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    found: FoundObject,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Card for a user delete: user name, enabled or not, and its groups."""
    obj: dict[str, Any] = {"kind": "keycloak_user", **_user_name_fields(found, params)}
    if found.rep is not None and found.object_id is not None:
        rows = await connector._get_admin_list(
            target,
            fill_path(
                _USER_GROUPS_PATH,
                {"realm": found.realm, "user-id": quote_segment(found.object_id)},
            ),
            operator=operator,
        )
        obj["enabled"] = found.rep.get("enabled") is True
        obj["groups"] = [
            row.get("path") or row.get("name") for row in rows if isinstance(row, dict)
        ]
    return _blast_radius(found, obj, [])


def client_blast_radius(found: FoundObject) -> dict[str, Any]:
    """Card for a client delete: client id, enabled or not, public or confidential.

    Built only from named, non-secret fields; the client representation (which
    can carry the client ``secret``) is never echoed. A client with a service
    account loses that service-account user too, so it is listed as a child.
    """
    obj: dict[str, Any] = {"kind": "keycloak_client", "client_id": found.name}
    children: list[dict[str, Any]] = []
    if found.rep is not None:
        obj["enabled"] = found.rep.get("enabled") is True
        obj["client_type"] = "public" if found.rep.get("publicClient") is True else "confidential"
        if found.rep.get("serviceAccountsEnabled") is True:
            children.append(
                {
                    "kind": "service_account_user",
                    "username": f"{SERVICE_ACCOUNT_PREFIX}{found.name}".lower(),
                }
            )
    return _blast_radius(found, obj, children)


async def group_blast_radius(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    found: FoundObject,
) -> dict[str, Any]:
    """Card for a group delete: name, number of members, number of sub-groups.

    Members are counted up to :data:`MEMBER_COUNT_CAP`; ``member_count_capped``
    is true when the group has more. Members are not deleted, they only lose
    this membership. Direct sub-groups are deleted with the group, so they are
    listed as children (up to :data:`SUBGROUP_LIST_CAP`).
    """
    obj: dict[str, Any] = {"kind": "keycloak_group", "name": found.name}
    children: list[dict[str, Any]] = []
    if found.rep is not None and found.object_id is not None:
        values = {"realm": found.realm, "group-id": quote_segment(found.object_id)}
        members = await connector._get_admin_list(
            target,
            fill_path(_GROUP_MEMBERS_PATH, values),
            operator=operator,
            params={"first": "0", "max": str(MEMBER_COUNT_CAP + 1), "briefRepresentation": "true"},
        )
        sub_count = subgroup_count(found.rep)
        obj["path"] = str_field(found.rep, "path")
        obj["member_count"] = min(len(members), MEMBER_COUNT_CAP)
        obj["member_count_capped"] = len(members) > MEMBER_COUNT_CAP
        obj["subgroup_count"] = sub_count
        if sub_count != 0:
            rows = await connector._get_admin_list(
                target,
                fill_path(_GROUP_CHILDREN_PATH, values),
                operator=operator,
                params={
                    "first": "0",
                    "max": str(SUBGROUP_LIST_CAP),
                    "briefRepresentation": "true",
                },
            )
            children = [
                {"kind": "sub_group", "name": row.get("name"), "path": row.get("path")}
                for row in rows
                if isinstance(row, dict)
            ]
    return _blast_radius(found, obj, children)


# ---------------------------------------------------------------------------
# Step 3 — the handlers
# ---------------------------------------------------------------------------


async def _delete_and_verify(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    *,
    op_id: str,
    path: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Send the DELETE, then read the object again and expect 404."""
    try:
        await connector._write_admin(
            target, "DELETE", path, operator=operator, idempotent_conflict=False
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 404:
            raise
        # Removed by someone else between the lookup and the DELETE.
        return {**result, "deleted": False, "already_absent": True}
    if await get_or_none(connector, target, path, operator) is not None:
        raise KeycloakDeleteVerificationError(
            f"{op_id}: Keycloak answered the DELETE, but the object can still be "
            f"read at {path}. It was not removed."
        )
    return {**result, "deleted": True, "already_absent": False}


def _absent(result: dict[str, Any]) -> dict[str, Any]:
    return {**result, "deleted": False, "already_absent": True}


async def _approved_user(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    card: dict[str, Any],
) -> FoundObject:
    """The user on the approval card of a user delete, read again by its id.

    Raises :exc:`ValueError` when the card said the user does not exist, shows
    no user id or realm, or names another user than the one stored for the id.
    """
    block = card.get("blast_radius")
    shown = block.get("object") if isinstance(block, dict) else None
    shown = shown if isinstance(shown, dict) else {}
    if shown.get("present") is False:
        raise ValueError(
            f"{USER_DELETE_OP}: the approval card said the user does not exist, so "
            "there is no approved user to delete. Nothing was deleted. Send a new request."
        )
    uuid = card_user_id(USER_DELETE_OP, shown)
    realm = str_field(shown, "realm")
    if realm is None:
        raise ValueError(
            f"{USER_DELETE_OP}: the approval card shows no realm. Nothing was deleted. "
            "Send a new request."
        )
    realms = dataclasses.replace(resolve_realm_config(target), managed_realm=realm)
    path = fill_path(_USER_PATH, {"realm": realm, "user-id": quote_segment(uuid)})
    rep = await get_or_none(connector, target, path, operator)
    if rep is not None:
        check_card_username(USER_DELETE_OP, shown, rep)
    return FoundObject(realms=realms, object_id=uuid, name=str_field(rep, "username"), rep=rep)


async def keycloak_user_delete(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Delete one user (``DELETE .../users/{id}``). Op-id: ``keycloak.user.delete``.

    In the approved run it acts on the user on the approval card, not on a
    new lookup of the username.
    """
    card = approved_preview(USER_DELETE_OP)
    if card is None:
        found = await find_user(self, operator, target, params)
    else:
        found = await _approved_user(self, operator, target, card)
    result = {"realm": found.realm, "id": found.object_id, **_user_name_fields(found, params)}
    if not found.present or found.object_id is None:
        return _absent(result)
    await check_user_guards(self, operator, target, found)
    path = fill_path(_USER_PATH, {"realm": found.realm, "user-id": quote_segment(found.object_id)})
    return await _delete_and_verify(
        self, operator, target, op_id=USER_DELETE_OP, path=path, result=result
    )


async def keycloak_client_delete(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Delete one client (``DELETE .../clients/{id}``). Op-id: ``keycloak.client.delete``."""
    found = await find_client(self, operator, target, params)
    result = {"realm": found.realm, "id": found.object_id, "client_id": found.name}
    if not found.present or found.object_id is None:
        return _absent(result)
    await check_client_guards(self, operator, target, found)
    path = fill_path(
        _CLIENT_PATH, {"realm": found.realm, "client-uuid": quote_segment(found.object_id)}
    )
    return await _delete_and_verify(
        self, operator, target, op_id=CLIENT_DELETE_OP, path=path, result=result
    )


async def keycloak_group_delete(
    self: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Delete one group and its sub-groups (``DELETE .../groups/{id}``).

    Op-id: ``keycloak.group.delete``. A group that has sub-groups is refused
    unless ``delete_subgroups`` is true.
    """
    found = await find_group(self, operator, target, params)
    result = {
        "realm": found.realm,
        "id": found.object_id,
        "name": found.name,
        "path": str_field(found.rep, "path"),
    }
    if not found.present or found.object_id is None:
        return _absent(result)
    check_group_guards(found, params)
    path = fill_path(
        _GROUP_PATH, {"realm": found.realm, "group-id": quote_segment(found.object_id)}
    )
    return await _delete_and_verify(
        self, operator, target, op_id=GROUP_DELETE_OP, path=path, result=result
    )

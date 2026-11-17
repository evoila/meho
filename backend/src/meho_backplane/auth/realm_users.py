# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find a person in MEHO's own sign-in realm, and check their tenant.

Used when a tenant admin signs out one tool of another person
(``tools.sign_out``, :mod:`meho_backplane.connectors.tools`). The reads
go through MEHO's own Keycloak admin client
(:class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminClient`), so
they always ask the realm MEHO uses for sign-in, never a Keycloak target.

* :func:`find_realm_user` -- a person by Keycloak user id or by exact
  username. A user id is any string: a person from a linked user
  directory can have an id that is not a UUID.
* :func:`user_in_tenant` -- whether the person is in a role group of one
  tenant. A role group carries the tenant in its ``meho_tenant``
  attribute (``<tenant-uuid>:<tenant>:<Role>``) or its ``tenant_id``
  attribute. A group without them counts through its parent groups, as
  Keycloak copies a parent's attributes into the token too.

A failed read raises
:class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminError`. It never
reads as "no such person" or "not in the tenant".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakAdminError,
    KeycloakForbiddenError,
)

__all__ = [
    "RealmUser",
    "RealmUserNotFoundError",
    "find_realm_user",
    "user_in_tenant",
]

#: Groups read per page of ``GET /users/{id}/groups``.
GROUP_PAGE_SIZE: Final[int] = 100

#: The most pages :func:`user_in_tenant` reads before it gives up.
MAX_GROUP_PAGES: Final[int] = 50

#: The most parent groups :func:`user_in_tenant` reads in one check.
MAX_PARENT_READS: Final[int] = 100


@dataclass(frozen=True)
class RealmUser:
    """One person of MEHO's sign-in realm."""

    id: str
    username: str


class RealmUserNotFoundError(KeycloakAdminError):
    """No person with this id or exact username in MEHO's sign-in realm."""


def _check_status(status: int, what: str) -> None:
    if status == 403:
        raise KeycloakForbiddenError(f"Keycloak refused to read {what}: HTTP 403")
    if status != 200:
        raise KeycloakAdminError(f"Keycloak could not read {what}: HTTP {status}")


def _as_user(body: Any) -> RealmUser | None:
    if not isinstance(body, dict):
        return None
    user_id, username = body.get("id"), body.get("username")
    if isinstance(user_id, str) and user_id and isinstance(username, str) and username:
        return RealmUser(id=user_id, username=username)
    return None


async def _user_by_id(kc: KeycloakAdminClient, user_id: str) -> RealmUser | None:
    status, body = await kc.read(f"/users/{quote(user_id, safe='')}")
    if status == 404:
        return None
    _check_status(status, "the person")
    user = _as_user(body)
    if user is None:
        raise KeycloakAdminError("Keycloak answered the person with no id or username")
    return user


async def _user_by_name(kc: KeycloakAdminClient, username: str) -> RealmUser | None:
    status, body = await kc.read(
        "/users", {"username": username, "exact": "true", "briefRepresentation": "true"}
    )
    _check_status(status, "the people")
    if not isinstance(body, list):
        raise KeycloakAdminError("Keycloak answered the people with no list")
    wanted = username.casefold()
    found = [u for u in map(_as_user, body) if u is not None and u.username.casefold() == wanted]
    if len(found) > 1:
        raise KeycloakAdminError(f"Keycloak has more than one person named {username!r}")
    return found[0] if found else None


async def find_realm_user(kc: KeycloakAdminClient, user: str, *, by_id: bool = False) -> RealmUser:
    """Return the person with Keycloak user id *user*, else with username *user*.

    The username must match exactly (upper and lower case do not count).
    With *by_id*, only the user id counts. Raises
    :class:`RealmUserNotFoundError` when no person matches.
    """
    wanted = user.strip()
    if not wanted:
        raise RealmUserNotFoundError("no person given")
    found = await _user_by_id(kc, wanted)
    if found is None and not by_id:
        found = await _user_by_name(kc, wanted)
    if found is None:
        raise RealmUserNotFoundError(f"MEHO's sign-in realm has no person {wanted!r}")
    return found


def _names_tenant(attributes: Any, tenant_id: uuid.UUID) -> bool:
    """Whether a group's attributes name *tenant_id*."""
    if not isinstance(attributes, dict):
        return False
    values: list[str] = []
    for value in attributes.get("meho_tenant") or []:
        if isinstance(value, str):
            values.append(value.split(":", 1)[0])
    values.extend(v for v in attributes.get("tenant_id") or [] if isinstance(v, str))
    for value in values:
        try:
            if uuid.UUID(value.strip()) == tenant_id:
                return True
        except ValueError:
            continue
    return False


async def _user_groups(kc: KeycloakAdminClient, user_id: str) -> list[dict[str, Any]]:
    """Read every group of the person, page by page."""
    path = f"/users/{quote(user_id, safe='')}/groups"
    groups: list[dict[str, Any]] = []
    for _ in range(MAX_GROUP_PAGES):
        status, body = await kc.read(
            path,
            {
                "briefRepresentation": "false",
                "first": str(len(groups)),
                "max": str(GROUP_PAGE_SIZE),
            },
        )
        _check_status(status, "the groups of the person")
        if not isinstance(body, list):
            raise KeycloakAdminError("Keycloak answered the groups of the person with no list")
        groups.extend(g for g in body if isinstance(g, dict))
        if len(body) < GROUP_PAGE_SIZE:
            return groups
    raise KeycloakAdminError("the person is in too many groups to check")


async def user_in_tenant(kc: KeycloakAdminClient, user_id: str, tenant_id: uuid.UUID) -> bool:
    """Whether the person *user_id* is in a role group of *tenant_id*.

    Reads every group of the person, and the parent groups of a group
    that names no tenant itself.
    """
    parents: list[str] = []
    for group in await _user_groups(kc, user_id):
        if _names_tenant(group.get("attributes"), tenant_id):
            return True
        if isinstance(group.get("parentId"), str):
            parents.append(group["parentId"])
    seen: set[str] = set()
    while parents:
        parent_id = parents.pop()
        if parent_id in seen:
            continue
        if len(seen) >= MAX_PARENT_READS:
            raise KeycloakAdminError("the person's groups have too many parent groups to check")
        seen.add(parent_id)
        status, body = await kc.read(f"/groups/{quote(parent_id, safe='')}")
        _check_status(status, "a parent group of the person")
        if not isinstance(body, dict):
            raise KeycloakAdminError("Keycloak answered a parent group with no object")
        if _names_tenant(body.get("attributes"), tenant_id):
            return True
        if isinstance(body.get("parentId"), str):
            parents.append(body["parentId"])
    return False

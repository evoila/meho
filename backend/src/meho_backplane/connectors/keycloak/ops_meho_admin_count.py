# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Count MEHO's platform admins in a Keycloak realm, the way MEHO's login gives the rights.

MEHO's login reads the platform admin value with a user attribute mapper.
Keycloak takes the value like this:

1. The user's own ``platform_admin`` value, when the user has one.
2. Else the value of the user's groups. For one group, the group's own
   value wins over the value of its parent group, and so on up the chain.

The name or path of a group gives no rights. Rights from a realm role
(``JWT_PLATFORM_ADMIN_ROLE_NAME``) are not counted here; the guard refuses
those cases.

Two answers for one person (:class:`Rights`):

* ``possible``: one of the values can be ``true``. The user that a write
  changes is a platform admin when this holds.
* ``sure``: the value is ``true`` in every case: the user's own value is
  ``true``, or the user has no own value and every group of theirs with a
  value has ``true``. Only such a person counts as another platform admin.
  When two groups give different values, Keycloak picks one of them, so
  such a person is not counted.

MEHO finds the other platform admins through the groups whose value is
``true``. A person with the rights only from their own value is not looked
for, so such a person is not counted.

A :class:`Change` says what the write changes. The value of a group and the
rights of a person are read before the write (``after=False``) or as they
are after it (``after=True``). Each list is read page by page. A failed
read, or a row in an odd shape, raises :exc:`CannotCountError`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import (
    _GROUP_CHILDREN_PATH,
    _GROUP_MEMBERS_PATH,
    _GROUP_PATH,
    _GROUPS_PATH,
    _USER_GROUPS_PATH,
    _USER_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    SERVICE_ACCOUNT_PREFIX,
    get_or_none,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = ["CannotCountError", "Change", "Counter", "Rights", "own_value"]

#: Rows read per page.
_PAGE_SIZE: Final = 100
#: A list with more pages than this is a broken read.
_MAX_PAGES: Final = 1000


class CannotCountError(Exception):
    """MEHO cannot read what it needs to count the platform admins."""


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def row_id(row: dict[str, Any]) -> str:
    """The id of a Keycloak row; a row without one raises."""
    found = _text(row.get("id"))
    if found is None:
        raise CannotCountError("a row without an id")
    return found


def is_person(user: dict[str, Any]) -> bool:
    """Whether *user* is an enabled person; an incomplete user raises."""
    username = _text(user.get("username"))
    enabled = user.get("enabled")
    if username is None or not isinstance(enabled, bool):
        raise CannotCountError("a user without a user name or an enabled value")
    return enabled and not username.casefold().startswith(SERVICE_ACCOUNT_PREFIX)


def own_value(rep: dict[str, Any]) -> bool | None:
    """The platform admin value a user or group carries itself; ``None`` for no value."""
    attributes = rep.get("attributes")
    if attributes is None:
        return None
    if not isinstance(attributes, dict):
        raise CannotCountError("attributes that are not an object")
    # Imported late: the grant module imports the user lookup, which imports the guard.
    from meho_backplane.connectors.keycloak.ops_write_grant_platform import platform_flag_names

    names = platform_flag_names()
    found: set[bool] = set()
    for key, value in attributes.items():
        if not (isinstance(key, str) and key.strip().casefold() in names):
            continue
        values = value if isinstance(value, list) else [value]
        found.update(isinstance(v, str) and v.strip().casefold() == "true" for v in values)
    if len(found) > 1:
        raise CannotCountError("a platform admin value that is both true and not true")
    return found.pop() if found else None


@dataclass(frozen=True)
class Rights:
    """Whether a person can have, and is sure to have, platform admin rights."""

    possible: bool
    sure: bool


def rights(own: bool | None, group_values: Iterable[bool | None]) -> Rights:
    """The rights from the user's own value and the values of the user's groups."""
    if own is not None:
        return Rights(own, own)
    values = {value for value in group_values if value is not None}
    return Rights(True in values, values == {True})


@dataclass(frozen=True)
class Change:
    """What a write changes; every field is optional."""

    #: A user who has no rights after the write (disabled, deleted, locked out).
    user_gone: str | None = None
    #: ``(user id, group id)``: the user leaves the group.
    user_left: tuple[str, str] | None = None
    #: A group that is deleted with all groups under it.
    group_gone: str | None = None
    #: ``(group id, new own value)``: the group's own value after the write.
    group_value: tuple[str, bool | None] | None = None


@dataclass
class Counter:
    """Reads one realm for one count; keeps what it read."""

    connector: KeycloakConnector
    operator: Operator
    target: KeycloakTargetLike
    realm: str
    change: Change = field(default_factory=Change)
    #: group id -> (own value, parent id)
    _groups: dict[str, tuple[bool | None, str | None]] = field(default_factory=dict)
    _gone: set[str] = field(default_factory=set)

    async def read_list(self, path: str, **params: str) -> list[dict[str, Any]]:
        """Every row of a list, page by page; a failed or odd read raises."""
        rows: list[dict[str, Any]] = []
        for _ in range(_MAX_PAGES):
            page = await self.connector._get_admin_list(
                self.target,
                path,
                operator=self.operator,
                params={**params, "first": str(len(rows)), "max": str(_PAGE_SIZE)},
                strict=True,
            )
            if not all(isinstance(row, dict) for row in page):
                raise CannotCountError(f"GET {path} sent a row that is not an object")
            rows.extend(page)
            if len(page) < _PAGE_SIZE:
                return rows
        raise CannotCountError(f"GET {path} did not end")

    def _keep(self, group: dict[str, Any], parent: str | None) -> str:
        group_id = row_id(group)
        self._groups[group_id] = (own_value(group), parent)
        return group_id

    async def read_group(self, group_id: str) -> dict[str, Any]:
        """One group as Keycloak stores it; a group that is not found raises."""
        path = fill_path(_GROUP_PATH, {"realm": self.realm, "group-id": group_id})
        group = await get_or_none(self.connector, self.target, path, self.operator)
        if group is None:
            raise CannotCountError("a group was not found")
        parent = _text(group.get("parentId"))
        if parent is None and (_text(group.get("path")) or "/").strip("/").count("/"):
            raise CannotCountError("a group under another group without a parent id")
        self._keep(group, parent)
        return group

    async def _group(self, group_id: str) -> tuple[bool | None, str | None]:
        if group_id not in self._groups:
            await self.read_group(group_id)
        return self._groups[group_id]

    def _children(self, group_id: str) -> str:
        return fill_path(_GROUP_CHILDREN_PATH, {"realm": self.realm, "group-id": group_id})

    async def subtree(self, group_id: str) -> list[str]:
        """The group and every group under it, read page by page."""
        await self._group(group_id)
        found = [group_id]
        todo = [group_id]
        while todo:
            parent = todo.pop()
            for child in await self.read_list(self._children(parent), briefRepresentation="false"):
                child_id = self._keep(child, parent)
                if child_id in found:
                    raise CannotCountError("a group is under itself")
                found.append(child_id)
                if child.get("subGroupCount", 1) != 0:
                    todo.append(child_id)
        return found

    async def start(self) -> None:
        """Read what the change needs before the values are asked for."""
        if self.change.group_gone is not None:
            self._gone = set(await self.subtree(self.change.group_gone))

    async def value(self, group_id: str, *, after: bool) -> bool | None:
        """The platform admin value a member of the group gets from it."""
        from meho_backplane.connectors.keycloak.ops_write_grant_platform import PARENT_CAP

        seen: set[str] = set()
        current: str | None = group_id
        while current is not None:
            if current in seen or len(seen) > PARENT_CAP:
                raise CannotCountError("the parent chain of a group does not end")
            seen.add(current)
            own, parent = await self._group(current)
            if after and current in self._gone:
                return None
            if after and self.change.group_value and self.change.group_value[0] == current:
                own = self.change.group_value[1]
            if own is not None:
                return own
            current = parent
        return None

    async def user_groups(self, user_id: str) -> list[str]:
        path = fill_path(_USER_GROUPS_PATH, {"realm": self.realm, "user-id": user_id})
        return [row_id(row) for row in await self.read_list(path, briefRepresentation="true")]

    async def rights(self, user: dict[str, Any], *, after: bool) -> Rights:
        """The rights of *user* (as Keycloak stores it) before or after the write."""
        user_id = row_id(user)
        if after and self.change.user_gone == user_id:
            return Rights(False, False)
        own = own_value(user)
        groups = await self.user_groups(user_id)
        left = self.change.user_left
        if after and left is not None and left[0] == user_id:
            groups = [group for group in groups if group != left[1]]
        return rights(own, [await self.value(group, after=after) for group in groups])

    async def walk(self) -> list[str]:
        """Every group of the realm, at any level, read page by page."""
        found: set[str] = set()
        todo: list[tuple[str, str | None]] = [
            (fill_path(_GROUPS_PATH, {"realm": self.realm}), None)
        ]
        while todo:
            path, parent = todo.pop()
            for group in await self.read_list(path, briefRepresentation="false"):
                group_id = self._keep(group, parent)
                if group_id in found:
                    raise CannotCountError("a group was read twice")
                found.add(group_id)
                if group.get("subGroupCount", 1) != 0:
                    todo.append((self._children(group_id), group_id))
        return sorted(found)

    async def members(self, group_id: str) -> list[str]:
        path = fill_path(_GROUP_MEMBERS_PATH, {"realm": self.realm, "group-id": group_id})
        return [row_id(row) for row in await self.read_list(path, briefRepresentation="true")]

    async def read_user(self, user_id: str) -> dict[str, Any] | None:
        path = fill_path(_USER_PATH, {"realm": self.realm, "user-id": user_id})
        return await get_or_none(self.connector, self.target, path, self.operator)

    async def sure_admin_after(self) -> bool:
        """Whether a person is sure to have platform admin rights after the write."""
        checked: set[str] = set()
        for group_id in await self.walk():
            if await self.value(group_id, after=True) is not True:
                continue
            # Every row must have an id, also the rows after a platform admin is found.
            for member_id in await self.members(group_id):
                if member_id.casefold() in checked:
                    continue
                checked.add(member_id.casefold())
                user = await self.read_user(member_id)
                if user is None or not is_person(user):
                    continue
                if (await self.rights(user, after=True)).sure:
                    return True
        return False

    async def someone_loses(self, groups: Iterable[str]) -> bool:
        """Whether a person in *groups* has possible rights before and no sure rights after."""
        checked: set[str] = set()
        for group_id in groups:
            for member_id in await self.members(group_id):
                if member_id.casefold() in checked:
                    continue
                checked.add(member_id.casefold())
                user = await self.read_user(member_id)
                if user is None or not is_person(user):
                    continue
                if (await self.rights(user, after=False)).possible and not (
                    await self.rights(user, after=True)
                ).sure:
                    return True
        return False

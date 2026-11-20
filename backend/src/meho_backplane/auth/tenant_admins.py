# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the admins of one tenant from MEHO's own sign-in realm.

A console page about people and roles will use :func:`tenant_admin_ids`.
No other code calls it yet.

MEHO stores no list of people. :func:`tenant_admin_ids` reads them from
MEHO's own sign-in realm with the backplane's Keycloak admin client
(:class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminClient`): the
enabled members of each group that gives the ``tenant_admin`` role in the
tenant, plus the members of each group that gives ``platform_admin``.
Service accounts (logins of a client) are not counted: only a person is
an admin here. A group without these attributes takes them from its parent
group, as Keycloak does when it fills the login token. The groups and the
members are read page by page.

The answer is ``None`` ("not known") when MEHO cannot count:

* the admin client is not set up;
* a read fails, times out, is refused, or has an odd answer;
* platform admins come from a realm role (``JWT_PLATFORM_ADMIN_ROLE_NAME``),
  which the group read cannot see;
* the realm has no admin group for the tenant, or the groups have no enabled
  member (for example the people come from a company login MEHO cannot read).

An answer is kept for :data:`_KEEP_S` seconds per tenant (a failed read for
:data:`_KEEP_FAILED_S` seconds), so a caller that asks often does not read
Keycloak on every call.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import quote

import structlog

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminClient,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.auth.operator import RoleGroup, TenantRole
from meho_backplane.settings import Settings, get_settings

__all__ = [
    "tenant_admin_ids",
]

#: The whole read gives up after this many seconds.
_READ_TIMEOUT_S = 5.0

#: A count is kept this many seconds per admin URL and tenant.
_KEEP_S = 60.0

#: "Not known" is kept only this many seconds.
_KEEP_FAILED_S = 5.0

#: Groups or members read per page.
_PAGE_SIZE: Final[int] = 100

#: The most pages read for one list before the count is "not known".
_MAX_PAGES: Final[int] = 20

#: The most groups read in the whole group tree before the count is "not known".
_MAX_GROUPS: Final[int] = 1000

#: The kept answers: (admin URL, tenant) -> (when read, in monotonic seconds; the ids).
_kept: dict[tuple[str, uuid.UUID], tuple[float, frozenset[str] | None]] = {}


class _CannotCountError(Exception):
    """The read could not give a sure count; the arg says why."""


def _values(attributes: Mapping[str, Any], key: str) -> list[str]:
    raw = attributes.get(key)
    if not isinstance(raw, list):
        return []
    return [v.strip() for v in raw if isinstance(v, str)]


def _same_tenant(value: str, tenant_id: uuid.UUID) -> bool:
    try:
        return uuid.UUID(value) == tenant_id
    except ValueError:
        return False


def _gives_admin(attributes: Mapping[str, Any], tenant_id: uuid.UUID) -> bool:
    """Whether a group with these (inherited) attributes makes its members admins of the tenant."""
    if any(v.lower() == "true" for v in _values(attributes, "platform_admin")):
        return True
    for entry in _values(attributes, "meho_tenant"):
        parts = entry.split(":")
        if len(parts) == 3 and _same_tenant(parts[0], tenant_id) and parts[2] == RoleGroup.ADMINS:
            return True
    return TenantRole.TENANT_ADMIN.value in _values(attributes, "tenant_role") and any(
        _same_tenant(v, tenant_id) for v in _values(attributes, "tenant_id")
    )


def _inherit(own: Any, parent: Mapping[str, Any]) -> dict[str, Any]:
    """The group's own attributes; a missing one comes from the parent group."""
    merged = dict(parent)
    if isinstance(own, dict):
        merged.update({k: v for k, v in own.items() if isinstance(v, list) and v})
    return merged


async def _read_list(kc: KeycloakAdminClient, path: str, **params: str) -> list[dict[str, Any]]:
    """Every row of a list, page by page; a failed or odd read raises."""
    rows: list[dict[str, Any]] = []
    for _ in range(_MAX_PAGES):
        status, body = await kc.read(
            path, {**params, "first": str(len(rows)), "max": str(_PAGE_SIZE)}
        )
        if status != 200 or not isinstance(body, list):
            raise _CannotCountError(f"GET {path} answered HTTP {status}")
        rows.extend(row for row in body if isinstance(row, dict))
        if len(body) < _PAGE_SIZE:
            return rows
    raise _CannotCountError(f"GET {path} has too many pages")


async def _admin_groups(kc: KeycloakAdminClient, tenant_id: uuid.UUID) -> list[str]:
    """The ids of every group, at any level, that makes its members admins of the tenant."""
    found: list[str] = []
    seen = 0
    todo: list[tuple[str, dict[str, Any]]] = [("/groups", {})]
    while todo:
        path, parent_attributes = todo.pop()
        for group in await _read_list(kc, path, briefRepresentation="false"):
            seen += 1
            if seen > _MAX_GROUPS:
                raise _CannotCountError("the realm has too many groups")
            group_id = group.get("id")
            if not isinstance(group_id, str) or not group_id:
                raise _CannotCountError("a group has no id")
            attributes = _inherit(group.get("attributes"), parent_attributes)
            if _gives_admin(attributes, tenant_id):
                found.append(group_id)
            if group.get("subGroupCount", 1) != 0:
                todo.append((f"/groups/{quote(group_id, safe='')}/children", attributes))
    return found


def _is_service_account(user: Mapping[str, Any], prefix: str) -> bool:
    """Whether *user* is the login of a client (a service account), not a person.

    Only a person may approve a change of rights, so a service account in an
    admin group is not counted. Keycloak names it ``service-account-<client>``;
    the prefix is ``JWT_SERVICE_ACCOUNT_USERNAME_PREFIX``, as at sign-in.
    """
    username = user.get("username")
    return bool(prefix) and isinstance(username, str) and username.startswith(prefix)


async def _admins(
    kc: KeycloakAdminClient, tenant_id: uuid.UUID, service_account_prefix: str
) -> frozenset[str]:
    ids: set[str] = set()
    for group_id in await _admin_groups(kc, tenant_id):
        path = f"/groups/{quote(group_id, safe='')}/members"
        for user in await _read_list(kc, path, briefRepresentation="true"):
            user_id = user.get("id")
            if (
                isinstance(user_id, str)
                and user_id
                and user.get("enabled") is not False
                and not _is_service_account(user, service_account_prefix)
            ):
                ids.add(user_id.casefold())
    return frozenset(ids)


async def _read(settings: Settings, tenant_id: uuid.UUID) -> frozenset[str] | None:
    """Read the admins of the tenant; ``None`` when MEHO cannot count them. Never raises."""
    if settings.jwt_platform_admin_role_name:
        return None
    try:
        kc = KeycloakAdminClient.from_settings(settings)
    except KeycloakAdminNotConfiguredError:
        return None
    try:
        async with asyncio.timeout(_READ_TIMEOUT_S), kc:
            ids = await _admins(kc, tenant_id, settings.jwt_service_account_username_prefix)
    except Exception as exc:
        structlog.get_logger(__name__).info(
            "tenant_admin_count_unknown", error_class=type(exc).__name__
        )
        return None
    return ids or None


async def tenant_admin_ids(
    tenant_id: uuid.UUID, settings: Settings | None = None
) -> frozenset[str] | None:
    """The Keycloak user ids (lower case) of the tenant's admins, or ``None`` when not known."""
    settings = settings or get_settings()
    key = (settings.keycloak_admin_url, tenant_id)
    kept = _kept.get(key)
    if kept is not None:
        keep = _KEEP_S if kept[1] is not None else _KEEP_FAILED_S
        if time.monotonic() - kept[0] < keep:
            return kept[1]
    ids = await _read(settings, tenant_id)
    _kept[key] = (time.monotonic(), ids)
    return ids

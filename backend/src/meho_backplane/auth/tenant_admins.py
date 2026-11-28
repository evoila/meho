# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Count the admins of one tenant, for the note on a change of rights.

A change of rights waits for a platform admin when it is in MEHO's own
login realm, in ``master`` or in the target's admin realm, or when MEHO could
not make sure that it is not (the approval card's ``login_realm`` fact is
true).
The person who asked approves it only when they are a platform admin and
asked for it in the web console
(:func:`~meho_backplane.operations.grant_approval.check_rights_change_approval`).
So when the person who asked is the only admin of the tenant, nobody else in
the tenant can approve it. In every other realm, the person who asked
approves the change in the web console, so it needs no other admin.

In MEHO's own login realm, in ``master`` and in the target's admin realm,
only a platform admin approves a change of rights. So the note and the
doctor line count the platform admins on their own
(:func:`platform_admin_ids`), next to all admins (:func:`tenant_admin_ids`).

MEHO stores no list of people. :func:`tenant_admin_ids` reads them from
MEHO's own sign-in realm with the backplane's Keycloak admin client
(:class:`~meho_backplane.auth.keycloak_admin.KeycloakAdminClient`): the
enabled members of each group that gives the ``tenant_admin`` role in the
tenant, plus the members of each group that gives ``platform_admin``.
Service accounts (logins of a client) are not counted: only a person may
approve a change of rights. A
group without these attributes takes them from its parent group, as
Keycloak does when it fills the login token. The groups and the members are
read page by page.

The answer is ``None`` ("not known") when MEHO cannot count:

* the admin client is not set up;
* a read fails, times out, is refused, or has an odd answer (also a group
  or a group member with no id);
* platform admins come from a realm role (``JWT_PLATFORM_ADMIN_ROLE_NAME``),
  which the group read cannot see;
* the realm has no admin group for the tenant, or the groups have no enabled
  member (for example the people come from a company login MEHO cannot read).

"Not known" never gives a warning. Only a count MEHO is sure of does:

* :func:`only_admin_note` at request time. The caller
  (:func:`~meho_backplane.operations.approval_queue.rights_change_note`)
  asks for it for every waiting change of rights. A sure count of zero
  gives the "no admin" note for each of them. The note that the person who
  asked is the only admin shows only for a change in MEHO's own login
  realm, in ``master`` or in the target's admin realm, or where MEHO could
  not make sure that it is not;
* the doctor line of ``meho doctor``
  (``approvals.rights_change_platform_admins`` in ``/api/v1/health``), which
  warns when the tenant has no platform admin or only one, because some
  changes of rights in that realm need a platform admin other than the
  person who asked.

An answer is kept for :data:`_KEEP_S` seconds per tenant (a failed read for
:data:`_KEEP_FAILED_S` seconds), so the health answer, which is called
often, does not read Keycloak on every call.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
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
    "LOGIN_REALM_APPROVER",
    "LOGIN_REALM_APPROVER_BASE",
    "NO_ADMIN",
    "ONLY_ONE_ADMIN",
    "RIGHTS_CHANGE_GUIDE_URL",
    "admin_count",
    "only_admin_note",
    "platform_admin_count",
    "platform_admin_ids",
    "tenant_admin_ids",
]

#: The first part of the note, for a change that the person who asked can
#: never approve (it can change the approval sign-in, or it sets the password
#: of the asker's own login).
LOGIN_REALM_APPROVER_BASE: Final[str] = (
    "A platform admin must approve this change. "
    "It is in MEHO's own login realm, in `master` or in the target's admin realm, "
    "or MEHO could not make sure that it is not."
)

#: Who approves a waiting change of rights in MEHO's own login realm, in
#: ``master`` or in the target's admin realm, or where MEHO could not make
#: sure that it is not: the first part of the note. An answer about an approved request
#: leaves it out.
LOGIN_REALM_APPROVER: Final[str] = (
    f"{LOGIN_REALM_APPROVER_BASE} "
    "You can approve it yourself only if you are a platform admin and asked for it "
    "in the web console."
)

#: The sentence a person reads when MEHO found no other platform admin in the tenant.
ONLY_ONE_ADMIN: Final[str] = "MEHO found no other platform admin in this tenant."

#: The sentence a person reads when the tenant has no admin who could approve at all.
NO_ADMIN: Final[str] = "This tenant has no admin."

#: The guide part that explains who approves a change of rights.
RIGHTS_CHANGE_GUIDE_URL: Final[str] = (
    "https://evoila.github.io/meho/latest/guides/approvals-and-break-glass/"
    "#changes-of-rights-need-another-admin"
)

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


@dataclass(frozen=True)
class _Admins:
    """The admins of one tenant: everyone, and the platform admins among them."""

    everyone: frozenset[str]
    platform: frozenset[str]


#: The kept answers: (admin URL, tenant) -> (when read, in monotonic seconds; the admins).
_kept: dict[tuple[str, uuid.UUID], tuple[float, _Admins | None]] = {}


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


def _gives_platform_admin(attributes: Mapping[str, Any]) -> bool:
    """Whether a group with these (inherited) attributes makes its members platform admins."""
    return any(v.lower() == "true" for v in _values(attributes, "platform_admin"))


def _gives_admin(attributes: Mapping[str, Any], tenant_id: uuid.UUID) -> bool:
    """Whether a group with these (inherited) attributes makes its members admins of the tenant."""
    if _gives_platform_admin(attributes):
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


async def _admin_groups(kc: KeycloakAdminClient, tenant_id: uuid.UUID) -> list[tuple[str, bool]]:
    """Every group, at any level, that makes its members admins of the tenant.

    Each row is the group id and whether the group gives platform admin rights.
    """
    found: list[tuple[str, bool]] = []
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
                found.append((group_id, _gives_platform_admin(attributes)))
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
) -> _Admins:
    ids: set[str] = set()
    platform: set[str] = set()
    for group_id, gives_platform in await _admin_groups(kc, tenant_id):
        path = f"/groups/{quote(group_id, safe='')}/members"
        for user in await _read_list(kc, path, briefRepresentation="true"):
            user_id = user.get("id")
            if not isinstance(user_id, str) or not user_id:
                raise _CannotCountError("a group member has no id")
            if user.get("enabled") is not False and not _is_service_account(
                user, service_account_prefix
            ):
                ids.add(user_id.casefold())
                if gives_platform:
                    platform.add(user_id.casefold())
    return _Admins(frozenset(ids), frozenset(platform))


async def _read(settings: Settings, tenant_id: uuid.UUID) -> _Admins | None:
    """Read the admins of the tenant; ``None`` when MEHO cannot count them. Never raises."""
    if settings.jwt_platform_admin_role_name:
        return None
    try:
        kc = KeycloakAdminClient.from_settings(settings)
    except KeycloakAdminNotConfiguredError:
        return None
    try:
        async with asyncio.timeout(_READ_TIMEOUT_S), kc:
            admins = await _admins(kc, tenant_id, settings.jwt_service_account_username_prefix)
    except Exception as exc:
        structlog.get_logger(__name__).info(
            "tenant_admin_count_unknown", error_class=type(exc).__name__
        )
        return None
    return admins if admins.everyone else None


async def _tenant_admins(tenant_id: uuid.UUID, settings: Settings | None = None) -> _Admins | None:
    settings = settings or get_settings()
    key = (settings.keycloak_admin_url, tenant_id)
    kept = _kept.get(key)
    if kept is not None:
        keep = _KEEP_S if kept[1] is not None else _KEEP_FAILED_S
        if time.monotonic() - kept[0] < keep:
            return kept[1]
    admins = await _read(settings, tenant_id)
    _kept[key] = (time.monotonic(), admins)
    return admins


async def tenant_admin_ids(
    tenant_id: uuid.UUID, settings: Settings | None = None
) -> frozenset[str] | None:
    """The Keycloak user ids (lower case) of the tenant's admins, or ``None`` when not known.

    Tenant admins and platform admins together.
    """
    admins = await _tenant_admins(tenant_id, settings)
    return None if admins is None else admins.everyone


async def platform_admin_ids(
    tenant_id: uuid.UUID, settings: Settings | None = None
) -> frozenset[str] | None:
    """The user ids (lower case) of the tenant's platform admins, or ``None`` when not known."""
    admins = await _tenant_admins(tenant_id, settings)
    return None if admins is None else admins.platform


async def admin_count(tenant_id: uuid.UUID) -> int | None:
    """How many admins the tenant has (tenant and platform admins); ``None`` when not known."""
    ids = await tenant_admin_ids(tenant_id)
    return None if ids is None else len(ids)


async def platform_admin_count(tenant_id: uuid.UUID) -> int | None:
    """How many platform admins the tenant has; ``None`` when not known."""
    ids = await platform_admin_ids(tenant_id)
    return None if ids is None else len(ids)


async def only_admin_note(
    tenant_id: uuid.UUID,
    requester_sub: str,
    *,
    login_realm: bool = True,
    asker_never_approves: bool = False,
) -> str | None:
    """The note for a waiting change of rights no other admin could approve, else ``None``.

    The caller asks for every waiting change of rights. *login_realm* is
    true when a platform admin must approve it: it is in MEHO's own login
    realm, in ``master`` or in the target's admin realm, or MEHO could not
    make sure that it is not.

    * No admin at all: :data:`NO_ADMIN`, for every change of rights. It has
      no sentence about who must approve, because one more admin alone
      would not be enough.
    * *login_realm* is true and MEHO finds no platform admin in the tenant
      other than the requester: :data:`LOGIN_REALM_APPROVER` and
      :data:`ONLY_ONE_ADMIN`.
    * With *asker_never_approves* the note leaves out the sentence about
      approving it yourself: the person who asked is always refused.
    * ``None`` when another platform admin is found, when *login_realm* is
      false, and when the count is not known: then MEHO says nothing new.
    """
    admins = await _tenant_admins(tenant_id)
    if admins is None:
        return None
    if not admins.everyone:
        return f"{NO_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"
    if not login_realm or admins.platform - {requester_sub.casefold()}:
        return None
    who = LOGIN_REALM_APPROVER_BASE if asker_never_approves else LOGIN_REALM_APPROVER
    return f"{who} {ONLY_ONE_ADMIN} See {RIGHTS_CHANGE_GUIDE_URL}"

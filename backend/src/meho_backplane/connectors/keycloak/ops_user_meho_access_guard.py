# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep Keycloak user writes from taking away MEHO's own access.

In MEHO's own login realm (:func:`is_meho_login_realm`), MEHO refuses a
user write when it hits one of these users:

1. **A login MEHO itself uses.** These are the service-account users of
   ``KEYCLOAK_ADMIN_CLIENT_ID`` and ``CHECK_RUNNER_CLIENT_ID``. Every write
   that can lock a login out or take its rights away is refused
   (:data:`SERVICE_LOGIN_WRITES`).
2. **The last active platform admin.** A write that disables, deletes or
   locks out a platform admin (:data:`LOCK_OUT_WRITES`), or removes them
   from a group that gives platform admin rights
   (``keycloak.group.member.remove``), is refused when no other active
   platform admin is left. MEHO counts the others when the check runs.
3. **The account of the person or login that asks.** The lock-out writes
   (:data:`LOCK_OUT_WRITES`) are refused on that account. When an approved
   request runs, the account of the person who runs it counts too.

The check runs before the request waits for approval and again when the
call runs. So no approval and no standing grant lets such a write through:
a call that a grant runs at once still meets the run-time check.

On any doubt MEHO refuses: when it cannot read Keycloak, cannot count the
platform admins, or cannot tell which realm its own login uses. Platform
admin rights that come from a realm role (``JWT_PLATFORM_ADMIN_ROLE_NAME``)
cannot be counted, so with that setting a lock-out write on a platform
admin is always refused.

Who counts as a platform admin (the same rules as MEHO's login):

* the user's own ``platform_admin`` attribute is ``true``;
* the user is in a group that gives platform admin rights: the platform
  admin group or a group under it, or a group whose own or parent's
  ``platform_admin`` attribute is ``true``
  (:mod:`~meho_backplane.connectors.keycloak.ops_write_grant_platform`);
* with ``JWT_PLATFORM_ADMIN_ROLE_NAME`` set, the user has that realm role.

"Active" means the user is enabled and is not a client's service-account
user. When MEHO counts the other platform admins, it counts the enabled
people in the groups that give platform admin rights.

Each refusal is a :exc:`MehoAccessRefusedError`. Its message names no user,
so the same text is the reason before approval and the error of the run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import (
    _GROUP_CHILDREN_PATH,
    _GROUP_MEMBERS_PATH,
    _GROUPS_PATH,
    _USER_GROUPS_PATH,
    _USER_PATH,
    _USER_ROLE_MAPPINGS_REALM_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import (
    SERVICE_ACCOUNT_PREFIX,
    KeycloakDeleteRefusedError,
    get_or_none,
)
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.operations.secondary_target_policy import approved_requester_sub
from meho_backplane.settings import get_settings
from meho_backplane.targets.keycloak_realm_warnings import backplane_login_realms

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "LOCK_OUT_WRITES",
    "SERVICE_LOGIN_WRITES",
    "MehoAccessRefusedError",
    "is_meho_login_realm",
    "meho_access_park_reason",
    "meho_service_login_names",
    "refuse_meho_access_loss",
]

_MEMBER_REMOVE_OP: Final = "keycloak.group.member.remove"
_USER_UPDATE_OP: Final = "keycloak.user.update"
_SECRET_MOVE_OP: Final = "secret.move"

#: The writes that disable, delete or lock out a user, and the words that
#: finish the sentence "<words> ...".
LOCK_OUT_WRITES: Final[dict[str, str]] = {
    "keycloak.user.disable": "disabling it",
    "keycloak.user.delete": "deleting it",
    "keycloak.user.required_actions.update": "changing its required actions",
    "keycloak.user.reset_password": "resetting its password",
    "keycloak.user.credential.delete": "deleting one of its credentials",
    _SECRET_MOVE_OP: "setting its password",
}

#: The writes refused on a login MEHO itself uses.
SERVICE_LOGIN_WRITES: Final[dict[str, str]] = {
    **LOCK_OUT_WRITES,
    _MEMBER_REMOVE_OP: "removing it from a group",
    "keycloak.role_mapping.client.remove": "removing one of its roles",
    _USER_UPDATE_OP: "changing its user name or email",
}

#: ``keycloak.user.update`` fields a login signs in with.
_SIGN_IN_FIELDS: Final = ("username", "email")

#: The whole count gives up after this many seconds.
_COUNT_TIMEOUT_S: Final = 10.0
#: Rows read per page.
_PAGE_SIZE: Final = 100
#: The most pages read for one list before the count is "not known".
_MAX_PAGES: Final = 20
#: The most groups read in the whole group tree before the count is "not known".
_MAX_GROUPS: Final = 1000

_SERVICE_LOGIN: Final = "meho_service_login"
_LAST_PLATFORM_ADMIN: Final = "last_platform_admin"
_OWN_ACCOUNT: Final = "own_account"
_NOT_CHECKED: Final = "meho_access_not_checked"


class MehoAccessRefusedError(KeycloakDeleteRefusedError):
    """A user write that would take away MEHO's own access was refused.

    ``code`` is one of ``meho_service_login``, ``last_platform_admin``,
    ``own_account`` or ``meho_access_not_checked``. ``reason`` is the plain
    text without the op id and code; it names no user.
    """

    def __init__(self, op_id: str, code: str, reason: str) -> None:
        super().__init__(op_id, code, reason)
        self.reason = f"{op_id}: {reason}"


@dataclass(frozen=True)
class _Reader:
    """The target and the caller that read a group."""

    target: Any
    operator: Operator


class _CannotCountError(Exception):
    """MEHO cannot tell whether the write leaves another platform admin."""


class _RealmRoleError(_CannotCountError):
    """Platform admin rights come from a realm role, which MEHO cannot count."""


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def is_meho_login_realm(realm: str) -> bool:
    """Whether *realm* is a realm MEHO's own login uses.

    ``True`` too when MEHO cannot read its settings or finds no realm in
    them: then any realm may be the login realm.
    """
    try:
        realms = backplane_login_realms()
    except Exception:
        return True
    if not realms:
        return True
    return realm.casefold() in {name.casefold() for name in realms}


def meho_service_login_names() -> frozenset[str]:
    """The user names (lower case) of the service logins MEHO itself uses."""
    settings = get_settings()
    names = set()
    for client_id in (settings.keycloak_admin_client_id, settings.check_runner_client_id):
        client = _text(client_id)
        if client is not None:
            names.add(f"{SERVICE_ACCOUNT_PREFIX}{client}".casefold())
    return frozenset(names)


def _service_login_action(op_id: str, params: dict[str, Any]) -> str | None:
    action = SERVICE_LOGIN_WRITES.get(op_id)
    if action is None or op_id != _USER_UPDATE_OP:
        return action
    changes = params.get("changes")
    if isinstance(changes, dict) and any(field in changes for field in _SIGN_IN_FIELDS):
        return action
    return None


def _is_service_login(user: dict[str, Any]) -> bool:
    names = meho_service_login_names()
    username = (_text(user.get("username")) or "").casefold()
    if username and username in names:
        return True
    linked = _text(user.get("serviceAccountClientId"))
    return linked is not None and f"{SERVICE_ACCOUNT_PREFIX}{linked}".casefold() in names


def _own_ids(operator: Operator) -> tuple[set[str], set[str]]:
    """The user ids and user names of the caller and, in an approved run, the requester."""
    ids = {operator.sub.casefold()}
    requester = approved_requester_sub()
    if requester:
        ids.add(requester.casefold())
    names = set()
    if operator.client_id:
        names.add(f"{SERVICE_ACCOUNT_PREFIX}{operator.client_id}".casefold())
    return ids, names


def _is_own_account(user: dict[str, Any], operator: Operator) -> bool:
    ids, names = _own_ids(operator)
    user_id = (_text(user.get("id")) or "").casefold()
    username = (_text(user.get("username")) or "").casefold()
    return bool(user_id and user_id in ids) or bool(username and username in names)


def _is_person(user: dict[str, Any]) -> bool:
    username = _text(user.get("username")) or ""
    return user.get("enabled") is not False and not username.casefold().startswith(
        SERVICE_ACCOUNT_PREFIX
    )


async def _read_list(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    path: str,
    **params: str,
) -> list[dict[str, Any]]:
    """Every row of a list, page by page; a failed or odd read raises."""
    rows: list[dict[str, Any]] = []
    for _ in range(_MAX_PAGES):
        page = await connector._get_admin_list(
            target,
            path,
            operator=operator,
            params={**params, "first": str(len(rows)), "max": str(_PAGE_SIZE)},
            strict=True,
        )
        rows.extend(row for row in page if isinstance(row, dict))
        if len(page) < _PAGE_SIZE:
            return rows
    raise _CannotCountError(f"GET {path} has too many pages")


async def _platform_groups(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, realm: str
) -> set[str]:
    """The ids of every group, at any level, that gives platform admin rights."""
    # Imported late: this module imports the user lookup, which imports this module.
    from meho_backplane.connectors.keycloak.ops_write_grant_platform import (
        group_marks_platform_admin,
    )

    found: set[str] = set()
    seen = 0
    todo: list[tuple[str, bool]] = [(fill_path(_GROUPS_PATH, {"realm": realm}), False)]
    while todo:
        path, parent_gives = todo.pop()
        for group in await _read_list(
            connector, operator, target, path, briefRepresentation="false"
        ):
            seen += 1
            if seen > _MAX_GROUPS:
                raise _CannotCountError("the realm has too many groups")
            group_id = _text(group.get("id"))
            if group_id is None:
                raise _CannotCountError("a group has no id")
            gives = parent_gives or group_marks_platform_admin(group)
            if gives:
                found.add(group_id)
            if group.get("subGroupCount", 1) != 0:
                children = fill_path(_GROUP_CHILDREN_PATH, {"realm": realm, "group-id": group_id})
                todo.append((children, gives))
    return found


async def _other_platform_admins(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    groups: set[str],
    user_id: str,
) -> bool:
    """Whether an active person other than *user_id* is in one of *groups*."""
    for group_id in sorted(groups):
        path = fill_path(_GROUP_MEMBERS_PATH, {"realm": realm, "group-id": group_id})
        for member in await _read_list(
            connector, operator, target, path, briefRepresentation="true"
        ):
            member_id = (_text(member.get("id")) or "").casefold()
            if member_id and member_id != user_id.casefold() and _is_person(member):
                return True
    return False


async def _removed_group_id(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> str:
    # Imported late: ``ops_write_groups`` imports the user lookup, which imports this module.
    from meho_backplane.connectors.keycloak.ops_write_groups import _resolve_group_id

    return await _resolve_group_id(
        connector, target, {**params, "_op_id": _MEMBER_REMOVE_OP}, operator
    )


async def _has_realm_role(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    user_id: str,
    roles: set[str],
) -> bool:
    """Whether the user has one of *roles*: direct, from a group, or as part of another role."""
    path = fill_path(_USER_ROLE_MAPPINGS_REALM_PATH, {"realm": realm, "user-id": user_id})
    rows = await connector._get_admin_list(
        target, f"{path}/composite", operator=operator, strict=True
    )
    names = {_text(row.get("name")) if isinstance(row, dict) else None for row in rows}
    if None in names:
        raise _CannotCountError("a role without a name")
    return bool(names & roles)


async def _loses_last_platform_admin(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    user: dict[str, Any],
    *,
    op_id: str,
    params: dict[str, Any],
) -> bool:
    """Whether the write takes the last active platform admin away; raises when not known."""
    # Imported late: these modules import the user lookup, which imports this module.
    from meho_backplane.connectors.keycloak.ops_write_effective_roles import platform_role_names
    from meho_backplane.connectors.keycloak.ops_write_grant_platform import (
        group_gives_platform_admin,
        user_flag_marks_platform_admin,
    )

    user_id = _text(user.get("id"))
    if user_id is None:
        raise _CannotCountError("the user has no id")
    removed: str | None = None
    if op_id == _MEMBER_REMOVE_OP:
        removed = await _removed_group_id(connector, operator, target, params)
        gives = await group_gives_platform_admin(
            connector, _Reader(target=target, operator=operator), removed
        )
        if gives is None:
            raise _CannotCountError("MEHO cannot read the group")
        if not gives:
            return False
    roles = platform_role_names()
    if roles is None:
        raise _CannotCountError("MEHO cannot read its settings")
    groups = await _platform_groups(connector, operator, target, realm)
    user_groups_path = fill_path(_USER_GROUPS_PATH, {"realm": realm, "user-id": user_id})
    user_groups = {
        gid
        for row in await _read_list(
            connector, operator, target, user_groups_path, briefRepresentation="true"
        )
        if (gid := _text(row.get("id"))) is not None
    }
    flag = user_flag_marks_platform_admin(user)
    has_role = bool(roles) and await _has_realm_role(
        connector, operator, target, realm, user_id, roles
    )
    if not (flag or has_role or user_groups & groups):
        return False
    if roles:
        # A realm role can give the rights in ways MEHO cannot count.
        raise _RealmRoleError("platform admin rights come from a realm role")
    if removed is not None and (flag or (user_groups - {removed}) & groups):
        return False
    return not await _other_platform_admins(connector, operator, target, realm, groups, user_id)


def _refused(op_id: str, code: str, reason: str) -> MehoAccessRefusedError:
    return MehoAccessRefusedError(op_id, code, reason)


async def refuse_meho_access_loss(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    user: dict[str, Any],
    *,
    op_id: str,
    params: dict[str, Any],
    realm: str | None = None,
) -> None:
    """Raise :exc:`MehoAccessRefusedError` when the write on *user* takes MEHO's access away.

    *user* is the user as Keycloak stores it. *realm* is the realm of the
    write; it defaults to the target's managed realm.
    """
    realm = realm or resolve_realm_config(target).managed_realm
    if not is_meho_login_realm(realm):
        return
    service_action = _service_login_action(op_id, params)
    if service_action is not None and _is_service_login(user):
        raise _refused(
            op_id,
            _SERVICE_LOGIN,
            f"this user is a login that MEHO itself uses; {service_action} can stop MEHO "
            "from working. Nothing was changed. No approval and no grant allows this write. "
            "To replace this login, set up the new login in MEHO's settings first; a Keycloak "
            "administrator then changes the old one in the Keycloak admin console.",
        )
    action = LOCK_OUT_WRITES.get(op_id)
    if action is not None and _is_own_account(user, operator):
        raise _refused(
            op_id,
            _OWN_ACCOUNT,
            f"this user is the account that asked for this write or runs it; {action} can "
            "lock that account out of MEHO. Nothing was changed. Ask another admin to send "
            "the request.",
        )
    if action is None and op_id != _MEMBER_REMOVE_OP:
        return
    words = action or "removing it from this group"
    if not _is_person(user):
        return
    try:
        async with asyncio.timeout(_COUNT_TIMEOUT_S):
            last = await _loses_last_platform_admin(
                connector, operator, target, realm, user, op_id=op_id, params=params
            )
    except _RealmRoleError as exc:
        raise _refused(
            op_id,
            _NOT_CHECKED,
            "platform admin rights come from a realm role here, so MEHO cannot count "
            "the platform admins. Nothing was changed. A Keycloak administrator makes "
            "this change in the Keycloak admin console.",
        ) from exc
    except Exception as exc:
        raise _refused(
            op_id,
            _NOT_CHECKED,
            "MEHO could not read Keycloak to check that this write leaves another "
            f"platform admin ({type(exc).__name__}). Nothing was changed. Send the "
            "request again when MEHO can read Keycloak.",
        ) from exc
    if last:
        raise _refused(
            op_id,
            _LAST_PLATFORM_ADMIN,
            f"this user is the last active platform admin of MEHO; {words} leaves MEHO "
            "with no platform admin. Nothing was changed. First give a second person "
            "platform admin rights, then send the request again.",
        )


async def _park_user(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    realm: str,
    id_key: str,
) -> dict[str, Any] | None:
    uuid = _text(params.get(id_key))
    if uuid is None:
        username = _text(params.get("username"))
        if username is None:
            return None
        uuid = await connector._find_user_uuid(target, realm, username, operator=operator)
        if uuid is None:
            return None
    path = fill_path(_USER_PATH, {"realm": realm, "user-id": uuid})
    return await get_or_none(connector, target, path, operator)


async def meho_access_park_reason(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
    *,
    op_id: str,
    id_key: str = "id",
) -> str | None:
    """The check before approval: the refusal reason, or ``None``.

    Reads the user named by *id_key* or ``username``. A user that does not
    exist, or cannot be read, is let through: the run then fails with "not
    found", or is refused because the card shows no stored user.
    """
    if op_id not in SERVICE_LOGIN_WRITES:
        return None
    realm = resolve_realm_config(target).managed_realm
    if not is_meho_login_realm(realm):
        return None
    try:
        user = await _park_user(connector, operator, target, params, realm, id_key)
    except Exception:
        # The card then shows that MEHO could not read the user, and the run is refused.
        return None
    if user is None:
        return None
    try:
        await refuse_meho_access_loss(
            connector, operator, target, user, op_id=op_id, params=params, realm=realm
        )
    except MehoAccessRefusedError as exc:
        return exc.reason
    return None

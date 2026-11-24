# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep Keycloak writes from taking away MEHO's own access.

In MEHO's own login realm (:func:`is_meho_login_realm`), MEHO refuses a
user write when it hits one of these users:

1. **A login MEHO itself uses.** These are the service-account users of
   ``KEYCLOAK_ADMIN_CLIENT_ID`` and ``CHECK_RUNNER_CLIENT_ID``. Every write
   that can lock a login out or take its rights away is refused
   (:data:`SERVICE_LOGIN_WRITES`).
2. **The last active platform admin.** A write that disables, deletes or
   locks out a platform admin (:data:`LOCK_OUT_WRITES`), or removes them
   from a group (``keycloak.group.member.remove``), is refused when after
   the write no other active platform admin is left. The group writes
   ``keycloak.group.delete`` and ``keycloak.group.update_attributes``
   follow the same rule
   (:mod:`~meho_backplane.connectors.keycloak.ops_group_meho_access_guard`).
3. **The account of the person or login that asks.** The lock-out writes
   (:data:`LOCK_OUT_WRITES`) are refused on that account. When an approved
   request runs, the account of the person who runs it counts too.

The check runs before the request waits for approval and again when the
call runs. So no approval and no standing grant lets such a write through:
a call that a grant runs at once still meets the run-time check. When the
call runs, MEHO holds a lock for the login realm from the count until the
write is done (:mod:`~meho_backplane.connectors.keycloak.ops_meho_access_lock`),
so two such writes at the same moment cannot both pass.

Who counts as a platform admin follows MEHO's login: the user's own
``platform_admin`` value, else the value of the user's groups, where a
group's own value wins over its parent's
(:mod:`~meho_backplane.connectors.keycloak.ops_meho_admin_count`). The name
of a group gives no rights. "Active" means the user is enabled and is not a
client's service-account user.

On any doubt MEHO refuses: when it cannot read Keycloak, cannot count the
platform admins, cannot take the lock, or cannot tell which realm its own
login uses. Platform admin rights that come from a realm role
(``JWT_PLATFORM_ADMIN_ROLE_NAME``) cannot be counted, so with that setting
such a write on a platform admin is always refused.

Each refusal is a :exc:`MehoAccessRefusedError`. Its message says what was
refused and why, and names no user, so the same text is the reason before
approval and the error of the run. It gives no advice to change rights.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import (
    _GROUP_REALM_COMPOSITE_PATH,
    _USER_PATH,
    _USER_ROLE_MAPPINGS_REALM_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_meho_access_lock import (
    MehoAccessScopeError,
    confirm_meho_access_lock,
    hold_meho_access_lock,
    meho_access_check_only,
)
from meho_backplane.connectors.keycloak.ops_meho_admin_count import (
    CannotCountError,
    Change,
    Counter,
    is_person,
    row_id,
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
    "NO_APPROVAL",
    "SERVICE_LOGIN_WRITES",
    "MehoAccessRefusedError",
    "RealmRoleError",
    "has_platform_role",
    "is_meho_login_realm",
    "meho_access_park_reason",
    "meho_service_login_names",
    "refuse_last_admin_loss",
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
_COUNT_TIMEOUT_S: Final = 30.0
_SERVICE_LOGIN: Final = "meho_service_login"
_LAST_PLATFORM_ADMIN: Final = "last_platform_admin"
_OWN_ACCOUNT: Final = "own_account"
_NOT_CHECKED: Final = "meho_access_not_checked"

#: The start of every refusal text.
_REFUSED: Final = "this write was refused"
#: The end of a refusal text for a write that no approval lets through.
NO_APPROVAL: Final = "No approval and no grant allows this write."


class MehoAccessRefusedError(KeycloakDeleteRefusedError):
    """A user write that would take away MEHO's own access was refused.

    ``code`` is one of ``meho_service_login``, ``last_platform_admin``,
    ``own_account`` or ``meho_access_not_checked``; the client and realm
    writes add ``meho_own_client`` and ``meho_login_realm``
    (:mod:`~meho_backplane.connectors.keycloak.ops_meho_own_login_guard`).
    ``reason`` is the plain text without the op id and code; it names no
    user.
    """

    def __init__(self, op_id: str, code: str, reason: str) -> None:
        super().__init__(op_id, code, reason)
        self.reason = f"{op_id}: {reason}"


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


class RealmRoleError(CannotCountError):
    """Platform admin rights come from a realm role, which MEHO cannot count."""


async def _composite_role_names(
    connector: KeycloakConnector, operator: Operator, target: KeycloakTargetLike, path: str
) -> set[str]:
    rows = await connector._get_admin_list(target, path, operator=operator, strict=True)
    names = {_text(row.get("name")) if isinstance(row, dict) else None for row in rows}
    if None in names:
        raise CannotCountError("a role without a name")
    return {name for name in names if name is not None}


async def has_platform_role(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realm: str,
    *,
    roles: set[str],
    user_id: str | None = None,
    group_id: str | None = None,
) -> bool:
    """Whether the user or group has one of *roles*: direct, from a group, or in another role."""
    if user_id is not None:
        path = fill_path(_USER_ROLE_MAPPINGS_REALM_PATH, {"realm": realm, "user-id": user_id})
        path = f"{path}/composite"
    else:
        path = fill_path(_GROUP_REALM_COMPOSITE_PATH, {"realm": realm, "group-id": group_id})
    return bool(await _composite_role_names(connector, operator, target, path) & roles)


def platform_roles() -> set[str]:
    """The realm role names that give platform admin rights; raises when not known."""
    # Imported late: this module imports the user lookup, which imports this module.
    from meho_backplane.connectors.keycloak.ops_write_effective_roles import platform_role_names

    roles = platform_role_names()
    if roles is None:
        raise CannotCountError("MEHO cannot read its settings")
    return roles


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
    user_id = row_id(user)
    if not is_person(user):
        return False
    roles = platform_roles()
    if op_id == _MEMBER_REMOVE_OP:
        removed = await _removed_group_id(connector, operator, target, params)
        change = Change(user_left=(user_id, removed))
    else:
        change = Change(user_gone=user_id)
    counter = Counter(connector, operator, target, realm, change)
    before = await counter.rights(user, after=False)
    has_role = bool(roles) and await has_platform_role(
        connector, operator, target, realm, roles=roles, user_id=user_id
    )
    if not (before.possible or has_role):
        return False
    if roles:
        # A realm role can give the rights in ways MEHO cannot count.
        raise RealmRoleError("platform admin rights come from a realm role")
    if (await counter.rights(user, after=True)).sure:
        return False
    return not await counter.sure_admin_after()


def _refused(op_id: str, code: str, reason: str) -> MehoAccessRefusedError:
    return MehoAccessRefusedError(op_id, code, reason)


async def refuse_last_admin_loss(
    op_id: str, realm: str, last_reason: str, count: Callable[[], Awaitable[bool]]
) -> None:
    """Take the realm's lock, run *count*, and refuse when it is true or cannot be known.

    Before the write is sent, MEHO checks that the database still holds the
    lock. A run path without a lock scope raises :exc:`MehoAccessScopeError`.
    """
    try:
        await hold_meho_access_lock(realm)
    except MehoAccessScopeError:
        raise
    except Exception as exc:
        raise _refused(
            op_id,
            _NOT_CHECKED,
            f"{_REFUSED}: MEHO could not make sure that no other write of this kind runs "
            f"at the same time ({type(exc).__name__}). Nothing was changed.",
        ) from exc
    try:
        async with asyncio.timeout(_COUNT_TIMEOUT_S):
            last = await count()
    except RealmRoleError as exc:
        raise _refused(
            op_id,
            _NOT_CHECKED,
            f"{_REFUSED}: platform admin rights come from a realm role here, so MEHO "
            "cannot count the platform admins. Nothing was changed.",
        ) from exc
    except Exception as exc:
        raise _refused(
            op_id,
            _NOT_CHECKED,
            f"{_REFUSED}: a Keycloak read failed, so MEHO could not check that this write "
            f"leaves another platform admin ({type(exc).__name__}). Nothing was changed.",
        ) from exc
    if last:
        raise _refused(op_id, _LAST_PLATFORM_ADMIN, last_reason)
    try:
        await confirm_meho_access_lock(realm)
    except MehoAccessScopeError:
        raise
    except Exception as exc:
        raise _refused(
            op_id,
            _NOT_CHECKED,
            f"{_REFUSED}: MEHO lost its hold that no other write of this kind runs at the "
            f"same time ({type(exc).__name__}). Nothing was changed.",
        ) from exc


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
            f"{_REFUSED}: this user is a login that MEHO itself uses, and {service_action} "
            f"can stop MEHO from working. Nothing was changed. {NO_APPROVAL}",
        )
    action = LOCK_OUT_WRITES.get(op_id)
    if action is not None and _is_own_account(user, operator):
        raise _refused(
            op_id,
            _OWN_ACCOUNT,
            f"{_REFUSED}: this user is the account that asked for this write or runs it, "
            f"and {action} can lock that account out of MEHO. Nothing was changed. "
            f"{NO_APPROVAL}",
        )
    if action is None and op_id != _MEMBER_REMOVE_OP:
        return
    words = action or "removing it from this group"

    async def _count() -> bool:
        return await _loses_last_platform_admin(
            connector, operator, target, realm, user, op_id=op_id, params=params
        )

    await refuse_last_admin_loss(
        op_id,
        realm,
        f"{_REFUSED}: this user is the last active platform admin of MEHO, and {words} "
        f"leaves MEHO with no platform admin. Nothing was changed. {NO_APPROVAL}",
        _count,
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
        async with meho_access_check_only():
            await refuse_meho_access_loss(
                connector, operator, target, user, op_id=op_id, params=params, realm=realm
            )
    except MehoAccessRefusedError as exc:
        return exc.reason
    return None

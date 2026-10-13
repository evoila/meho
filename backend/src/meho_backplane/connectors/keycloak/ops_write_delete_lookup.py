# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Find the object and check the guards for a Keycloak delete (#3944).

The first two steps of every governed Keycloak delete, shared by the preview
builders (:mod:`~meho_backplane.connectors.keycloak.ops_write_preview`, before
the request waits for approval) and the handlers
(:mod:`~meho_backplane.connectors.keycloak.ops_write_delete`, when the
approved call runs):

1. **Find the object** by internal id, or by a name that is looked up in the
   target's managed realm. A name that matches more than one object raises
   :exc:`KeycloakDeleteRefusedError` (``ambiguous_name``).
2. **Check the guards**: Keycloak's built-in clients, the client and the user
   this MEHO target logs in with, and a group that still has sub-groups
   (unless ``delete_subgroups`` is true). A breach raises
   :exc:`KeycloakDeleteRefusedError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import httpx

from meho_backplane.connectors.keycloak._paths import (
    _CLIENT_PATH,
    _CLIENTS_PATH,
    _GROUP_PATH,
    _GROUPS_PATH,
    _USER_PATH,
    _USERS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.session import (
    KeycloakPasswordCredentials,
    RealmConfig,
    quote_segment,
    resolve_realm_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector
    from meho_backplane.connectors.keycloak.session import KeycloakTargetLike

__all__ = [
    "BUILTIN_CLIENT_IDS",
    "CLIENT_DELETE_OP",
    "GROUP_DELETE_OP",
    "SERVICE_ACCOUNT_PREFIX",
    "USER_DELETE_OP",
    "FoundObject",
    "KeycloakDeleteRefusedError",
    "check_client_guards",
    "check_group_guards",
    "check_user_guards",
    "find_client",
    "find_group",
    "find_user",
    "get_or_none",
    "str_field",
    "subgroup_count",
]

USER_DELETE_OP: Final = "keycloak.user.delete"
GROUP_DELETE_OP: Final = "keycloak.group.delete"
CLIENT_DELETE_OP: Final = "keycloak.client.delete"

#: The clients Keycloak creates in every realm. Deleting one breaks login,
#: the account console or the admin console for the whole realm.
BUILTIN_CLIENT_IDS: Final[frozenset[str]] = frozenset(
    {
        "admin-cli",
        "account",
        "account-console",
        "broker",
        "realm-management",
        "security-admin-console",
    }
)

#: Keycloak names a client's service-account user ``service-account-<clientId>``
#: (lower case, like every Keycloak username).
SERVICE_ACCOUNT_PREFIX: Final = "service-account-"


class KeycloakDeleteRefusedError(Exception):
    """A delete was refused before anything was sent to Keycloak.

    ``code`` is a short, stable word for scripts: ``ambiguous_name``,
    ``builtin_client``, ``target_login_client``, ``target_login_user`` or
    ``group_has_subgroups``. The message says why in plain words.
    """

    def __init__(self, op_id: str, code: str, message: str) -> None:
        super().__init__(f"{op_id} refused ({code}): {message}")
        self.code = code


@dataclass(frozen=True)
class FoundObject:
    """The object one delete acts on, after the lookup.

    ``rep`` is the object's current representation, or ``None`` when it does
    not exist. ``object_id`` is its internal id (``None`` when a name matched
    nothing). ``name`` is the human name to report: the stored one when the
    object exists, else the name the caller passed.
    """

    realms: RealmConfig
    object_id: str | None
    name: str | None
    rep: dict[str, Any] | None

    @property
    def present(self) -> bool:
        return self.rep is not None

    @property
    def realm(self) -> str:
        return self.realms.managed_realm


def _opt_str(value: Any) -> str | None:
    """Return a trimmed non-empty string, or ``None`` for absent/blank input."""
    if isinstance(value, str):
        trimmed = value.strip()
        return trimmed or None
    return None


def str_field(rep: dict[str, Any] | None, key: str) -> str | None:
    """Return ``rep[key]`` when it is a non-empty string, else ``None``."""
    value = rep.get(key) if rep is not None else None
    return value if isinstance(value, str) and value else None


async def get_or_none(
    connector: KeycloakConnector,
    target: KeycloakTargetLike,
    path: str,
    operator: Operator,
) -> dict[str, Any] | None:
    """GET one object; ``None`` when Keycloak answers 404 (it does not exist)."""
    try:
        rep = await connector._get_admin_json(target, path, operator=operator)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise
    return rep if isinstance(rep, dict) else None


def _single_match(
    op_id: str,
    matches: list[dict[str, Any]],
    *,
    kind: str,
    name: str,
    realm: str,
    describe: Callable[[dict[str, Any]], str],
) -> dict[str, Any] | None:
    """Return the one match, ``None`` for no match; refuse more than one."""
    if len(matches) > 1:
        found = ", ".join(describe(row) for row in matches[:10])
        raise KeycloakDeleteRefusedError(
            op_id,
            "ambiguous_name",
            f"the name {name!r} matches {len(matches)} {kind}s in realm {realm!r}. "
            f"Pass the id instead. Matches: {found}",
        )
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Step 1 — find the object
# ---------------------------------------------------------------------------


async def find_user(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> FoundObject:
    """Find the user by ``id`` (preferred) or by exact ``username``."""
    realms = resolve_realm_config(target)
    realm = realms.managed_realm
    user_id = _opt_str(params.get("id"))
    username = _opt_str(params.get("username"))
    if user_id is not None:
        path = fill_path(_USER_PATH, {"realm": realm, "user-id": quote_segment(user_id)})
        rep = await get_or_none(connector, target, path, operator)
    elif username is not None:
        rows = await connector._get_admin_list(
            target,
            fill_path(_USERS_PATH, {"realm": realm}),
            operator=operator,
            params={"username": username, "exact": "true"},
        )
        # Keycloak usernames are case-insensitive; compare the same way.
        wanted = username.casefold()
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("username", "")).casefold() == wanted
        ]
        rep = _single_match(
            USER_DELETE_OP,
            matches,
            kind="user",
            name=username,
            realm=realm,
            describe=lambda row: f"id={row.get('id')}",
        )
    else:
        raise ValueError(f"{USER_DELETE_OP} requires either 'id' (UUID) or 'username'")
    return FoundObject(
        realms=realms,
        object_id=str_field(rep, "id") or user_id,
        name=str_field(rep, "username") or username,
        rep=rep,
    )


async def find_client(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> FoundObject:
    """Find the client by internal ``id`` (preferred) or by exact ``client_id``."""
    realms = resolve_realm_config(target)
    realm = realms.managed_realm
    uuid = _opt_str(params.get("id"))
    client_id = _opt_str(params.get("client_id"))
    if uuid is not None:
        path = fill_path(_CLIENT_PATH, {"realm": realm, "client-uuid": quote_segment(uuid)})
        rep = await get_or_none(connector, target, path, operator)
    elif client_id is not None:
        rows = await connector._get_admin_list(
            target,
            fill_path(_CLIENTS_PATH, {"realm": realm}),
            operator=operator,
            params={"clientId": client_id},
        )
        matches = [
            row for row in rows if isinstance(row, dict) and row.get("clientId") == client_id
        ]
        rep = _single_match(
            CLIENT_DELETE_OP,
            matches,
            kind="client",
            name=client_id,
            realm=realm,
            describe=lambda row: f"id={row.get('id')}",
        )
    else:
        raise ValueError(f"{CLIENT_DELETE_OP} requires either 'id' (UUID) or 'client_id'")
    return FoundObject(
        realms=realms,
        object_id=str_field(rep, "id") or uuid,
        name=str_field(rep, "clientId") or client_id,
        rep=rep,
    )


async def find_group(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    params: dict[str, Any],
) -> FoundObject:
    """Find the group by ``id`` (preferred) or by exact ``name`` at any level.

    A name lookup searches the whole group tree, not one level: the same
    name can exist under different parents, and a level-only lookup would
    report a nested group as "already gone". Keycloak's exact search with
    ``populateHierarchy=false`` returns every matching group as a flat list
    (with its ``path``). The match is then read by id, so the result always
    carries the current ``subGroupCount``.
    """
    realms = resolve_realm_config(target)
    realm = realms.managed_realm
    group_id = _opt_str(params.get("id"))
    name = _opt_str(params.get("name"))
    if group_id is None:
        if name is None:
            raise ValueError(f"{GROUP_DELETE_OP} requires either 'id' (UUID) or 'name'")
        rows = await connector._get_admin_list(
            target,
            fill_path(_GROUPS_PATH, {"realm": realm}),
            operator=operator,
            params={
                "search": name,
                "exact": "true",
                "populateHierarchy": "false",
                "briefRepresentation": "true",
            },
        )
        matches = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
        match = _single_match(
            GROUP_DELETE_OP,
            matches,
            kind="group",
            name=name,
            realm=realm,
            describe=lambda row: f"{row.get('path')} id={row.get('id')}",
        )
        group_id = str_field(match, "id")
    rep = None
    if group_id is not None:
        path = fill_path(_GROUP_PATH, {"realm": realm, "group-id": quote_segment(group_id)})
        rep = await get_or_none(connector, target, path, operator)
    return FoundObject(
        realms=realms,
        object_id=group_id,
        name=str_field(rep, "name") or name,
        rep=rep,
    )


# ---------------------------------------------------------------------------
# Step 2 — the guards
# ---------------------------------------------------------------------------


async def _target_login_identity(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    realms: RealmConfig,
) -> tuple[str | None, frozenset[str]]:
    """Return the client id and usernames this target logs in with.

    The target logs in to its ``admin_realm``. A delete runs in the
    ``managed_realm``, so the login client and user can only be hit when the
    two realms are the same; otherwise this returns ``(None, frozenset())``
    without reading the admin credential.

    With a client secret, the login user is the client's service-account
    user. With a username and password, it is that username. Only the names
    are used here; the secret is never read out of the credential object.
    """
    if realms.managed_realm != realms.admin_realm:
        return None, frozenset()
    creds = await connector._credentials_loader(target, operator)
    if isinstance(creds, KeycloakPasswordCredentials):
        return creds.client_id, frozenset({creds.username.casefold()})
    return creds.client_id, frozenset({f"{SERVICE_ACCOUNT_PREFIX}{creds.client_id}".casefold()})


async def check_user_guards(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    found: FoundObject,
) -> None:
    """Refuse the user this target logs in with."""
    username = found.name or ""
    _client_id, login_users = await _target_login_identity(
        connector, operator, target, found.realms
    )
    if username.casefold() in login_users:
        raise KeycloakDeleteRefusedError(
            USER_DELETE_OP,
            "target_login_user",
            f"{username!r} is the user this MEHO target logs in with; deleting it "
            "breaks the target.",
        )


async def check_client_guards(
    connector: KeycloakConnector,
    operator: Operator,
    target: KeycloakTargetLike,
    found: FoundObject,
) -> None:
    """Refuse Keycloak's built-in clients and the client this target logs in with."""
    client_id = found.name or ""
    if client_id in BUILTIN_CLIENT_IDS:
        raise KeycloakDeleteRefusedError(
            CLIENT_DELETE_OP,
            "builtin_client",
            f"{client_id!r} is a built-in Keycloak client; deleting it breaks login "
            "or the admin console.",
        )
    login_client, _users = await _target_login_identity(connector, operator, target, found.realms)
    if login_client is not None and client_id == login_client:
        raise KeycloakDeleteRefusedError(
            CLIENT_DELETE_OP,
            "target_login_client",
            f"{client_id!r} is the client this MEHO target logs in with; deleting it "
            "breaks the target.",
        )


def subgroup_count(rep: dict[str, Any]) -> int | None:
    """Return the group's ``subGroupCount``, or ``None`` when it is missing."""
    count = rep.get("subGroupCount")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    return None


def check_group_guards(found: FoundObject, params: dict[str, Any]) -> None:
    """Refuse a group with sub-groups unless ``delete_subgroups`` is true.

    An unknown sub-group count is treated as "has sub-groups" (fail closed).
    """
    if found.rep is None or params.get("delete_subgroups") is True:
        return
    count = subgroup_count(found.rep)
    if count == 0:
        return
    shown = "an unknown number of" if count is None else str(count)
    raise KeycloakDeleteRefusedError(
        GROUP_DELETE_OP,
        "group_has_subgroups",
        f"group {str_field(found.rep, 'path') or found.name!r} has {shown} sub-group(s), "
        "and Keycloak deletes them with it. Pass delete_subgroups=true to do that "
        "on purpose.",
    )

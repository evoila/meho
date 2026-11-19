# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time facts for the Keycloak writes that change who has rights.

``keycloak.group.member.add``, ``keycloak.role_mapping.assign``,
``keycloak.role_mapping.client.assign`` and
``keycloak.group.update_attributes`` change what a login may do in MEHO.
``keycloak.user.update`` (a new email or user name),
``keycloak.user.credential.delete`` (a passkey or one-time code removed)
and ``keycloak.user.reset_password`` (a new password) change how a login
signs in, so they count as a change of rights too. In the login realm they
count as "gives platform admin" when the login has platform admin rights
(:func:`~.ops_write_platform_reach.login_has_platform_admin`).
When one of them waits for approval, :func:`with_grant_facts` adds a
``grant`` block to its preview:

* ``user_id``: the login the change is for. A user name is resolved to its
  id here, so the approval compares ids. ``None`` when it cannot be read.
* ``group_id``: the group the change is for, resolved the way the handler
  resolves it: an id in the params wins, else the name is looked up on
  the same level (``parent_id``). ``None`` when it cannot be read.
* ``login_realm``: the target manages a realm that MEHO's own login uses,
  by the realm name Keycloak answers with. ``True`` when it cannot be read.
* ``platform_admin``: the change can give platform admin rights, or gives a
  role of the ``realm-management`` client (it decides who may manage the
  realm). For a group, MEHO reads what the group gives
  (:func:`~.ops_write_grant_platform.group_gives_platform_admin`): the
  ``platform_admin`` attribute on the group or a parent group, a place under
  the platform admin group, or the platform admin realm role. A group
  attribute change that sets ``platform_admin`` counts too. ``None`` when
  MEHO could not read the group, which the approval treats as the strict
  case.
* ``members``: for a group attribute change in the login realm, the ids
  of the logins in the group (up to :data:`MEMBER_CAP`). ``None`` when
  they could not be read, or when there are more than the cap. The
  approval then refuses: it cannot tell whether the approver is in the
  group.

The same builder runs again right before the approved change runs, so
the approval check can compare the ids with the live system. For
``keycloak.user.credential.delete`` it also compares the credentials the
card lists under ``will_remove``: when they differ, the run is refused
with :data:`CREDENTIALS_CHANGED`. The approval
rules that use these facts are in
:mod:`meho_backplane.operations.grant_approval`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import (
    _ADMIN_REALM_PATH,
    _GROUP_MEMBERS_PATH,
    fill_path,
)
from meho_backplane.connectors.keycloak.ops_write_account_common import is_realm_management
from meho_backplane.connectors.keycloak.ops_write_effective_roles import (
    ROLE_PARTS_CHANGED,
    ROLE_PARTS_NOT_READ,
    effective_roles,
)
from meho_backplane.connectors.keycloak.ops_write_grant_platform import (
    attributes_set_platform_admin,
    group_gives_platform_admin,
)
from meho_backplane.connectors.keycloak.ops_write_platform_reach import (
    REASON_LOGIN,
    login_has_platform_admin,
    platform_fields,
)
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.core.keycloak import ADMIN_GROUPS
from meho_backplane.operations._preview import PreviewBuilder, PreviewContext
from meho_backplane.operations.grant_approval import register_grant_op
from meho_backplane.settings import get_settings
from meho_backplane.targets.keycloak_realm_warnings import (
    backplane_login_realms as _backplane_login_realms,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

__all__ = [
    "CREDENTIALS_CHANGED",
    "GRANT_OPS",
    "MEMBER_CAP",
    "is_login_realm",
    "is_login_realm_name",
    "register_grant_ops",
    "with_grant_facts",
]

PLATFORM_ADMIN_GROUP = ADMIN_GROUPS[1]

#: The most members MEHO reads for a group attribute change. A group with
#: more counts as "members unknown".
MEMBER_CAP: Final[int] = 1000

_MEMBER_PAGE: Final[int] = 100

#: op_id -> (params with the user id, param with the group id, param with the group name)
GRANT_OPS: dict[str, tuple[tuple[str, ...], str | None, str | None]] = {
    "keycloak.group.member.add": (("user_id",), "group_id", "group_name"),
    "keycloak.role_mapping.assign": (("id",), None, None),
    "keycloak.role_mapping.client.assign": (("id",), None, None),
    "keycloak.user.update": (("id",), None, None),
    "keycloak.user.credential.delete": (("id",), None, None),
    "keycloak.user.reset_password": (("id",), None, None),
    "keycloak.group.update_attributes": ((), "id", "name"),
}

#: Why an approved credential delete did not run.
CREDENTIALS_CHANGED: Final[str] = (
    "The credentials changed since the request; send a new request. The operation did not run."
)

#: op_id -> (the card key that lists what the run acts on, the refusal when it changed)
_BOUND_IDS: dict[str, tuple[str, str]] = {
    "keycloak.user.credential.delete": ("will_remove", CREDENTIALS_CHANGED),
    "keycloak.role_mapping.assign": ("effective_roles", ROLE_PARTS_CHANGED),
    "keycloak.role_mapping.client.assign": ("effective_roles", ROLE_PARTS_CHANGED),
}


#: The ops that give roles: their card lists the roles the request really gives.
_CLIENT_ASSIGN: Final[str] = "keycloak.role_mapping.client.assign"
_ROLE_OPS: Final[frozenset[str]] = frozenset({"keycloak.role_mapping.assign", _CLIENT_ASSIGN})
_ATTRS: Final[str] = "keycloak.group.update_attributes"

#: The ops that change how one login signs in: they need a platform admin when
#: the login has platform admin rights.
_LOGIN_OPS: Final[frozenset[str]] = frozenset(
    {"keycloak.user.update", "keycloak.user.credential.delete", "keycloak.user.reset_password"}
)

#: Why a group or role change needs a platform admin.
REASON_GROUP_OR_ROLE: Final[str] = (
    "The group or role gives platform admin rights, or lets a login manage the realm."
)


def register_grant_ops() -> None:
    """Tell the approval check which Keycloak ops change who has rights."""
    for op_id, (user_params, group_id_param, _group_name) in GRANT_OPS.items():
        register_grant_op(
            op_id,
            user_id_params=user_params,
            group_id_params=(group_id_param,) if group_id_param else (),
            bound_ids=_BOUND_IDS.get(op_id),
            bound_ids_missing=ROLE_PARTS_NOT_READ if op_id in _ROLE_OPS else None,
        )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _live_connector(ctx: PreviewContext) -> KeycloakConnector | None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if ctx.target is None or not isinstance(connector, KeycloakConnector):
        return None
    return connector


def is_login_realm_name(realm: str) -> bool:
    """True when *realm* names a realm of MEHO's own login (letter case and spaces ignored)."""
    own = {name.strip().casefold() for name in _backplane_login_realms()}
    return realm.strip().casefold() in own


async def is_login_realm(ctx: PreviewContext) -> bool:
    """True when the target manages a realm of MEHO's own login, or that cannot be read.

    The name is the one Keycloak answers with for the target's realm
    (``GET /admin/realms/{realm}``), not the text stored on the target: a
    Keycloak database can match a realm name without letter case or with
    a space at the end. When the read fails, the realm counts as MEHO's own.
    """
    connector = _live_connector(ctx)
    if connector is None:
        return True
    try:
        stored = resolve_realm_config(ctx.target).managed_realm.strip()
        answer = await connector._get_admin_json(
            ctx.target,
            fill_path(_ADMIN_REALM_PATH, {"realm": stored}),
            operator=ctx.operator,
        )
        name = _text(answer.get("realm")) if isinstance(answer, dict) else None
        if name is None:
            return True
        return is_login_realm_name(name) or is_login_realm_name(stored)
    except Exception:
        return True


async def _user_id(ctx: PreviewContext, user_params: tuple[str, ...]) -> str | None:
    for key in user_params:
        if (value := _text(ctx.params.get(key))) is not None:
            return value
    username = _text(ctx.params.get("username"))
    connector = _live_connector(ctx)
    if username is None or connector is None:
        return None
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        return await connector._find_user_uuid(ctx.target, realm, username, operator=ctx.operator)
    except Exception:
        return None


async def _group(ctx: PreviewContext, group_id_param: str, group_name_param: str) -> str | None:
    """The id of the group the handler will change; ``None`` when unknown.

    The same order as the handler: an id in the params wins. Without an id,
    the name is looked up on the level ``parent_id`` names, which gives the id.
    """
    group_id = _text(ctx.params.get(group_id_param))
    if group_id is not None:
        return group_id
    name = _text(ctx.params.get(group_name_param))
    connector = _live_connector(ctx)
    if name is None or connector is None:
        return None
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        row = await connector._find_group(
            ctx.target, realm, name, _text(ctx.params.get("parent_id")), operator=ctx.operator
        )
    except Exception:
        return None
    found_id = row.get("id") if isinstance(row, dict) else None
    return found_id if isinstance(found_id, str) and found_id else None


async def _group_platform(op_id: str, ctx: PreviewContext, group_id: str | None) -> bool | None:
    """Whether the group change can give platform admin rights; ``None`` when unknown."""
    if op_id == _ATTRS and attributes_set_platform_admin(ctx.params.get("attributes")):
        return True
    connector = _live_connector(ctx)
    if group_id is None or connector is None:
        return None
    return await group_gives_platform_admin(connector, ctx, group_id)


async def _members(ctx: PreviewContext, group_id: str) -> list[str] | None:
    """The ids of the logins in the group; ``None`` when they cannot be read or exceed the cap."""
    connector = _live_connector(ctx)
    if connector is None:
        return None
    members: list[str] = []
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        path = fill_path(_GROUP_MEMBERS_PATH, {"realm": realm, "group-id": group_id})
        while len(members) <= MEMBER_CAP:
            rows = await connector._get_admin_list(
                ctx.target,
                path,
                operator=ctx.operator,
                params={
                    "first": str(len(members)),
                    "max": str(_MEMBER_PAGE),
                    "briefRepresentation": "true",
                },
                strict=True,
            )
            ids = [row["id"] for row in rows if isinstance(row, dict) and _text(row.get("id"))]
            if len(ids) != len(rows):
                return None
            members.extend(ids)
            if len(rows) < _MEMBER_PAGE:
                break
    except Exception:
        return None
    return None if len(members) > MEMBER_CAP else members


def _roles_include_platform_admin(ctx: PreviewContext) -> bool | None:
    """Whether the roles include the platform admin role; ``None`` when that cannot be read."""
    raw = ctx.params.get("roles")
    names = {str(r).strip().casefold() for r in raw} if isinstance(raw, list) else set()
    if names & {PLATFORM_ADMIN_GROUP.casefold(), "platform_admin"}:
        return True
    try:
        configured = get_settings().jwt_platform_admin_role_name
    except Exception:
        return None
    if not configured:
        return False
    return configured.casefold() in names


async def _with_effective_roles(
    op_id: str,
    ctx: PreviewContext,
    preview: dict[str, Any],
    platform: bool | None,
    *,
    login_realm: bool,
) -> bool | None:
    """Put the effective roles on the card; return the platform admin fact they decide.

    In MEHO's own login realm a list MEHO could read decides alone: the name of
    the asked-for role does not count. A list MEHO cannot read makes the fact
    strict (``None``) unless it was already ``True``. Outside the login realm
    the list is empty and the fact is unchanged.
    """
    connector = _live_connector(ctx)
    roles: list[dict[str, Any]] | None = []
    if login_realm:
        roles = None
        if connector is not None:
            client_id = _text(ctx.params.get("client_id")) if op_id == _CLIENT_ASSIGN else None
            roles = await effective_roles(
                connector,
                ctx,
                resolve_realm_config(ctx.target).managed_realm,
                client_id=client_id,
            )
        preview["effective_roles"] = roles
        if roles is None:
            return True if platform else None
        return any(r["realm_management"] or r["login_role"] for r in roles)
    else:
        preview["effective_roles"] = roles
    return platform


async def _login_platform(ctx: PreviewContext, user_id: str | None) -> bool | None:
    """Whether the login of a sign-in change has platform admin rights; ``None`` when unknown."""
    connector = _live_connector(ctx)
    if user_id is None or connector is None:
        return None
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
    except Exception:
        return None
    return await login_has_platform_admin(connector, ctx, realm, user_id)


def with_grant_facts(op_id: str, builder: PreviewBuilder) -> PreviewBuilder:
    """Wrap *builder* so its preview carries the ``grant`` block."""
    user_params, group_id_param, group_name_param = GRANT_OPS[op_id]

    async def _wrapped(ctx: PreviewContext) -> dict[str, Any] | None:
        preview = await builder(ctx)
        if preview is None:
            return None
        login_realm = await is_login_realm(ctx)
        user_id = await _user_id(ctx, user_params)
        group_id: str | None = None
        reason = REASON_GROUP_OR_ROLE
        if op_id in _LOGIN_OPS:
            platform: bool | None = _roles_include_platform_admin(ctx)
            if platform is False and login_realm:
                platform = await _login_platform(ctx, user_id)
                reason = REASON_LOGIN
        elif op_id == _CLIENT_ASSIGN and is_realm_management(ctx.params):
            platform = True
        elif group_id_param is None or group_name_param is None:
            platform = _roles_include_platform_admin(ctx)
        else:
            group_id = await _group(ctx, group_id_param, group_name_param)
            platform = await _group_platform(op_id, ctx, group_id)
        if op_id in _ROLE_OPS:
            platform = await _with_effective_roles(
                op_id, ctx, preview, platform, login_realm=login_realm
            )
        grant: dict[str, Any] = {
            "user_id": user_id,
            "group_id": group_id,
            "login_realm": login_realm,
            **platform_fields(platform, reason),
        }
        if not user_params:
            grant["members"] = (
                await _members(ctx, group_id) if login_realm and group_id is not None else None
            )
        preview["grant"] = grant
        return preview

    return _wrapped

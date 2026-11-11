# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time facts for the Keycloak writes that change who has rights.

``keycloak.group.member.add``, ``keycloak.role_mapping.assign``,
``keycloak.role_mapping.client.assign`` and
``keycloak.group.update_attributes`` change what a login may do in MEHO.
``keycloak.user.update`` (a new email or user name) and
``keycloak.user.credential.delete`` (a passkey or one-time code removed)
change how a login signs in, so they count as a change of rights too.
When one of them waits for approval, :func:`with_grant_facts` adds a
``grant`` block to its preview:

* ``user_id``: the login the change is for. A user name is resolved to its
  id here, so the approval compares ids. ``None`` when it cannot be read.
* ``group_id``: the group the change is for, resolved the way the handler
  resolves it: an id in the params wins, else the name is looked up on
  the same level (``parent_id``). ``None`` when it cannot be read.
* ``login_realm``: the target manages a realm that MEHO's own login uses.
* ``platform_admin``: the change is for the platform admin group or role.
  Read from the group the handler will change, by id when the params
  carry one. ``None`` when MEHO could not read the group, which the
  approval treats as the strict case.
* ``members``: for a group attribute change in the login realm, the ids
  of the logins in the group (up to :data:`MEMBER_CAP`). ``None`` when
  they could not be read, or when there are more than the cap. The
  approval then refuses: it cannot tell whether the approver is in the
  group.

The same builder runs again right before the approved change runs, so
the approval check can compare the ids with the live system. The approval
rules that use these facts are in
:mod:`meho_backplane.operations.grant_approval`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.keycloak._paths import _GROUP_MEMBERS_PATH, fill_path
from meho_backplane.connectors.keycloak.ops_write_delete_lookup import find_group
from meho_backplane.connectors.keycloak.ops_write_realm_delete import _backplane_login_realms
from meho_backplane.connectors.keycloak.session import quote_segment, resolve_realm_config
from meho_backplane.core.keycloak import ADMIN_GROUPS
from meho_backplane.operations._preview import PreviewBuilder, PreviewContext
from meho_backplane.operations.grant_approval import register_grant_op
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

__all__ = ["GRANT_OPS", "MEMBER_CAP", "register_grant_ops", "with_grant_facts"]

PLATFORM_ADMIN_GROUP = ADMIN_GROUPS[1]
_WANTED: Final[str] = PLATFORM_ADMIN_GROUP.casefold()

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
    "keycloak.group.update_attributes": ((), "id", "name"),
}


def register_grant_ops() -> None:
    """Tell the approval check which Keycloak ops change who has rights."""
    for op_id, (user_params, group_id_param, _group_name) in GRANT_OPS.items():
        register_grant_op(
            op_id,
            user_id_params=user_params,
            group_id_params=(group_id_param,) if group_id_param else (),
        )


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _live_connector(ctx: PreviewContext) -> KeycloakConnector | None:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

    connector = ctx.connector_instance
    if ctx.target is None or not isinstance(connector, KeycloakConnector):
        return None
    return connector


def _login_realm(ctx: PreviewContext) -> bool:
    """True when the target manages a realm of MEHO's own login, or that cannot be read."""
    if ctx.target is None:
        return True
    try:
        realm = resolve_realm_config(ctx.target).managed_realm.casefold()
        return realm in {name.casefold() for name in _backplane_login_realms()}
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


async def _group(
    ctx: PreviewContext, group_id_param: str, group_name_param: str
) -> tuple[str | None, str | None]:
    """The id and the name of the group the handler will change; ``None`` when unknown.

    The same order as the handler: an id in the params wins, and its name
    is read by that id. Without an id, the name is looked up on the level
    ``parent_id`` names, which gives the id.
    """
    group_id = _text(ctx.params.get(group_id_param))
    name = _text(ctx.params.get(group_name_param))
    connector = _live_connector(ctx)
    if group_id is not None:
        if connector is None:
            return group_id, None
        try:
            found = await find_group(connector, ctx.operator, ctx.target, {"id": group_id})
        except Exception:
            return group_id, None
        return group_id, found.name
    if name is None or connector is None:
        return None, name
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        row = await connector._find_group(
            ctx.target, realm, name, _text(ctx.params.get("parent_id")), operator=ctx.operator
        )
    except Exception:
        return None, name
    found_id = row.get("id") if isinstance(row, dict) else None
    return (found_id if isinstance(found_id, str) and found_id else None), name


async def _members(ctx: PreviewContext, group_id: str) -> list[str] | None:
    """The ids of the logins in the group; ``None`` when they cannot be read or exceed the cap."""
    connector = _live_connector(ctx)
    if connector is None:
        return None
    members: list[str] = []
    try:
        realm = resolve_realm_config(ctx.target).managed_realm
        path = fill_path(_GROUP_MEMBERS_PATH, {"realm": realm, "group-id": quote_segment(group_id)})
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


def with_grant_facts(op_id: str, builder: PreviewBuilder) -> PreviewBuilder:
    """Wrap *builder* so its preview carries the ``grant`` block."""
    user_params, group_id_param, group_name_param = GRANT_OPS[op_id]

    async def _wrapped(ctx: PreviewContext) -> dict[str, Any] | None:
        preview = await builder(ctx)
        if preview is None:
            return None
        login_realm = _login_realm(ctx)
        group_id: str | None = None
        if group_id_param is None or group_name_param is None:
            platform = _roles_include_platform_admin(ctx)
        else:
            group_id, group_name = await _group(ctx, group_id_param, group_name_param)
            platform = None if group_name is None else group_name.casefold() == _WANTED
        grant: dict[str, Any] = {
            "user_id": await _user_id(ctx, user_params),
            "group_id": group_id,
            "login_realm": login_realm,
            "platform_admin": platform,
        }
        if not user_params:
            grant["members"] = (
                await _members(ctx, group_id) if login_realm and group_id is not None else None
            )
        preview["grant"] = grant
        return preview

    return _wrapped

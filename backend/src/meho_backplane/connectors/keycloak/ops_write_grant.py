# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Park-time facts for the Keycloak writes that change who has rights.

``keycloak.group.member.add``, ``keycloak.role_mapping.assign`` and
``keycloak.group.update_attributes`` change what a login may do in MEHO.
When one of them waits for approval, :func:`with_grant_facts` adds a
``grant`` block to its preview:

* ``user_id``: the login the change is for. A user name is resolved to its
  id here, so the approval compares ids. ``None`` when it cannot be read.
* ``login_realm``: the target manages a realm that MEHO's own login uses.
* ``platform_admin``: the change is for the platform admin group or role.
  ``None`` when MEHO could not read the group, which the approval treats as
  the strict case.

The approval rules that use these facts are in
:mod:`meho_backplane.operations.grant_approval`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors.keycloak.ops_write_delete_lookup import find_group
from meho_backplane.connectors.keycloak.ops_write_realm_delete import _backplane_login_realms
from meho_backplane.connectors.keycloak.session import resolve_realm_config
from meho_backplane.core.keycloak import ADMIN_GROUPS
from meho_backplane.operations._preview import PreviewBuilder, PreviewContext
from meho_backplane.operations.grant_approval import register_grant_op
from meho_backplane.settings import get_settings

if TYPE_CHECKING:
    from meho_backplane.connectors.keycloak.connector import KeycloakConnector

__all__ = ["GRANT_OPS", "register_grant_ops", "with_grant_facts"]

PLATFORM_ADMIN_GROUP = ADMIN_GROUPS[1]

#: op_id -> (params with the user id, param with the group id, param with the group name)
GRANT_OPS: dict[str, tuple[tuple[str, ...], str | None, str | None]] = {
    "keycloak.group.member.add": (("user_id",), "group_id", "group_name"),
    "keycloak.role_mapping.assign": (("id",), None, None),
    "keycloak.group.update_attributes": ((), "id", "name"),
}


def register_grant_ops() -> None:
    """Tell the approval check which Keycloak ops change who has rights."""
    for op_id, (user_params, _group_id, _group_name) in GRANT_OPS.items():
        register_grant_op(op_id, user_id_params=user_params)


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


async def _group_is_platform_admin(
    ctx: PreviewContext, group_id_param: str | None, group_name_param: str | None
) -> bool | None:
    """Whether the group is the platform admin group; ``None`` when unknown."""
    wanted = PLATFORM_ADMIN_GROUP.casefold()
    name = _text(ctx.params.get(group_name_param)) if group_name_param else None
    if name is not None:
        return name.casefold() == wanted
    group_id = _text(ctx.params.get(group_id_param)) if group_id_param else None
    connector = _live_connector(ctx)
    if group_id is None or connector is None:
        return None
    try:
        found = await find_group(connector, ctx.operator, ctx.target, {"id": group_id})
    except Exception:
        return None
    return None if found.name is None else found.name.casefold() == wanted


def _roles_include_platform_admin(ctx: PreviewContext) -> bool:
    raw = ctx.params.get("roles")
    names = {str(r).strip().casefold() for r in raw} if isinstance(raw, list) else set()
    wanted = {PLATFORM_ADMIN_GROUP.casefold(), "platform_admin"}
    configured = get_settings().jwt_platform_admin_role_name
    if configured:
        wanted.add(configured.casefold())
    return bool(names & wanted)


def with_grant_facts(op_id: str, builder: PreviewBuilder) -> PreviewBuilder:
    """Wrap *builder* so its preview carries the ``grant`` block."""
    user_params, group_id_param, group_name_param = GRANT_OPS[op_id]

    async def _wrapped(ctx: PreviewContext) -> dict[str, Any] | None:
        preview = await builder(ctx)
        if preview is None:
            return None
        if group_id_param is None:
            platform: bool | None = _roles_include_platform_admin(ctx)
        else:
            platform = await _group_is_platform_admin(ctx, group_id_param, group_name_param)
        preview["grant"] = {
            "user_id": await _user_id(ctx, user_params),
            "login_realm": _login_realm(ctx),
            "platform_admin": platform,
        }
        return preview

    return _wrapped

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the FreeIPA user, group and role writes, and their approval previews.

:class:`FreeipaIdentityWriteOps` holds one handler method per write.
:class:`~meho_backplane.connectors.freeipa.connector.FreeipaConnector`
inherits them, so the dispatcher binds each one to the live connector (the
mixin path of
:func:`~meho_backplane.operations._handler_resolve.is_unbound_method`).
Every write waits until a second person approves; the dispatcher runs these
methods only after that.

No write takes or returns a password. ``freeipa.user.create`` sends neither
``userpassword`` nor ``random``, so the new user has no password yet, and the
parameter schemas refuse any other parameter.

An add-member body checks FreeIPA's ``failed`` and ``completed`` with
:func:`~meho_backplane.connectors.freeipa._write_common.check_done`, so a
member FreeIPA did not add fails the call.

Three writes have an approval preview, each read with the requester's login:

* ``freeipa.user.delete`` shows the groups and roles the user leaves;
* ``freeipa.role.delete`` shows the members and privileges of the role;
* ``freeipa.role.member.add`` shows the privileges the new members get.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.freeipa._write_common import (
    add_members,
    as_list,
    description_option,
    result_dict,
    run_delete,
)
from meho_backplane.connectors.freeipa.rpc import (
    GROUP_ADD_COMMAND,
    GROUP_ADD_MEMBER_COMMAND,
    ROLE_ADD_MEMBER_COMMAND,
    ROLE_DEL_COMMAND,
    ROLE_SHOW_COMMAND,
    USER_ADD_COMMAND,
    USER_DEL_COMMAND,
    USER_DISABLE_COMMAND,
    USER_MOD_COMMAND,
    USER_SHOW_COMMAND,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.freeipa.rpc import FreeipaAnswer
    from meho_backplane.connectors.freeipa.session import FreeipaTargetLike

    class _Connector:
        """What the handler methods use of the FreeipaConnector they are part of."""

        async def call(
            self,
            target: FreeipaTargetLike,
            operator: Operator,
            command: str,
            args: list[Any] | None = None,
            options: dict[str, Any] | None = None,
        ) -> FreeipaAnswer:
            """Run one IPA command: :meth:`FreeipaConnector.call`."""

    _Base = _Connector
else:
    _Base = object

__all__ = [
    "EXPIRES_FORMAT",
    "ROLE_DELETE_OP_ID",
    "ROLE_MEMBER_ADD_OP_ID",
    "USER_DELETE_OP_ID",
    "FreeipaIdentityWriteOps",
    "role_delete_preview",
    "role_member_add_preview",
    "user_delete_preview",
]

USER_DELETE_OP_ID: Final[str] = "freeipa.user.delete"
ROLE_DELETE_OP_ID: Final[str] = "freeipa.role.delete"
ROLE_MEMBER_ADD_OP_ID: Final[str] = "freeipa.role.member.add"

#: The format of the ``expires`` parameter: UTC, to the second.
EXPIRES_FORMAT: Final[str] = "%Y-%m-%dT%H:%M:%SZ"

#: How FreeIPA's JSON-RPC takes a date (``ipalib/ipajson.py``).
_IPA_DATETIME_FORMAT: Final[str] = "%Y%m%d%H%M%SZ"

#: Each member parameter of ``freeipa.role.member.add``, and the IPA option it is sent as.
_ROLE_MEMBERS: Final[dict[str, str]] = {
    "users": "user",
    "groups": "group",
    "hosts": "host",
    "hostgroups": "hostgroup",
}


def _expiry(text: str) -> dict[str, str]:
    """FreeIPA's date value for *text*; refuses a time that is not in the future.

    A password that has run out must be changed at the next login, so a date
    in the past would lock a service account out.
    """
    when = datetime.strptime(text, EXPIRES_FORMAT).replace(tzinfo=UTC)
    if when <= datetime.now(UTC):
        raise ValueError(f"expires must be in the future; got {text}")
    return {"__datetime__": when.strftime(_IPA_DATETIME_FORMAT)}


class FreeipaIdentityWriteOps(_Base):
    """Handler methods of the user, group and role writes."""

    async def user_create(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.user.create``: ``user_add`` with no password and no ``random``."""
        options = {
            "givenname": params["first_name"],
            "sn": params["last_name"],
            "mail": [params["mail"]] if params.get("mail") else None,
        }
        answer = await self.call(target, operator, USER_ADD_COMMAND, [params["login"]], options)
        return {"entry": result_dict(result_dict(answer.result).get("result"))}

    async def user_password_expiry_set(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.user.password_expiry.set``: ``user_mod`` with ``krbpasswordexpiration``."""
        options = {"krbpasswordexpiration": _expiry(params["expires"])}
        answer = await self.call(target, operator, USER_MOD_COMMAND, [params["login"]], options)
        return {"entry": result_dict(result_dict(answer.result).get("result"))}

    async def user_disable(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.user.disable``: ``user_disable``."""
        answer = await self.call(target, operator, USER_DISABLE_COMMAND, [params["login"]])
        return {"disabled": result_dict(answer.result).get("value") or params["login"]}

    async def user_delete(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.user.delete``: ``user_del``. It sends no ``preserve``, so the user is gone."""
        return await run_delete(self, operator, target, USER_DEL_COMMAND, params["login"])

    async def group_create(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.group.create``: ``group_add``."""
        answer = await self.call(
            target, operator, GROUP_ADD_COMMAND, [params["name"]], description_option(params)
        )
        return {"entry": result_dict(result_dict(answer.result).get("result"))}

    async def group_member_add(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.group.member.add``: ``group_add_member`` with users and groups."""
        members = {"users": "user", "groups": "group"}
        return await add_members(self, operator, target, GROUP_ADD_MEMBER_COMMAND, params, members)

    async def role_member_add(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.role.member.add``: ``role_add_member`` with four kinds of member."""
        return await add_members(
            self, operator, target, ROLE_ADD_MEMBER_COMMAND, params, _ROLE_MEMBERS
        )

    async def role_delete(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.role.delete``: ``role_del``."""
        return await run_delete(self, operator, target, ROLE_DEL_COMMAND, params["name"])


async def _show(ctx: PreviewContext, command: str, key: str) -> tuple[str, dict[str, Any]] | None:
    """Read the object a write names, with the requester's login.

    Returns its name and entry. Returns ``None`` without a live connector
    (the ``preview_operation`` path, which calls no vendor system) or
    without a name. A failed read raises, so the approver sees that the
    preview failed and why while the request still waits for approval.
    """
    connector = ctx.connector_instance
    name = ctx.params.get(key)
    if not isinstance(connector, FreeipaIdentityWriteOps) or ctx.target is None:
        return None
    if not isinstance(name, str):
        return None
    answer = await connector.call(ctx.target, ctx.operator, command, [name])
    return name, result_dict(result_dict(answer.result).get("result"))


async def user_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The approval preview of ``freeipa.user.delete``: the user's groups and roles."""
    shown = await _show(ctx, USER_SHOW_COMMAND, "login")
    if shown is None:
        return None
    login, entry = shown
    return {
        "user": login,
        "groups": as_list(entry.get("memberof_group")),
        "indirect_groups": as_list(entry.get("memberofindirect_group")),
        "roles": as_list(entry.get("memberof_role")),
    }


async def role_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The approval preview of ``freeipa.role.delete``: the role's members and privileges."""
    shown = await _show(ctx, ROLE_SHOW_COMMAND, "name")
    if shown is None:
        return None
    name, entry = shown
    return {
        "role": name,
        "privileges": as_list(entry.get("memberof_privilege")),
        "members": {
            key: as_list(entry.get(f"member_{option}")) for key, option in _ROLE_MEMBERS.items()
        },
    }


async def role_member_add_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The approval preview of ``freeipa.role.member.add``: the privileges the new members get."""
    shown = await _show(ctx, ROLE_SHOW_COMMAND, "name")
    if shown is None:
        return None
    name, entry = shown
    return {
        "role": name,
        "privileges": as_list(entry.get("memberof_privilege")),
        "new_members": {key: list(ctx.params.get(key) or []) for key in _ROLE_MEMBERS},
    }


register_preview_builder(USER_DELETE_OP_ID, user_delete_preview)
register_preview_builder(ROLE_DELETE_OP_ID, role_delete_preview)
register_preview_builder(ROLE_MEMBER_ADD_OP_ID, role_member_add_preview)

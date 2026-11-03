# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the FreeIPA sudo, access rule (HBAC) and password policy writes, and their previews.

:class:`FreeipaAccessWriteOps` holds one handler method per write.
:class:`~meho_backplane.connectors.freeipa.connector.FreeipaConnector`
inherits them, so the dispatcher binds each one to the live connector (the
mixin path of
:func:`~meho_backplane.operations._handler_resolve.is_unbound_method`).
Every write waits until a second person approves; the dispatcher runs these
methods only after that.

An add-member body checks FreeIPA's ``failed`` and ``completed`` with
:func:`~meho_backplane.connectors.freeipa._write_common.check_done`, so a
member FreeIPA did not add fails the call. A sudo rule differs in one way:
FreeIPA keeps a user, group or host name it does not know as an external
name (``externaluser``, ``externalhost``) instead of failing, for example a
local user that is not in IPA. The sudo bodies return these names in
``external``, so a typo is visible.

Two writes have an approval preview:

* ``freeipa.hbacrule.disable`` names the rule. For ``allow_all``, FreeIPA's
  default rule that lets every user in everywhere, it also reads the other
  enabled access rules, so the approver sees who can still log in.
* ``freeipa.pwpolicy.set`` names the policy and the new values, and says
  when the change is for the global policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.freeipa._write_common import (
    as_list,
    check_done,
    description_option,
    result_dict,
)
from meho_backplane.connectors.freeipa.reads import MAX_LIMIT
from meho_backplane.connectors.freeipa.rpc import (
    HBACRULE_ADD_COMMAND,
    HBACRULE_ADD_HOST_COMMAND,
    HBACRULE_ADD_SERVICE_COMMAND,
    HBACRULE_ADD_USER_COMMAND,
    HBACRULE_DISABLE_COMMAND,
    HBACRULE_FIND_COMMAND,
    PWPOLICY_MOD_COMMAND,
    SUDOCMD_ADD_COMMAND,
    SUDORULE_ADD_ALLOW_COMMAND_COMMAND,
    SUDORULE_ADD_COMMAND,
    SUDORULE_ADD_HOST_COMMAND,
    SUDORULE_ADD_USER_COMMAND,
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
        ) -> FreeipaAnswer: ...

    _Base = _Connector
else:
    _Base = object

__all__ = [
    "ALLOW_ALL",
    "HBACRULE_DISABLE_OP_ID",
    "PWPOLICY_OPTIONS",
    "PWPOLICY_SET_OP_ID",
    "FreeipaAccessWriteOps",
    "hbacrule_disable_preview",
    "pwpolicy_set_preview",
]

HBACRULE_DISABLE_OP_ID: Final[str] = "freeipa.hbacrule.disable"
PWPOLICY_SET_OP_ID: Final[str] = "freeipa.pwpolicy.set"

#: FreeIPA's default access rule: every user may use every service on every host.
ALLOW_ALL: Final[str] = "allow_all"

#: The name FreeIPA gives the global password policy.
_GLOBAL_POLICY: Final[str] = "global_policy"

#: Each parameter of ``freeipa.pwpolicy.set`` that changes a value, and the
#: ``pwpolicy_mod`` option it is sent as.
PWPOLICY_OPTIONS: Final[dict[str, str]] = {
    "max_life_days": "krbmaxpwdlife",
    "min_life_hours": "krbminpwdlife",
    "history_size": "krbpwdhistorylength",
    "min_classes": "krbpwdmindiffchars",
    "min_length": "krbpwdminlength",
    "max_failures": "krbpwdmaxfailure",
    "failure_reset_seconds": "krbpwdfailurecountinterval",
    "lockout_seconds": "krbpwdlockoutduration",
    "priority": "cospriority",
}

_ALLOW_ALL_WARNING: Final[str] = (
    "allow_all is FreeIPA's default access rule: it lets every user use every service on every "
    "host. After this change, only the other enabled access rules let users in."
)

_NO_OTHER_RULE: Final[str] = (
    "No other access rule is enabled, so after this change no IPA user can log in to any host "
    "that uses IPA."
)

_GLOBAL_POLICY_WARNING: Final[str] = (
    "This changes the global password policy. It applies to every user whose groups have no "
    "password policy of their own."
)


def _external(
    entry: dict[str, Any], attribute: str, sent: list[str], prefix: str = ""
) -> list[str]:
    """The names in *sent* that FreeIPA keeps in *attribute*, because it knows no such object.

    FreeIPA keeps such a name in lower case. With *prefix*, the name also
    counts when it is kept with that prefix: sudo writes a group as
    ``%<group>``.
    """
    kept = {str(name).lower() for name in as_list(entry.get(attribute))}
    return [name for name in sent if {name.lower(), f"{prefix}{name}".lower()} & kept]


class FreeipaAccessWriteOps(_Base):
    """Handler methods of the sudo, access rule (HBAC) and password policy writes."""

    async def _create(
        self,
        operator: Operator,
        target: FreeipaTargetLike,
        command: str,
        name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Run an ``*_add`` command for one new object with an optional description."""
        answer = await self.call(target, operator, command, [name], description_option(params))
        return {"entry": result_dict(result_dict(answer.result).get("result"))}

    async def _add_members(
        self,
        operator: Operator,
        target: FreeipaTargetLike,
        command: str,
        params: dict[str, Any],
        members: dict[str, str],
    ) -> dict[str, Any]:
        """Run an add-member command on the rule ``params["name"]``.

        *members* maps each member parameter to the IPA option it is sent
        as. Fails unless FreeIPA added every member sent.
        """
        sent = {option: list(params.get(key) or []) for key, option in members.items()}
        options = {option: names or None for option, names in sent.items()}
        answer = await self.call(target, operator, command, [params["name"]], options)
        result = result_dict(answer.result)
        check_done(
            command,
            result.get("failed"),
            result.get("completed"),
            sum(len(names) for names in sent.values()),
        )
        return {"entry": result_dict(result.get("result")), "completed": result.get("completed")}

    async def sudocmd_create(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.sudocmd.create``: ``sudocmd_add``."""
        return await self._create(operator, target, SUDOCMD_ADD_COMMAND, params["command"], params)

    async def sudorule_create(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.sudorule.create``: ``sudorule_add``."""
        return await self._create(operator, target, SUDORULE_ADD_COMMAND, params["name"], params)

    async def sudorule_user_add(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.sudorule.user.add``: ``sudorule_add_user`` with users and user groups."""
        members = {"users": "user", "groups": "group"}
        answer = await self._add_members(
            operator, target, SUDORULE_ADD_USER_COMMAND, params, members
        )
        entry = answer["entry"]
        answer["external"] = [
            *_external(entry, "externaluser", params.get("users", [])),
            *_external(entry, "externaluser", params.get("groups", []), prefix="%"),
        ]
        return answer

    async def sudorule_host_add(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.sudorule.host.add``: ``sudorule_add_host`` with hosts and host groups."""
        members = {"hosts": "host", "hostgroups": "hostgroup"}
        answer = await self._add_members(
            operator, target, SUDORULE_ADD_HOST_COMMAND, params, members
        )
        answer["external"] = _external(answer["entry"], "externalhost", params.get("hosts", []))
        return answer

    async def sudorule_command_add(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.sudorule.command.add``: ``sudorule_add_allow_command`` with sudo commands."""
        members = {"commands": "sudocmd"}
        return await self._add_members(
            operator, target, SUDORULE_ADD_ALLOW_COMMAND_COMMAND, params, members
        )

    async def hbacrule_create(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hbacrule.create``: ``hbacrule_add``."""
        return await self._create(operator, target, HBACRULE_ADD_COMMAND, params["name"], params)

    async def hbacrule_user_add(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hbacrule.user.add``: ``hbacrule_add_user`` with users and user groups."""
        members = {"users": "user", "groups": "group"}
        return await self._add_members(operator, target, HBACRULE_ADD_USER_COMMAND, params, members)

    async def hbacrule_host_add(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hbacrule.host.add``: ``hbacrule_add_host`` with hosts and host groups."""
        members = {"hosts": "host", "hostgroups": "hostgroup"}
        return await self._add_members(operator, target, HBACRULE_ADD_HOST_COMMAND, params, members)

    async def hbacrule_service_add(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hbacrule.service.add``: ``hbacrule_add_service`` with services and groups."""
        members = {"services": "hbacsvc", "service_groups": "hbacsvcgroup"}
        return await self._add_members(
            operator, target, HBACRULE_ADD_SERVICE_COMMAND, params, members
        )

    async def hbacrule_disable(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.hbacrule.disable``: ``hbacrule_disable``.

        FreeIPA answers ``result: true`` also when the rule was disabled
        already: the rule is disabled either way.
        """
        answer = await self.call(target, operator, HBACRULE_DISABLE_COMMAND, [params["name"]])
        return {"disabled": result_dict(answer.result).get("value") or params["name"]}

    async def pwpolicy_set(
        self, operator: Operator, target: FreeipaTargetLike, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``freeipa.pwpolicy.set``: ``pwpolicy_mod`` for a group, or the global policy."""
        group = params.get("group")
        options = {option: params[key] for key, option in PWPOLICY_OPTIONS.items() if key in params}
        answer = await self.call(
            target, operator, PWPOLICY_MOD_COMMAND, [group] if group else [], options
        )
        return {"entry": result_dict(result_dict(answer.result).get("result"))}


async def hbacrule_disable_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The approval preview of ``freeipa.hbacrule.disable``.

    It names the rule. For ``allow_all`` it also reads the other enabled
    access rules with ``hbacrule_find``, and declines without a live
    connector (the ``preview_operation`` path, which calls no vendor
    system). When that read fails, the request still waits for approval, and
    the approver sees that the preview failed and why.
    """
    name = ctx.params.get("name")
    if not isinstance(name, str):
        return None
    if name.lower() != ALLOW_ALL:
        return {"rule": name}
    connector = ctx.connector_instance
    if not isinstance(connector, FreeipaAccessWriteOps) or ctx.target is None:
        return None
    options = {"ipaenabledflag": True, "pkey_only": True, "sizelimit": MAX_LIMIT}
    answer = await connector.call(ctx.target, ctx.operator, HBACRULE_FIND_COMMAND, [], options)
    result = result_dict(answer.result)
    others = [
        str(rule)
        for row in as_list(result.get("result"))
        if isinstance(row, dict)
        for rule in as_list(row.get("cn"))
        if str(rule).lower() != ALLOW_ALL
    ]
    return {
        "rule": name,
        "warning": _ALLOW_ALL_WARNING if others else f"{_ALLOW_ALL_WARNING} {_NO_OTHER_RULE}",
        "other_enabled_rules": others,
        "truncated": bool(result.get("truncated")),
    }


async def pwpolicy_set_preview(ctx: PreviewContext) -> dict[str, Any]:
    """The approval preview of ``freeipa.pwpolicy.set``: the policy and its new values.

    For the global policy it adds a warning. That is the policy without
    ``group``, and also with the group name ``global_policy`` in any case:
    FreeIPA builds the policy's LDAP name from the group name, which then
    names the global policy. It reads only the parameters, so it also runs
    without a live connector.
    """
    changes = {key: ctx.params[key] for key in PWPOLICY_OPTIONS if key in ctx.params}
    group = ctx.params.get("group")
    if isinstance(group, str) and group and group.lower() != _GLOBAL_POLICY:
        return {"policy": group, "changes": changes}
    return {"policy": _GLOBAL_POLICY, "warning": _GLOBAL_POLICY_WARNING, "changes": changes}


register_preview_builder(HBACRULE_DISABLE_OP_ID, hbacrule_disable_preview)
register_preview_builder(PWPOLICY_SET_OP_ID, pwpolicy_set_preview)

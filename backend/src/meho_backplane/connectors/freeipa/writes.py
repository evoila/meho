# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the FreeIPA host writes, and the approval preview of a host delete.

Every write waits until a second person approves. The dispatcher runs these
bodies only after that.

Every body checks FreeIPA's ``failed`` with
:func:`~meho_backplane.connectors.freeipa._write_common.check_done`, so a
change that did not happen is never reported as ok.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.freeipa._write_common import (
    as_list,
    check_done,
    description_option,
    result_dict,
)
from meho_backplane.connectors.freeipa.connector import FreeipaConnector
from meho_backplane.connectors.freeipa.rpc import (
    AUTOMEMBER_ADD_COMMAND,
    AUTOMEMBER_ADD_CONDITION_COMMAND,
    HOST_DEL_COMMAND,
    HOST_SHOW_COMMAND,
    HOSTGROUP_ADD_COMMAND,
    HOSTGROUP_ADD_MEMBER_COMMAND,
    HOSTGROUP_DEL_COMMAND,
)
from meho_backplane.operations._preview import PreviewContext, register_preview_builder

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.freeipa.session import FreeipaTargetLike

__all__ = [
    "HOST_DELETE_OP_ID",
    "automember_condition_add_impl",
    "automember_rule_create_impl",
    "host_delete_impl",
    "host_delete_preview",
    "hostgroup_create_impl",
    "hostgroup_delete_impl",
    "hostgroup_member_add_impl",
]

HOST_DELETE_OP_ID: Final[str] = "freeipa.host.delete"

#: The automember rules of this connector fill host groups, never user groups.
_HOSTGROUP_TYPE: Final[str] = "hostgroup"


async def hostgroup_create_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.hostgroup.create``: ``hostgroup_add``."""
    answer = await connector.call(
        target, operator, HOSTGROUP_ADD_COMMAND, [params["name"]], description_option(params)
    )
    return {"entry": result_dict(result_dict(answer.result).get("result"))}


async def hostgroup_member_add_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.hostgroup.member.add``: ``hostgroup_add_member`` with hosts and host groups."""
    hosts = list(params.get("hosts") or [])
    hostgroups = list(params.get("hostgroups") or [])
    options = {"host": hosts or None, "hostgroup": hostgroups or None}
    answer = await connector.call(
        target, operator, HOSTGROUP_ADD_MEMBER_COMMAND, [params["name"]], options
    )
    result = result_dict(answer.result)
    check_done(
        HOSTGROUP_ADD_MEMBER_COMMAND,
        result.get("failed"),
        result.get("completed"),
        len(hosts) + len(hostgroups),
    )
    return {"entry": result_dict(result.get("result")), "completed": result.get("completed")}


async def automember_rule_create_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.automember.rule.create``: ``automember_add`` for a host group.

    FreeIPA names the rule after the host group it fills.
    """
    options = {"type": _HOSTGROUP_TYPE, **description_option(params)}
    answer = await connector.call(
        target, operator, AUTOMEMBER_ADD_COMMAND, [params["hostgroup"]], options
    )
    return {"entry": result_dict(result_dict(answer.result).get("result"))}


async def automember_condition_add_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.automember.condition.add``: ``automember_add_condition`` on a host group rule."""
    inclusive = list(params.get("inclusive_regex") or [])
    exclusive = list(params.get("exclusive_regex") or [])
    options = {
        "type": _HOSTGROUP_TYPE,
        "key": params["key"],
        "automemberinclusiveregex": inclusive or None,
        "automemberexclusiveregex": exclusive or None,
    }
    answer = await connector.call(
        target, operator, AUTOMEMBER_ADD_CONDITION_COMMAND, [params["hostgroup"]], options
    )
    result = result_dict(answer.result)
    check_done(
        AUTOMEMBER_ADD_CONDITION_COMMAND,
        result.get("failed"),
        result.get("completed"),
        len(inclusive) + len(exclusive),
    )
    return {"entry": result_dict(result.get("result")), "completed": result.get("completed")}


async def _delete(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    command: str,
    name: str,
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a ``*_del`` command for one object; return what FreeIPA deleted, and its warnings.

    The warnings are FreeIPA's messages, for example that it found no DNS
    records to remove.
    """
    answer = await connector.call(target, operator, command, [name], options)
    result = result_dict(answer.result)
    check_done(command, result_dict(result.get("result")).get("failed"))
    warnings = [
        str(message["message"])
        for message in as_list(result.get("messages"))
        if isinstance(message, dict) and message.get("message")
    ]
    return {"deleted": as_list(result.get("value")), "warnings": warnings}


async def host_delete_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.host.delete``: ``host_del``, with ``updatedns`` when asked."""
    options = {"updatedns": bool(params.get("updatedns", False))}
    return await _delete(connector, operator, target, HOST_DEL_COMMAND, params["fqdn"], options)


async def hostgroup_delete_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.hostgroup.delete``: ``hostgroup_del``."""
    return await _delete(connector, operator, target, HOSTGROUP_DEL_COMMAND, params["name"])


async def host_delete_preview(ctx: PreviewContext) -> dict[str, Any] | None:
    """The approval preview of ``freeipa.host.delete``: the host and its host groups.

    Reads the host with ``host_show``. Declines without a live connector (the
    ``preview_operation`` path, which calls no vendor system). When the read
    fails, for example because no such host exists, the request still waits
    for approval, and the approver sees that the preview failed and why.
    """
    connector = ctx.connector_instance
    fqdn = ctx.params.get("fqdn")
    if not isinstance(connector, FreeipaConnector) or ctx.target is None:
        return None
    if not isinstance(fqdn, str):
        return None
    answer = await connector.call(ctx.target, ctx.operator, HOST_SHOW_COMMAND, [fqdn])
    entry = result_dict(result_dict(answer.result).get("result"))
    return {
        "host": fqdn,
        "hostgroups": as_list(entry.get("memberof_hostgroup")),
        "indirect_hostgroups": as_list(entry.get("memberofindirect_hostgroup")),
        "updatedns": bool(ctx.params.get("updatedns", False)),
    }


register_preview_builder(HOST_DELETE_OP_ID, host_delete_preview)

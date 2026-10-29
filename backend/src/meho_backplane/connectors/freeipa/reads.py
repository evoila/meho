# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the FreeIPA reads: one IPA command each.

Each function takes the connector, the operator, the target and the
validated params, runs one command through
:meth:`~meho_backplane.connectors.freeipa.connector.FreeipaConnector.call`
and returns a dict:

* a list read (``*_find``) returns ``{rows, total, truncated}``, so MEHO can
  keep a long list behind a link to the stored result;
* a read of one object (``*_show``) returns ``{entry}``.

Rows and entries are FreeIPA's own objects with the default attributes. Most
values are lists, also when they hold one value. Members are in keys named
``<attribute>_<object type>``, for example ``member_host``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.freeipa.rpc import (
    API_VERSION,
    GROUP_FIND_COMMAND,
    GROUP_SHOW_COMMAND,
    HBACRULE_FIND_COMMAND,
    HBACRULE_SHOW_COMMAND,
    HBACTEST_COMMAND,
    HOST_FIND_COMMAND,
    HOST_SHOW_COMMAND,
    HOSTGROUP_FIND_COMMAND,
    HOSTGROUP_SHOW_COMMAND,
    PING_COMMAND,
    PWPOLICY_SHOW_COMMAND,
    ROLE_FIND_COMMAND,
    ROLE_SHOW_COMMAND,
    SUDORULE_FIND_COMMAND,
    SUDORULE_SHOW_COMMAND,
    USER_FIND_COMMAND,
    USER_SHOW_COMMAND,
    parse_ping_summary,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.freeipa.connector import FreeipaConnector
    from meho_backplane.connectors.freeipa.session import FreeipaTargetLike

__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "about_impl",
    "group_list_impl",
    "group_show_impl",
    "hbac_test_impl",
    "hbacrule_list_impl",
    "hbacrule_show_impl",
    "host_list_impl",
    "host_show_impl",
    "hostgroup_list_impl",
    "hostgroup_show_impl",
    "pwpolicy_show_impl",
    "role_list_impl",
    "role_show_impl",
    "sudorule_list_impl",
    "sudorule_show_impl",
    "user_list_impl",
    "user_show_impl",
]

#: Rows a list read returns when the caller gives no ``limit``.
DEFAULT_LIMIT: Final[int] = 100

#: The largest ``limit`` a list read accepts. 0 (no limit) is never sent.
MAX_LIMIT: Final[int] = 2000


def _result_dict(result: Any) -> dict[str, Any]:
    return result if isinstance(result, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


async def _find(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    command: str,
    params: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a ``*_find`` command and return ``{rows, total, truncated}``."""
    text = params.get("text")
    answer = await connector.call(
        target,
        operator,
        command,
        [text] if text else [],
        {"sizelimit": params.get("limit", DEFAULT_LIMIT), **(options or {})},
    )
    result = _result_dict(answer.result)
    rows = [row for row in _as_list(result.get("result")) if isinstance(row, dict)]
    return {"rows": rows, "total": len(rows), "truncated": bool(result.get("truncated"))}


async def _show(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    command: str,
    args: list[Any],
) -> dict[str, Any]:
    """Run a ``*_show`` command and return ``{entry}``."""
    answer = await connector.call(target, operator, command, args)
    entry = _result_dict(answer.result).get("result")
    return {"entry": entry if isinstance(entry, dict) else {}}


async def about_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.about``: ``ping``, and the IPA principal of MEHO's login."""
    del params
    answer = await connector.call(target, operator, PING_COMMAND)
    summary = _result_dict(answer.result).get("summary")
    server_version, api_version = parse_ping_summary(summary)
    return {
        "server_version": server_version or answer.server_version,
        "api_version": api_version,
        "connector_api_version": API_VERSION,
        "principal": answer.principal,
    }


async def host_list_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.host.list``: ``host_find``, optionally only the hosts of one host group."""
    hostgroup = params.get("hostgroup")
    options = {"in_hostgroup": [hostgroup]} if hostgroup else None
    return await _find(connector, operator, target, HOST_FIND_COMMAND, params, options)


async def host_show_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.host.show``: ``host_show``."""
    return await _show(connector, operator, target, HOST_SHOW_COMMAND, [params["fqdn"]])


async def hostgroup_list_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.hostgroup.list``: ``hostgroup_find``."""
    return await _find(connector, operator, target, HOSTGROUP_FIND_COMMAND, params)


async def hostgroup_show_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.hostgroup.show``: ``hostgroup_show``."""
    return await _show(connector, operator, target, HOSTGROUP_SHOW_COMMAND, [params["name"]])


async def user_list_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.user.list``: ``user_find``."""
    return await _find(connector, operator, target, USER_FIND_COMMAND, params)


async def user_show_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.user.show``: ``user_show``."""
    return await _show(connector, operator, target, USER_SHOW_COMMAND, [params["uid"]])


async def group_list_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.group.list``: ``group_find``."""
    return await _find(connector, operator, target, GROUP_FIND_COMMAND, params)


async def group_show_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.group.show``: ``group_show``."""
    return await _show(connector, operator, target, GROUP_SHOW_COMMAND, [params["name"]])


async def role_list_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.role.list``: ``role_find``."""
    return await _find(connector, operator, target, ROLE_FIND_COMMAND, params)


async def role_show_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.role.show``: ``role_show``."""
    return await _show(connector, operator, target, ROLE_SHOW_COMMAND, [params["name"]])


async def sudorule_list_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.sudorule.list``: ``sudorule_find``."""
    return await _find(connector, operator, target, SUDORULE_FIND_COMMAND, params)


async def sudorule_show_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.sudorule.show``: ``sudorule_show``."""
    return await _show(connector, operator, target, SUDORULE_SHOW_COMMAND, [params["name"]])


async def hbacrule_list_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.hbacrule.list``: ``hbacrule_find``."""
    return await _find(connector, operator, target, HBACRULE_FIND_COMMAND, params)


async def hbacrule_show_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.hbacrule.show``: ``hbacrule_show``."""
    return await _show(connector, operator, target, HBACRULE_SHOW_COMMAND, [params["name"]])


async def hbac_test_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.hbac.test``: ``hbactest`` for one user, host and service.

    FreeIPA's ``value`` (access granted or not) is returned as ``granted``.
    Its ``warning`` and ``error`` lists are returned as ``warnings`` and
    ``errors``, so they cannot be mistaken for a failed call.
    """
    options: dict[str, Any] = {
        "user": params["user"],
        "targethost": params["host"],
        "service": params["service"],
    }
    if params.get("rules"):
        options["rules"] = list(params["rules"])
    answer = await connector.call(target, operator, HBACTEST_COMMAND, [], options)
    result = _result_dict(answer.result)
    return {
        "granted": result.get("value") is True,
        "matched": _as_list(result.get("matched")),
        "notmatched": _as_list(result.get("notmatched")),
        "warnings": _as_list(result.get("warning")),
        "errors": _as_list(result.get("error")),
    }


async def pwpolicy_show_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.pwpolicy.show``: ``pwpolicy_show`` for a group, or the global policy."""
    group = params.get("group")
    return await _show(connector, operator, target, PWPOLICY_SHOW_COMMAND, [group] if group else [])

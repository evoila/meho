# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bodies of the FreeIPA host writes, and the approval preview of a host delete.

Every write waits until a second person approves. The dispatcher runs these
bodies only after that.

FreeIPA answers many writes with success and lists what it could not do in
``failed``:

* an add-member command lists each member with FreeIPA's reason, by
  attribute and object type:
  ``{"member": {"host": [["web99.example.test", "no such entry"]]}}``;
* ``automember_add_condition`` lists conditions:
  ``{"failed": {"automemberinclusiveregex": ["fqdn=^web"]}}``;
* a delete lists names: ``{"failed": ["web01.example.test"]}``.

Every body checks ``failed`` and raises when it is not empty, so a change
that did not happen is never reported as ok. An add-member or add-condition
body also compares FreeIPA's ``completed`` count with the number of values
sent: FreeIPA does not list a condition that the rule already has in
``failed``, it only counts it as not added.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meho_backplane.connectors.freeipa.connector import FreeipaConnector
from meho_backplane.connectors.freeipa.rpc import (
    AUTOMEMBER_ADD_COMMAND,
    AUTOMEMBER_ADD_CONDITION_COMMAND,
    HOST_DEL_COMMAND,
    HOST_SHOW_COMMAND,
    HOSTGROUP_ADD_COMMAND,
    HOSTGROUP_ADD_MEMBER_COMMAND,
    HOSTGROUP_DEL_COMMAND,
    FreeipaCommandError,
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

#: The name of the error a write raises when FreeIPA did not do all of it.
_NOT_DONE: Final[str] = "NotDone"


def _result_dict(result: Any) -> dict[str, Any]:
    return result if isinstance(result, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _failures(failed: Any, kind: str = "") -> list[str]:
    """Every entry of FreeIPA's ``failed``, as text: ``"host web99.example.test: no such entry"``.

    The innermost key names the kind of the entry. An entry is a name with
    FreeIPA's reason, or a name alone.
    """
    if isinstance(failed, dict):
        return [text for key, value in failed.items() for text in _failures(value, str(key))]
    texts = []
    for entry in _as_list(failed):
        if isinstance(entry, list) and len(entry) == 2:
            texts.append(f"{kind} {entry[0]}: {entry[1]}".strip())
        else:
            texts.append(f"{kind} {entry}".strip())
    return texts


def _check_done(command: str, failed: Any, completed: Any = None, sent: int | None = None) -> None:
    """Raise when FreeIPA did not do all of the change.

    It did not when *failed* has an entry, or when *sent* is given and
    FreeIPA's *completed* count is lower.
    """
    failures = _failures(failed)
    done = completed if isinstance(completed, int) else 0
    if not failures and (sent is None or done >= sent):
        return
    parts = [f"FreeIPA did {done} of {sent} changes."] if sent is not None else []
    if failures:
        parts.append("Not done: " + "; ".join(failures) + ".")
    else:
        parts.append("FreeIPA did not say which; a value that is already there is not added again.")
    raise FreeipaCommandError(command, name=_NOT_DONE, code=None, message=" ".join(parts))


def _description(params: dict[str, Any]) -> dict[str, Any]:
    description = params.get("description")
    return {"description": description} if description else {}


async def hostgroup_create_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.hostgroup.create``: ``hostgroup_add``."""
    answer = await connector.call(
        target, operator, HOSTGROUP_ADD_COMMAND, [params["name"]], _description(params)
    )
    return {"entry": _result_dict(_result_dict(answer.result).get("result"))}


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
    result = _result_dict(answer.result)
    _check_done(
        HOSTGROUP_ADD_MEMBER_COMMAND,
        result.get("failed"),
        result.get("completed"),
        len(hosts) + len(hostgroups),
    )
    return {"entry": _result_dict(result.get("result")), "completed": result.get("completed")}


async def automember_rule_create_impl(
    connector: FreeipaConnector,
    operator: Operator,
    target: FreeipaTargetLike,
    params: dict[str, Any],
) -> dict[str, Any]:
    """``freeipa.automember.rule.create``: ``automember_add`` for a host group.

    FreeIPA names the rule after the host group it fills.
    """
    options = {"type": _HOSTGROUP_TYPE, **_description(params)}
    answer = await connector.call(
        target, operator, AUTOMEMBER_ADD_COMMAND, [params["hostgroup"]], options
    )
    return {"entry": _result_dict(_result_dict(answer.result).get("result"))}


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
    result = _result_dict(answer.result)
    _check_done(
        AUTOMEMBER_ADD_CONDITION_COMMAND,
        result.get("failed"),
        result.get("completed"),
        len(inclusive) + len(exclusive),
    )
    return {"entry": _result_dict(result.get("result")), "completed": result.get("completed")}


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
    result = _result_dict(answer.result)
    _check_done(command, _result_dict(result.get("result")).get("failed"))
    warnings = [
        str(message["message"])
        for message in _as_list(result.get("messages"))
        if isinstance(message, dict) and message.get("message")
    ]
    return {"deleted": _as_list(result.get("value")), "warnings": warnings}


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
    entry = _result_dict(_result_dict(answer.result).get("result"))
    return {
        "host": fqdn,
        "hostgroups": _as_list(entry.get("memberof_hostgroup")),
        "indirect_hostgroups": _as_list(entry.get("memberofindirect_hostgroup")),
        "updatedns": bool(ctx.params.get("updatedns", False)),
    }


register_preview_builder(HOST_DELETE_OP_ID, host_delete_preview)

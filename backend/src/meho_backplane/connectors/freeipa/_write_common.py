# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared pieces of the FreeIPA write bodies: reading the answer, and the check that all was done.

FreeIPA answers many writes with success and lists what it could not do in
``failed``:

* an add-member command lists each member with FreeIPA's reason, by
  attribute and object type:
  ``{"member": {"host": [["web99.example.test", "no such entry"]]}}``;
* ``automember_add_condition`` lists conditions:
  ``{"failed": {"automemberinclusiveregex": ["fqdn=^web"]}}``;
* a delete lists names: ``{"failed": ["web01.example.test"]}``.

:func:`check_done` raises when ``failed`` is not empty, so a change that did
not happen is never reported as ok. An add-member or add-condition body also
gives it the number of values sent: FreeIPA does not list a condition that
the rule already has in ``failed``, it only counts it as not added.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol

from meho_backplane.connectors.freeipa.rpc import FreeipaCommandError

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.freeipa.rpc import FreeipaAnswer
    from meho_backplane.connectors.freeipa.session import FreeipaTargetLike

    class _Caller(Protocol):
        """What :func:`run_delete` uses of the connector."""

        async def call(
            self,
            target: FreeipaTargetLike,
            operator: Operator,
            command: str,
            args: list[Any] | None = None,
            options: dict[str, Any] | None = None,
        ) -> FreeipaAnswer:
            """Run one IPA command."""


__all__ = ["as_list", "check_done", "description_option", "result_dict", "run_delete"]

#: The name of the error a write raises when FreeIPA did not do all of it.
_NOT_DONE: Final[str] = "NotDone"


def result_dict(result: Any) -> dict[str, Any]:
    return result if isinstance(result, dict) else {}


def as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _failures(failed: Any, kind: str = "") -> list[str]:
    """Every entry of FreeIPA's ``failed``, as text: ``"host web99.example.test: no such entry"``.

    The innermost key names the kind of the entry. An entry is a name with
    FreeIPA's reason, or a name alone.
    """
    if isinstance(failed, dict):
        return [text for key, value in failed.items() for text in _failures(value, str(key))]
    texts = []
    for entry in as_list(failed):
        if isinstance(entry, list) and len(entry) == 2:
            texts.append(f"{kind} {entry[0]}: {entry[1]}".strip())
        else:
            texts.append(f"{kind} {entry}".strip())
    return texts


def check_done(command: str, failed: Any, completed: Any = None, sent: int | None = None) -> None:
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


def description_option(params: dict[str, Any]) -> dict[str, Any]:
    """The ``description`` option, when the caller gave one."""
    description = params.get("description")
    return {"description": description} if description else {}


async def run_delete(
    connector: _Caller,
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

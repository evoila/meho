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

from typing import Any, Final

from meho_backplane.connectors.freeipa.rpc import FreeipaCommandError

__all__ = ["as_list", "check_done", "description_option", "result_dict"]

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

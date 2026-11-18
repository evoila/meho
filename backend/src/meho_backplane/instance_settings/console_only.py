# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tenant policies that a settings file changes only in the web console.

Two tenant policies decide how far an approval reaches or whether a gate
runs. A settings import changes them in the loosening direction only for a
person in the web console
(:func:`~meho_backplane.api.v1._wait_for_approval.runs_at_once`):

* ``run_approval_target_kinds`` turned on: one run approval may then cover a
  whole target kind (a product) in place of one target
  (:mod:`meho_backplane.operations.run_approvals`).
* ``announce_gate_enabled`` turned off: callers that are not a person then
  write without an announce claim (:mod:`meho_backplane.broadcast.announce_gate`).

Any other caller (the CLI, a script, an AI tool with a person's token) gets
403 for the whole file and MEHO writes nothing. The opposite direction
(target kinds off, gate on) narrows what runs, so it applies with a token.
The preview works with a token and names each such change in ``warnings``.

The other policies are not in this list. ``flight_recorder`` and
``mail_recipient_allowlist`` have their own REST routes, which write at once
for a token too. ``knowledge_wiki_collection`` names a docs collection that
search also reads; each read still checks the caller's own entitlement to
that collection, so it does not change who may read what.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from meho_backplane.api.v1._wait_for_approval import runs_at_once
from meho_backplane.auth.operator import Operator

__all__ = [
    "CONSOLE_ONLY_POLICIES",
    "console_only_changes",
    "console_only_refusal",
    "console_only_warnings",
]

#: Policy key -> (the value that loosens it, what that value does).
CONSOLE_ONLY_POLICIES: Final[dict[str, tuple[bool, str]]] = {
    "run_approval_target_kinds": (
        True,
        "lets one run approval cover a whole target kind",
    ),
    "announce_gate_enabled": (
        False,
        "turns the announce gate off",
    ),
}


def console_only_changes(index: int, before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The file paths of the console-only changes from *before* to *after* policies."""
    paths: list[str] = []
    for key, (loose, _) in CONSOLE_ONLY_POLICIES.items():
        if bool(after.get(key)) == loose and bool(before.get(key)) != loose:
            paths.append(f"spec.tenants[{index}].policies.{key}")
    return paths


def _what(path: str) -> str:
    return CONSOLE_ONLY_POLICIES[path.rsplit(".", 1)[1]][1]


def console_only_warnings(operator: Operator, paths: Iterable[str]) -> list[str]:
    """One preview sentence per path, for a caller that is not in the web console."""
    if runs_at_once(operator):
        return []
    return [
        f"`{path}` {_what(path)}. MEHO applies this change only for a person in the "
        "web console; an import with a token is refused (403) and writes nothing."
        for path in paths
    ]


def console_only_refusal(operator: Operator, paths: list[str]) -> str | None:
    """The 403 reason when *operator* may not apply *paths*, else ``None``."""
    if not paths or runs_at_once(operator):
        return None
    named = ", ".join(f"`{path}` ({_what(path)})" for path in paths)
    return (
        f"A settings file that changes {named} is applied in the web console. "
        "A call with a token cannot widen approvals or turn a gate off. "
        "MEHO wrote nothing from this file."
    )

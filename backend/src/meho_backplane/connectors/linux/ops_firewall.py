# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Firewall inspection read verb for :class:`LinuxSshConnector` (#3360).

``linux.firewall.show`` (group ``firewall``) -- a ``safe``, read-only dump
of the host's live firewall ruleset: ``nft list ruleset`` when ``nft`` is
present, falling back to ``iptables-save`` on legacy hosts. Takes no
operator parameters (a fixed command), so no operator value reaches the
shell. Returns ``{rows, total, backend}`` where ``rows`` is the ruleset
lines and ``backend`` is a ``nftables`` / ``iptables`` / ``none``
discriminator; the row list spills to a ``result_query`` handle above the
JSONFlux threshold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors.linux.ops import (
    SSH_TRANSPORT_NOTE,
    LinuxOp,
    normalise_json_rows,
)

if TYPE_CHECKING:
    from meho_backplane.connectors.linux.connector import LinuxSshConnector, Target

__all__ = [
    "BACKEND_MARKER",
    "FIREWALL_OPS",
    "FIREWALL_SHOW_COMMAND",
    "linux_firewall_show",
    "parse_firewall_output",
]

#: Prefix the command prints on its first line to declare which backend
#: produced the ruleset, so the parser attributes the rows correctly.
BACKEND_MARKER: str = "MEHO_BACKEND="

#: The fixed, operator-input-free firewall dump command. Prefers ``nft``
#: (the modern default), falls back to ``iptables-save``, and reports
#: ``none`` when neither is installed. Every branch is read-only.
FIREWALL_SHOW_COMMAND: str = (
    f"if command -v nft >/dev/null 2>&1; then printf '%snftables\\n' {BACKEND_MARKER!r}; "
    "nft list ruleset 2>/dev/null; "
    "elif command -v iptables-save >/dev/null 2>&1; then "
    f"printf '%siptables\\n' {BACKEND_MARKER!r}; "
    "iptables-save 2>/dev/null; "
    f"else printf '%snone\\n' {BACKEND_MARKER!r}; fi"
)


def parse_firewall_output(stdout: str) -> dict[str, Any]:
    """Parse the firewall dump into ``{rows, total, backend}``.

    The first line carries the ``BACKEND_MARKER`` prefix naming the backend
    (``nftables`` / ``iptables`` / ``none``); the remaining lines are the
    ruleset rows. An empty ruleset (default-accept host, or ``none``
    backend) yields ``rows=[]``.
    """
    lines = stdout.splitlines()
    backend = "none"
    rows: list[str] = []
    for index, line in enumerate(lines):
        if index == 0 and line.startswith(BACKEND_MARKER):
            backend = line[len(BACKEND_MARKER) :].strip() or "none"
            continue
        rows.append(line)
    return {**normalise_json_rows(rows), "backend": backend}


async def linux_firewall_show(
    connector: LinuxSshConnector,
    target: Target,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``linux.firewall.show`` -- dump the live firewall ruleset."""
    del params  # declared empty in schema; intentionally ignored
    proc = await connector._run_command(target, FIREWALL_SHOW_COMMAND, operator=operator)
    raw = (proc.stdout or "") if hasattr(proc, "stdout") else ""
    stdout = raw if isinstance(raw, str) else ""
    return parse_firewall_output(stdout)


_FIREWALL_SHOW_OP = LinuxOp(
    op_id="linux.firewall.show",
    handler_attr="firewall_show",
    summary="Dump the host's live firewall ruleset (nftables or iptables).",
    description=(
        "Runs ``nft list ruleset`` (or ``iptables-save`` on legacy hosts) "
        "and returns ``{rows, total, backend}`` where ``rows`` is the "
        "ruleset lines and ``backend`` names which tool produced them "
        "(``nftables`` / ``iptables`` / ``none``). Takes no parameters. "
        "Read-only -- it never loads, flushes, or edits a ruleset. Above "
        "the JSONFlux threshold the rows spill to a handle paged with "
        "``result_query(handle_id, offset, limit)``."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    response_schema={
        "type": "object",
        "properties": {
            "rows": {"type": "array", "items": {"type": "string"}},
            "total": {"type": "integer"},
            "backend": {"type": "string"},
        },
        "required": ["rows", "total", "backend"],
        "additionalProperties": True,
    },
    group_key="firewall",
    tags=("read-only", "firewall", "linux"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call to confirm the default-deny firewall ruleset the "
            "first-boot script was meant to load is actually present -- "
            "without changing it. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "``{rows, total, backend}``; rows are ruleset lines. Large "
            "rulesets spill to a ``result_query`` handle."
        ),
    },
)


#: The firewall read tier composed onto ``LINUX_OPS``.
FIREWALL_OPS: tuple[LinuxOp, ...] = (_FIREWALL_SHOW_OP,)

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

A login that is not root cannot read the ruleset. Then the read runs under
sudo, through the shared sudo path
(:mod:`~meho_backplane.connectors.linux._sudo`), with the sudo password of
the target's secret (an empty one when the secret holds none, for a login
that may use sudo without a password). A read that fails raises
:class:`LinuxFirewallReadError`; it never answers "no rules".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import strip_credential_value
from meho_backplane.connectors.linux._sudo import run_remote_bash_with_sudo
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
    "LinuxFirewallReadError",
    "linux_firewall_show",
    "parse_firewall_output",
]

#: Prefix the command prints on its first line to declare which backend
#: produced the ruleset, so the parser attributes the rows correctly.
BACKEND_MARKER: str = "MEHO_BACKEND="

#: The fixed, operator-input-free firewall dump command. Prefers ``nft``
#: (the modern default), falls back to ``iptables-save``, and reports
#: ``none`` when neither is installed. Every branch is read-only. The exit
#: status is the one of the read tool, and its error text is kept, so a failed
#: read is never taken for an empty ruleset.
FIREWALL_SHOW_COMMAND: str = (
    f"if command -v nft >/dev/null 2>&1; then printf '%snftables\\n' {BACKEND_MARKER!r}; "
    "nft list ruleset; "
    "elif command -v iptables-save >/dev/null 2>&1; then "
    f"printf '%siptables\\n' {BACKEND_MARKER!r}; "
    "iptables-save; "
    f"else printf '%snone\\n' {BACKEND_MARKER!r}; fi"
)

#: Words in the error text of a read that lacked the rights to run.
_PERMISSION_WORDS: tuple[str, ...] = (
    "operation not permitted",
    "permission denied",
    "a password is required",
    "sudo:",
)

_ERROR_TEXT_CAP: int = 300


class LinuxFirewallReadError(RuntimeError):
    """The firewall ruleset could not be read.

    The message starts with ``permission_denied`` when the login lacked the
    rights, else with ``read_failed``. The dispatcher turns it into a
    connector error, so a failed read never looks like an empty ruleset.
    """


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
    secret = await connector._resolve_secret(target, operator)
    username = strip_credential_value(secret.get("username", "root"))
    if username == "root":
        proc = await connector._run_command(target, FIREWALL_SHOW_COMMAND, operator=operator)
    else:
        # No password in the secret: send an empty one. sudo does not read it
        # when the login may use sudo without a password; else it refuses, and
        # the refusal is reported as permission_denied below.
        sudo_password = secret.get("sudo_password") or secret.get("password") or ""
        proc = await run_remote_bash_with_sudo(
            connector,
            target,
            FIREWALL_SHOW_COMMAND,
            operator=operator,
            sudo_password=strip_credential_value(sudo_password),
        )
    stdout = _text(proc, "stdout")
    exit_status = getattr(proc, "exit_status", None)
    if exit_status != 0:
        stderr = _text(proc, "stderr")
        raise LinuxFirewallReadError(_read_error_message(stdout, stderr, exit_status))
    return parse_firewall_output(stdout)


def _text(proc: Any, field: str) -> str:
    raw = getattr(proc, field, "") or ""
    return raw if isinstance(raw, str) else ""


def _read_error_message(stdout: str, stderr: str, exit_status: Any) -> str:
    """Build the error text of a failed read: the kind, the tool and its words."""
    backend = parse_firewall_output(stdout)["backend"] if stdout else "unknown"
    detail = " ".join(stderr.split())[:_ERROR_TEXT_CAP]
    if any(word in stderr.lower() for word in _PERMISSION_WORDS):
        kind = "permission_denied"
        hint = "The login needs root, or sudo rights for nft or iptables-save."
    else:
        kind = "read_failed"
        hint = f"The read ended with exit status {exit_status}."
    message = f"{kind}: could not read the firewall rules (backend {backend}). {hint}"
    return f"{message} {detail}".strip() if detail else message


_FIREWALL_SHOW_OP = LinuxOp(
    op_id="linux.firewall.show",
    handler_attr="firewall_show",
    summary="Dump the host's live firewall ruleset (nftables or iptables).",
    description=(
        "Runs ``nft list ruleset`` (or ``iptables-save`` on legacy hosts) "
        "and returns ``{rows, total, backend}`` where ``rows`` is the "
        "ruleset lines and ``backend`` names which tool produced them "
        "(``nftables`` / ``iptables`` / ``none``). Takes no parameters. "
        "Read-only -- it never loads, flushes, or edits a ruleset. A login "
        "that is not root reads through sudo. A failed read is an error "
        "(``permission_denied`` or ``read_failed``), never an empty list. "
        "Above the JSONFlux threshold the rows spill to a handle paged with "
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
            "rulesets spill to a ``result_query`` handle. ``backend: none`` "
            "means the host has neither nft nor iptables-save. A read that "
            "fails is an error, not empty rows."
        ),
    },
)


#: The firewall read tier composed onto ``LINUX_OPS``.
FIREWALL_OPS: tuple[LinuxOp, ...] = (_FIREWALL_SHOW_OP,)

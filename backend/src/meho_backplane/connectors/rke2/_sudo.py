# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Safe-by-construction ``sudo`` primitive for the RKE2 SSH write ops.

RKE2's node-OS maintenance verbs (``rke2 token rotate`` and the T3/T4
service/etcd ops) run as root, but the connector authenticates over SSH as
an unprivileged login user and elevates via ``sudo -S``. This module is the
one place the sudo password reaches the wire, and it is shaped so the
password can never leak into the remote ``argv`` (``ps`` /
``/proc/<pid>/cmdline``), the remote shell-history file, or a local log
event.

The wire shape is the same one bind9 hardened after the #697 stdio-buffer
regression (``bind9/connector.py``): the caller passes *script* and
*sudo_password* as **separate** arguments and cannot express a mis-ordered
payload. :func:`build_sudo_bash_remote_cmd` renders a remote command whose
only caller-derived interpolation is the script's UTF-8 **byte count** (an
integer); the script body and the password ride on stdin. ``head -c <N>``
reads exactly the script bytes into a ``umask 077`` temp file via unbuffered
``read(2)``, then ``sudo -S`` reads the password from what remains on stdin
(EOF immediately after the password newline, so sudo's buffered stdio has
nothing past the password line to prefetch-and-swallow), and ``bash "$f"``
runs the script body from the temp file rather than stdin.

Each RKE2 SSH connector family carries its own copy of this primitive
(holodeck carries ``_pwsh.py``; bind9 carries its sudo helper inline) rather
than sharing a single module, so a change to one connector's elevation shape
cannot silently alter another's. The bytes are identical to the bind9
reference by design -- the safety analysis is load-bearing and must not
drift between the two families.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import asyncssh
import structlog

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.adapters.ssh import SshConnector

__all__ = ["build_sudo_bash_remote_cmd", "run_remote_bash_with_sudo"]

_log = structlog.get_logger(__name__)


def build_sudo_bash_remote_cmd(script_byte_len: int) -> str:
    """Render the remote command for :func:`run_remote_bash_with_sudo`.

    Constant boilerplate parameterised only by *script_byte_len* -- an
    integer derived from the caller's script bytes. Neither the script nor
    the sudo password is interpolated into ``argv``. Module-level so the
    unit suite (which mocks ``conn.run``) can assert against the exact
    rendered shape without duplicating it.
    """
    return (
        f"set -e; umask 077; f=$(mktemp); "
        f'trap "rm -f $f" EXIT; '
        f'head -c {script_byte_len} > "$f"; '
        f'sudo -S -p "" bash "$f"'
    )


async def run_remote_bash_with_sudo(
    connector: SshConnector,
    target: Any,
    script: str,
    *,
    operator: Operator | None = None,
    sudo_password: str,
    timeout: float = 60.0,
) -> asyncssh.SSHCompletedProcess:
    """Run *script* on *target* under ``sudo`` without leaking the password.

    Free-function form of the bind9 ``_remote_bash_with_sudo`` primitive,
    taking the connector explicitly (the holodeck ``_run_text`` convention)
    so the RKE2 write handlers can call it from ``ops_write`` without a
    connector-method shim per op. The connection is drawn from the
    connector's SSH pool via :meth:`SshConnector._connect`; the password is
    streamed as the last stdin line after the exact script bytes.

    Parameters
    ----------
    connector
        The RKE2 SSH connector whose pooled connection runs the script.
    target
        The :class:`Target` the SSH connection is keyed to.
    script
        The bash script body to run under sudo. Must **not** embed the sudo
        password -- that is streamed separately via *sudo_password*.
    operator
        Forwarded to :meth:`SshConnector._connect` for the operator-context
        Vault credential read on a pool miss (#2155).
    sudo_password
        The password ``sudo -S`` reads from stdin. Streamed as the final
        stdin line; never in ``argv``, never logged.
    timeout
        Wall-clock timeout in seconds.

    Raises
    ------
    ValueError
        *sudo_password* contains ``\\n`` / ``\\r`` / ``\\x00`` -- a control
        character would terminate sudo's password read early and feed the
        trailing bytes to ``bash`` as commands, the exact leak this shape
        exists to make unrepresentable. Checked **before** ``_connect`` so
        an invalid credential never opens a wire connection.
    """
    if any(ch in sudo_password for ch in ("\n", "\r", "\x00")):
        raise ValueError(
            "sudo_password must be a single line: newline / carriage-return / NUL "
            "characters would smuggle additional bash commands onto stdin"
        )
    conn = await connector._connect(target, operator)
    script_bytes = script.encode("utf-8")
    cmd = build_sudo_bash_remote_cmd(len(script_bytes))
    # Script bytes first, password last, EOF immediately after (the #697
    # wire shape): ``head -c <N>`` consumes exactly the script's byte count
    # via unbuffered read(2), so sudo's buffered stdin sees only the
    # password line and cannot prefetch-and-swallow script bytes.
    stdin_payload = f"{script}{sudo_password}\n"
    result = await asyncio.wait_for(
        conn.run(cmd, input=stdin_payload, check=False),
        timeout=timeout,
    )
    # Logging discipline: only lengths + exit code. Neither the script nor
    # the password (nor any token the script rotates) is bound into the event.
    _log.info(
        "rke2_ssh_sudo_command_executed",
        target=getattr(target, "name", None),
        cmd_len=len(cmd),
        script_len=len(script_bytes),
        exit_code=result.exit_status,
    )
    return result

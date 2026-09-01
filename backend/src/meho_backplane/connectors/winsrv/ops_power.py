# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""winsrv power ops — ``reboot`` / ``shutdown`` (dangerous + requires_approval).

The rke2 approval-parked-write mold (``dangerous`` +
``requires_approval=True``): the dispatcher's policy gate parks a dispatch at
``needs-approval`` for a human decision, and the handler runs only on the
``_approved=True`` resume path. Per the Initiative #3259 satellite table a
destructive op is satellite-EXCLUDED by the tier ladder — central-dial /
on-site only.

Why ``shutdown.exe`` with a delay (not a bare ``Restart-Computer -Force``)
------------------------------------------------------------------------

A reboot/shutdown tears down the very SSH channel the transport is reading
its ``ConvertTo-Json`` ack over. A bare ``Restart-Computer -Force`` initiates
the teardown immediately, so the channel can drop before stdout flushes and
:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run` would raise on a
truncated read — reporting failure on a reboot that in fact succeeded. Using
``shutdown.exe /r /t <delay>`` *schedules* the reboot, returns
immediately, and the JSON ack flushes cleanly; the machine goes down after
the delay. ``$LASTEXITCODE`` is checked so a rejected schedule (already
shutting down, insufficient rights) surfaces as a real
:class:`~meho_backplane.connectors._shared.pwsh.PwshRunError` rather than a
false ``ok``.

PowerShell injection safety
---------------------------

The optional operator ``message`` is interpolated only inside a single-quoted
PowerShell literal via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`; the delay is
a Python-validated non-negative ``int``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.winsrv.ops import SSH_TRANSPORT_NOTE, WinsrvOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.winsrv.connector import WinsrvConnector

__all__ = [
    "POWER_OPS",
    "winsrv_power_reboot",
    "winsrv_power_shutdown",
]

#: Default schedule delay (seconds). Non-zero so the JSON ack flushes over
#: SSH before the OS tears the channel down, and a small grace window before
#: the host actually goes down.
_DEFAULT_DELAY_SECONDS: int = 15


def _validate_delay(raw: Any) -> int:
    """Return a validated non-negative ``int`` delay (default when absent)."""
    if raw is None:
        return _DEFAULT_DELAY_SECONDS
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise ValueError(f"delay_seconds must be a non-negative integer; got {raw!r}")
    return raw


async def _power_action(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    *,
    flag: str,
    action: str,
    operator: Operator | None,
) -> dict[str, Any]:
    """Schedule ``shutdown.exe`` with *flag* (``/r`` reboot, ``/s`` shutdown)."""
    delay = _validate_delay(params.get("delay_seconds"))
    message: str | None = params.get("message")
    comment_clause = ""
    if message:
        comment_clause = f" /c {ps_single_quote(message)}"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"shutdown.exe {flag} /t {delay}{comment_clause}; "
        'if ($LASTEXITCODE -ne 0) { throw "shutdown.exe exited $LASTEXITCODE" }; '
        "ConvertTo-Json -Compress -InputObject @{ "
        f"ok = $true; action = '{action}'; delay_seconds = {delay} }}"
    )
    await pwsh_run(connector, target, script, operator=operator)
    return {
        "ok": True,
        "action": action,
        "delay_seconds": delay,
        "message": message,
        "op_class": "write",
    }


async def winsrv_power_reboot(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.power.reboot`` (dangerous, resume path only)."""
    return await _power_action(
        connector, target, params, flag="/r", action="reboot", operator=operator
    )


async def winsrv_power_shutdown(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.power.shutdown`` (dangerous, resume path only)."""
    return await _power_action(
        connector, target, params, flag="/s", action="shutdown", operator=operator
    )


_POWER_PARAM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "delay_seconds": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "Seconds to wait before the host goes down (``shutdown.exe "
                "/t``). Default 15 — non-zero so the ack flushes and there is "
                "a short grace window."
            ),
        },
        "message": {
            "type": "string",
            "description": ("Optional comment shown to logged-in users (``shutdown.exe /c``)."),
        },
    },
    "additionalProperties": False,
}

_POWER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "action": {"type": "string"},
        "delay_seconds": {"type": "integer"},
        "message": {"type": ["string", "null"]},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["ok", "action", "op_class"],
    "additionalProperties": True,
}


POWER_OPS: tuple[WinsrvOp, ...] = (
    WinsrvOp(
        op_id="winsrv.power.reboot",
        handler_attr="winsrv_power_reboot",
        summary="Reboot the Windows Server host (dangerous, approval-gated).",
        description=(
            "Schedules a reboot via ``shutdown.exe /r /t <delay>`` (default "
            "15s) so the ack flushes before the SSH channel tears down. "
            "safety_level=dangerous, requires_approval=True — a dispatch "
            "parks for a human to approve before anything changes. The right "
            "op to land a pending reboot after a feature install; it does NOT "
            "run automatically as part of ``winsrv.feature.install``."
        ),
        parameter_schema=_POWER_PARAM_SCHEMA,
        response_schema=_POWER_RESPONSE_SCHEMA,
        group_key="power",
        tags=("write", "power", "reboot"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Reboot a Windows Server host (e.g. to land a "
                "Failover-Clustering install that reported "
                "``restart_needed``). Destructive to running work; "
                "approval-gated — parks for a human decision first. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "delay_seconds": "Optional; seconds before going down. Default 15.",
                "message": "Optional comment shown to logged-in users.",
            },
            "output_shape": (
                "{'ok': true, 'action': 'reboot', 'delay_seconds', 'message', "
                "'op_class': 'write'}. The reboot lands after the delay."
            ),
        },
    ),
    WinsrvOp(
        op_id="winsrv.power.shutdown",
        handler_attr="winsrv_power_shutdown",
        summary="Shut down the Windows Server host (dangerous, approval-gated).",
        description=(
            "Schedules a shutdown via ``shutdown.exe /s /t <delay>`` (default "
            "15s). safety_level=dangerous, requires_approval=True — parks for "
            "human approval. A shutdown (unlike a reboot) leaves the host off "
            "until it is powered back on out of band."
        ),
        parameter_schema=_POWER_PARAM_SCHEMA,
        response_schema=_POWER_RESPONSE_SCHEMA,
        group_key="power",
        tags=("write", "power", "shutdown"),
        safety_level="dangerous",
        requires_approval=True,
        llm_instructions={
            "when_to_use": (
                "Power a Windows Server host down (it stays off until powered "
                "back on out of band). Destructive; approval-gated. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "delay_seconds": "Optional; seconds before going down. Default 15.",
                "message": "Optional comment shown to logged-in users.",
            },
            "output_shape": (
                "{'ok': true, 'action': 'shutdown', 'delay_seconds', "
                "'message', 'op_class': 'write'}."
            ),
        },
    ),
)

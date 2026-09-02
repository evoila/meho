# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""hyperv power ops — ``power.start`` / ``power.stop`` (both caution).

The source-side cutover verbs. ``Start-VM`` / ``Stop-VM`` over the shared
PowerShell-over-SSH transport
(:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`). Both ``caution`` —
recoverable (a stopped VM can be started again). Stop the source VM as the final
cutover step once the target VMware VM is validated.

``Stop-VM`` modes (``mode`` param):

* ``shutdown`` (default) — graceful guest shutdown (``-Force`` so it proceeds
  non-interactively even with unsaved data; Hyper-V gives the guest five
  minutes). Blocks until the guest is off, so a wider transport timeout is used.
* ``turnoff`` — hard power-off (``-TurnOff``), equivalent to pulling the plug;
  can lose unsaved data.
* ``save`` — suspend to a saved state (``-Save``).

PowerShell injection safety
---------------------------

The operator-supplied ``vm_name`` is interpolated only inside a single-quoted
literal via :func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`;
``mode`` is validated against a fixed enum in Python and maps to a constant
switch string (never interpolated raw).

References
----------

* ``Start-VM`` / ``Stop-VM`` (``-TurnOff`` hard-off, ``-Save`` suspend,
  ``-Force`` non-interactive graceful shutdown):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/stop-vm
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.hyperv.ops import SSH_TRANSPORT_NOTE, HypervOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.hyperv.connector import HypervConnector

__all__ = ["POWER_OPS", "hyperv_power_start", "hyperv_power_stop"]

#: ``mode`` → the mode-selecting ``Stop-VM`` switch (``None`` for the default
#: graceful shutdown, which needs no mode switch). ``-Force`` is appended to
#: every mode by the handler so the cmdlet runs non-interactively.
_STOP_MODE_SWITCH: dict[str, str | None] = {
    "shutdown": None,
    "turnoff": "-TurnOff",
    "save": "-Save",
}

#: Wider transport budget for a graceful stop — the guest gets up to five
#: minutes to shut down, so a 30s default would falsely time out.
_STOP_TIMEOUT_SECONDS: float = 360.0


async def hyperv_power_start(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.power.start`` (caution) — ``Start-VM -Name``."""
    vm_name = params["vm_name"]
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$s = Start-VM -Name {ps_single_quote(vm_name)} -Passthru; "
        'ConvertTo-Json -Compress -InputObject @{ ok = $true; state = "$($s.State)" }'
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {"vm_name": vm_name, "action": "start", "state": data.get("state"), "op_class": "write"}


async def hyperv_power_stop(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.power.stop`` (caution) — ``Stop-VM -Name`` (mode-selected)."""
    vm_name = params["vm_name"]
    mode = params.get("mode", "shutdown")
    if mode not in _STOP_MODE_SWITCH:
        raise ValueError(f"mode must be one of {sorted(_STOP_MODE_SWITCH)}; got {mode!r}")
    mode_switch = _STOP_MODE_SWITCH[mode]
    # -Force is always appended so the cmdlet never waits on an interactive
    # confirmation; the default (graceful) mode adds no mode switch of its own.
    switch = f"{mode_switch} -Force" if mode_switch else "-Force"
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$s = Stop-VM -Name {ps_single_quote(vm_name)} {switch} -Passthru; "
        'ConvertTo-Json -Compress -InputObject @{ ok = $true; state = "$($s.State)" }'
    )
    payload = await pwsh_run(
        connector, target, script, operator=operator, timeout=_STOP_TIMEOUT_SECONDS
    )
    data = payload if isinstance(payload, dict) else {}
    return {
        "vm_name": vm_name,
        "action": "stop",
        "mode": mode,
        "state": data.get("state"),
        "op_class": "write",
    }


_VM_NAME_PROP: dict[str, Any] = {
    "type": "string",
    "minLength": 1,
    "pattern": "\\S",
    "description": "The virtual machine name (the ``Start-VM`` / ``Stop-VM -Name`` operand).",
}

_WRITE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vm_name": {"type": "string"},
        "action": {"type": "string"},
        "state": {"type": ["string", "null"]},
        "op_class": {"type": "string", "enum": ["write"]},
    },
    "required": ["vm_name", "action", "op_class"],
    "additionalProperties": True,
}


POWER_OPS: tuple[HypervOp, ...] = (
    HypervOp(
        op_id="hyperv.power.start",
        handler_attr="hyperv_power_start",
        summary="Start a VM via ``Start-VM`` (caution).",
        description=(
            "Runs ``Start-VM -Name <vm_name>`` and echoes the resulting state. "
            "safety_level=caution — recoverable. A source-side cutover verb "
            "(e.g. to bring a VM back after an aborted migration step)."
        ),
        parameter_schema={
            "type": "object",
            "properties": {"vm_name": _VM_NAME_PROP},
            "required": ["vm_name"],
            "additionalProperties": False,
        },
        response_schema=_WRITE_RESPONSE_SCHEMA,
        group_key="power",
        tags=("write", "power", "start"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Start a Hyper-V VM. Recoverable; safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {"vm_name": "Required. The VM name."},
            "output_shape": "{'vm_name', 'action': 'start', 'state', 'op_class': 'write'}.",
        },
    ),
    HypervOp(
        op_id="hyperv.power.stop",
        handler_attr="hyperv_power_stop",
        summary="Stop a VM via ``Stop-VM`` — graceful / turn-off / save (caution).",
        description=(
            "Runs ``Stop-VM -Name <vm_name>`` in one of three modes: "
            "``shutdown`` (default — graceful guest shutdown, ``-Force`` so it "
            "proceeds non-interactively), ``turnoff`` (hard power-off, "
            "``-TurnOff`` — can lose unsaved data), or ``save`` (suspend, "
            "``-Save``). safety_level=caution — recoverable. Stop the source VM "
            "as the final cutover step once the target is validated."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "vm_name": _VM_NAME_PROP,
                "mode": {
                    "type": "string",
                    "enum": ["shutdown", "turnoff", "save"],
                    "default": "shutdown",
                    "description": (
                        "How to stop: ``shutdown`` (graceful, default), "
                        "``turnoff`` (hard power-off), or ``save`` (suspend)."
                    ),
                },
            },
            "required": ["vm_name"],
            "additionalProperties": False,
        },
        response_schema={
            **_WRITE_RESPONSE_SCHEMA,
            "properties": {
                **_WRITE_RESPONSE_SCHEMA["properties"],
                "mode": {"type": "string"},
            },
        },
        group_key="power",
        tags=("write", "power", "stop"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Stop a Hyper-V VM — graceful shutdown (default), hard turn-off, "
                "or save/suspend. Recoverable; safety_level=caution. The final "
                "source-side cutover verb. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "vm_name": "Required. The VM name.",
                "mode": "Optional; 'shutdown' (default) / 'turnoff' / 'save'.",
            },
            "output_shape": (
                "{'vm_name', 'action': 'stop', 'mode', 'state', 'op_class': 'write'}."
            ),
        },
    ),
)

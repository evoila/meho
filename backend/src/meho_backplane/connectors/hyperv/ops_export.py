# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""hyperv export ops — ``export.vm`` (caution; the migration seed).

``Export-VM`` writes a VM's configuration + virtual disks + checkpoints to a
folder on the Hyper-V host (three subfolders: ``Virtual Machines``, ``Virtual
Hard Disks``, ``Snapshots``) — the source artifact a Hyper-V→VMware migration
consumes. ``caution``: recoverable (it copies, it does not remove the source),
but the tier makes it satellite-executable only through the Stage-3 composed
write gate (#2901).

Long-running semantics (documented, deliberate)
-----------------------------------------------

``Export-VM`` is **synchronous** and can run for many minutes on a
multi-hundred-GB VM: it blocks the ``powershell -EncodedCommand`` process (and
therefore this SSH round-trip) until the copy finishes. Unlike a reboot it does
**not** tear the SSH channel down, so a blocking read is safe — the only risk is
the wall-clock budget. The handler therefore exposes ``timeout_seconds`` (default
one hour) and forwards it to the transport's per-call timeout so the caller sizes
the budget to the VM's disk size. A truly asynchronous ``-AsJob`` submission +
out-of-band job polling is a documented follow-up (it needs a session-surviving
job store the stateless per-call transport does not have); this cut ships the
straightforward blocking export, which is correct for the lab-scale VMs the
consumer proof exercises.

PowerShell injection safety
---------------------------

The operator-supplied ``vm_name`` / ``path`` are interpolated only inside
single-quoted literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`. ``timeout_seconds``
is a Python-validated bounded ``int`` and never reaches the script text.

References
----------

* ``Export-VM`` (``-Name`` / ``-Path`` folder; no output on success):
  https://learn.microsoft.com/en-us/powershell/module/hyper-v/export-vm
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.hyperv.ops import SSH_TRANSPORT_NOTE, HypervOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.hyperv.connector import HypervConnector

__all__ = ["EXPORT_OPS", "hyperv_export_vm"]

#: Default export wall-clock budget (seconds). One hour covers a lab-scale VM;
#: the operator raises it for a large production disk.
_DEFAULT_TIMEOUT_SECONDS: int = 3600

#: Hard ceiling on the export budget — a full day. Guards against an
#: accidentally unbounded blocking SSH round-trip.
_MAX_TIMEOUT_SECONDS: int = 86400


def _validate_timeout(raw: Any) -> int:
    """Return a validated ``1..86400`` second budget (default when absent)."""
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS
    if not isinstance(raw, int) or isinstance(raw, bool) or not (0 < raw <= _MAX_TIMEOUT_SECONDS):
        raise ValueError(
            f"timeout_seconds must be an integer 1..{_MAX_TIMEOUT_SECONDS}; got {raw!r}"
        )
    return raw


async def hyperv_export_vm(
    connector: HypervConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``hyperv.export.vm`` (caution) — ``Export-VM -Name -Path``.

    Exports the VM to ``path`` (a folder on the host). Blocks until the export
    completes; ``timeout_seconds`` bounds the wall-clock budget (see the module
    docstring on long-running semantics).
    """
    vm_name = params["vm_name"]
    path = params["path"]
    timeout = _validate_timeout(params.get("timeout_seconds"))
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Export-VM -Name {ps_single_quote(vm_name)} -Path {ps_single_quote(path)}; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true }"
    )
    await pwsh_run(connector, target, script, operator=operator, timeout=float(timeout))
    return {
        "vm_name": vm_name,
        "path": path,
        "action": "export",
        "timeout_seconds": timeout,
        "op_class": "write",
    }


EXPORT_OPS: tuple[HypervOp, ...] = (
    HypervOp(
        op_id="hyperv.export.vm",
        handler_attr="hyperv_export_vm",
        summary="Export a VM to a folder via ``Export-VM`` (caution; long-running migration seed).",
        description=(
            "Runs ``Export-VM -Name <vm_name> -Path <path>`` to write the VM's "
            "configuration + virtual disks + checkpoints into a folder on the "
            "Hyper-V host — the source artifact a Hyper-V→VMware migration "
            "consumes. safety_level=caution — recoverable (it copies, does not "
            "remove the source). LONG-RUNNING: the call blocks over SSH until "
            "the export completes; set ``timeout_seconds`` (default 3600) to "
            "cover the VM's disk size. The export folder already contains the "
            "Virtual Hard Disks, so there is no separate disk-export op."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "vm_name": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": "The VM name to export (``Export-VM -Name``).",
                },
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": (
                        "The destination FOLDER on the Hyper-V host "
                        "(``Export-VM -Path``); Export-VM creates a per-VM "
                        "subfolder under it."
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_TIMEOUT_SECONDS,
                    "description": (
                        "Wall-clock budget for the blocking export (default "
                        "3600). Raise for a large disk; the export blocks the "
                        "SSH round-trip until it completes."
                    ),
                },
            },
            "required": ["vm_name", "path"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "vm_name": {"type": "string"},
                "path": {"type": "string"},
                "action": {"type": "string"},
                "timeout_seconds": {"type": "integer"},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["vm_name", "path", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="export",
        tags=("write", "export", "migration"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Export a source VM to a folder on the Hyper-V host to seed a "
                "Hyper-V→VMware migration. Recoverable; safety_level=caution. "
                "LONG-RUNNING — set timeout_seconds to cover the VM's disk "
                "size; the call blocks until the export finishes. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "vm_name": "Required. The VM name to export.",
                "path": "Required. The destination folder on the Hyper-V host.",
                "timeout_seconds": "Optional. Wall-clock budget in seconds (default 3600).",
            },
            "output_shape": (
                "{'vm_name', 'path', 'action': 'export', 'timeout_seconds', 'op_class': 'write'}."
            ),
        },
    ),
)

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`HypervConnector`.

The connector is the **Hyper-V migration-source** reach: it reads a Hyper-V
host's inventory, VM configuration, and virtual-disk / checkpoint facts — the
inputs to a Hyper-V→VMware migration plan — plus the guarded export that seeds
that migration, over SSH → PowerShell (``powershell`` running the Windows
PowerShell 5.1 ``Hyper-V`` module cmdlets), routed through the shared
PowerShell-over-SSH transport
:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run` (hoisted by #3260).
Structurally it is a copy of the ``winsrv`` estate mold (#3261): identity canary
+ per-domain op groups on the ``SshConnector`` base, the ``{rows, total}``
JSONFlux envelope for list reads, and the ``rke2`` approval-parked-write mold
for its destructive ops (checkpoint revert / delete).

Op surface (18 ops across six groups; safety tier per the Initiative #3259
satellite table — reads ``safe``, recoverable writes ``caution``, destructive
``dangerous`` + ``requires_approval``):

* ``hyperv.about``              [safe]   identity canary (OS + Hyper-V module + hypervisor).
* ``hyperv.host.info``          [safe]   Get-VMHost (CPUs, memory, NUMA spanning, default paths).
* ``hyperv.host.numa``          [safe]   Get-VMHostNumaNode (host NUMA topology; list).
* ``hyperv.host.vswitch.list``  [safe]   Get-VMSwitch (virtual switch inventory; list).
* ``hyperv.vms.list``           [safe]   Get-VM (VM inventory; the migration-assessment surface).
* ``hyperv.vms.get``            [safe]   Get-VM -Name (one VM's identity + state + generation).
* ``hyperv.vms.config``         [safe]   deep config: memory / processors / generation / firmware.
* ``hyperv.vms.state``          [safe]   runtime state: State / Status / Uptime / Heartbeat.
* ``hyperv.disks.vm.list``      [safe]   Get-VMHardDiskDrive (a VM's attached disks; list).
* ``hyperv.disks.vhd.get``      [safe]   Get-VHD (one VHD/VHDX's format/size/parent/fragmentation).
* ``hyperv.disks.vhd.chain``    [safe]   differencing-disk parent chain (leaf → base; list).
* ``hyperv.checkpoints.list``   [safe]   Get-VMSnapshot (a VM's checkpoints; list).
* ``hyperv.checkpoints.create`` [caution]              Checkpoint-VM.
* ``hyperv.checkpoints.revert`` [dangerous+approval]   Restore-VMSnapshot.
* ``hyperv.checkpoints.delete`` [dangerous+approval]   Remove-VMSnapshot.
* ``hyperv.export.vm``          [caution]              Export-VM (the migration seed; long-running).
* ``hyperv.power.start``        [caution]              Start-VM (source-side cutover verb).
* ``hyperv.power.stop``         [caution]              Stop-VM (graceful / turn-off / save).

**Explicit non-goals in this increment** (named so the scoping is on record):
VM create / delete on Hyper-V (we manage the source, we do not build on it),
SCVMM, and Hyper-V Replica. Cluster-wide views of a Hyper-V cluster's nodes
belong to the ``wsfc`` connector (#3263) on the same nodes — not duplicated
here.

The dataclass + tuple shape mirrors :mod:`~meho_backplane.connectors.winsrv.ops`
and :mod:`~meho_backplane.connectors.msad.ops`; the composition pattern
(``_hyperv_ops()`` importing per-domain op tuples) mirrors them too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from meho_backplane.connectors._shared.pwsh import pwsh_run

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.hyperv.connector import HypervConnector

__all__ = [
    "HYPERV_OPS",
    "HYPERV_WHEN_TO_USE_BY_GROUP",
    "SSH_TRANSPORT_NOTE",
    "HypervOp",
    "hyperv_list_read",
    "normalise_json_rows",
]


#: Curated ``when_to_use`` strings per group key, consumed by
#: :meth:`HypervConnector.register_operations` (the winsrv / rke2 precedent — the
#: blurbs live with the op metadata, not the transport class). The registration
#: walk fails closed with a :class:`ValueError` if a ``group_key`` lacks a
#: curated entry here.
HYPERV_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "host": (
        "Use for Hyper-V host facts before any VM drill-in: identity "
        "(``hyperv.about``), the host projection (``hyperv.host.info`` — logical "
        "processors, memory capacity, NUMA spanning, default VM / VHD paths), "
        "the NUMA topology (``hyperv.host.numa``), and the virtual-switch "
        "inventory (``hyperv.host.vswitch.list``). All read-only. Call "
        "``hyperv.about`` first to confirm the target is a reachable Hyper-V "
        "host (Hyper-V module present + hypervisor running)."
    ),
    "vms": (
        "Use to assess the VMs that are migration candidates: list every VM "
        "(``hyperv.vms.list`` — the assessment surface: state, generation, "
        "integration-services version), read one VM's identity "
        "(``hyperv.vms.get``), its deep configuration (``hyperv.vms.config`` — "
        "memory, processors, generation, firmware / secure-boot), and its "
        "runtime state (``hyperv.vms.state`` — State / Status / Uptime / "
        "Heartbeat). All read-only. Generation and secure-boot decide the "
        "target-side VMware firmware (BIOS vs EFI)."
    ),
    "disks": (
        "Use to plan the VHDX→VMDK conversion: enumerate a VM's attached "
        "virtual disks (``hyperv.disks.vm.list`` — controller + path), read one "
        "VHD/VHDX's facts (``hyperv.disks.vhd.get`` — format, virtual size, "
        "on-disk file size, parent path, fragmentation), and walk a "
        "differencing disk's parent chain to the base (``hyperv.disks.vhd.chain"
        "``). All read-only. A differencing chain must be merged before a clean "
        "single-file VMDK conversion."
    ),
    "checkpoints": (
        "Use to inventory and manage a VM's checkpoints (snapshots): list "
        "(``hyperv.checkpoints.list``, read-only, ``safe``); create a checkpoint "
        "(``hyperv.checkpoints.create``, ``caution`` — recoverable); revert to a "
        "checkpoint (``hyperv.checkpoints.revert``) and delete one "
        "(``hyperv.checkpoints.delete``) are ``dangerous`` + "
        "``requires_approval`` — a revert discards state written since the "
        "checkpoint and a delete is irreversible, so both park for a human "
        "decision. Take a checkpoint before a risky migration cutover step."
    ),
    "export": (
        "Use to seed the migration: ``hyperv.export.vm`` runs ``Export-VM`` to "
        "write a VM's configuration + virtual disks to a folder on the host "
        "(the source artifact a VMware import consumes). ``caution`` — "
        "recoverable (it copies, it does not remove the source) but "
        "**long-running**: the call blocks over SSH until the export completes, "
        "so set ``timeout_seconds`` to cover the VM's disk size. There is no "
        "separate disk-export op — the exported folder already contains the "
        "Virtual Hard Disks."
    ),
    "power": (
        "Use for the source-side cutover verbs: start a VM "
        "(``hyperv.power.start``) and stop one (``hyperv.power.stop`` — graceful "
        "guest shutdown by default, ``turnoff`` for a hard power-off, or "
        "``save`` to suspend). Both ``caution`` — recoverable (a stopped VM can "
        "be started again). Stop the source VM as the final cutover step once "
        "the target VMware VM is validated."
    ),
}


#: Canonical SSH-only / pwsh transport reminder copied into every op's
#: ``llm_instructions`` (the winsrv ``SSH_TRANSPORT_NOTE`` precedent) so an LLM
#: does not compose against a non-existent Hyper-V REST surface.
SSH_TRANSPORT_NOTE: str = (
    "Hyper-V exposes no unified REST API for these operations; the underlying "
    "transport is PowerShell-over-SSH (powershell -EncodedCommand routed "
    "through asyncssh) driving the Windows PowerShell 5.1 Hyper-V module "
    "cmdlets on the Hyper-V host."
)


@dataclass(frozen=True)
class HypervOp:
    """Metadata for one hyperv op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod can splat
    the dataclass into the helper. ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.hyperv.connector.HypervConnector` exposing
    the async handler; the connector resolves the bound method against itself at
    registration time (identical shape to
    :class:`~meho_backplane.connectors.winsrv.ops.WinsrvOp`).
    """

    op_id: str
    handler_attr: str
    summary: str
    description: str
    parameter_schema: dict[str, Any]
    response_schema: dict[str, Any] | None
    group_key: str | None
    tags: tuple[str, ...]
    safety_level: Literal["safe", "caution", "dangerous", "destructive"]
    requires_approval: bool
    llm_instructions: dict[str, Any] | None


def normalise_json_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalise a ``ConvertTo-Json`` payload into a list of row dicts.

    ``ConvertTo-Json`` renders a **single**-element result as a flat object and
    a **multi**-element result as a JSON array; a zero-element result renders as
    ``null`` (an empty stdout is caught upstream by
    :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`). This collapses
    all three shapes to a ``list[dict]`` — the winsrv ``normalise_json_rows``
    shape, shared across every list op so the ``{rows, total}`` envelope the
    JSONFlux reducer keys on is built identically.
    """
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


async def hyperv_list_read(
    connector: HypervConnector,
    target: Any,
    *,
    pipeline: str,
    operator: Operator | None,
    prelude: str = "",
) -> dict[str, Any]:
    """Run a read *pipeline* under ``'Stop'`` and return the ``{rows, total}`` envelope.

    *pipeline* is the ``Get-VM… | Select-Object …`` expression whose output is
    wrapped in ``@(…)`` (so a single object still counts as one row and an empty
    result stays JSON-shaped — never an empty-stdout
    :class:`~meho_backplane.connectors._shared.pwsh.PwshRunError`). *prelude* is
    any statement(s) that must run first (e.g. a ``$n = '…'`` assignment); it is
    emitted verbatim before the pipeline. Running under
    ``$ErrorActionPreference = 'Stop'`` turns a cmdlet error (unknown VM, bad
    path) into a real ``PwshRunError`` rather than a false-empty read. ``-Depth
    4`` keeps calculated / nested projections (NUMA processor arrays, VHD parent
    metadata) from truncating.
    """
    script = (
        "$ErrorActionPreference = 'Stop'; "
        + prelude
        + f"$x = @({pipeline}); "
        + "ConvertTo-Json -Depth 4 -InputObject @{ rows = $x; total = $x.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


#: The identity canary op. ``hyperv.about`` is the operator-facing wrapper around
#: :meth:`HypervConnector.fingerprint` — same vendor / product / host payload,
#: surfaced through the typed-op dispatcher so callers see the standard
#: :class:`OperationResult` envelope. Mirrors ``winsrv.about``.
_HYPERV_ABOUT_OP = HypervOp(
    op_id="hyperv.about",
    handler_attr="about",
    summary="Return the Hyper-V host identity (OS + Hyper-V module + hypervisor).",
    description=(
        "Connects to the Hyper-V host over SSH and runs a single ``powershell "
        "-EncodedCommand`` script that reads the machine hostname, the "
        "``Win32_OperatingSystem`` CIM instance (caption / version / build), the "
        "Windows PowerShell version, whether the ``Hyper-V`` management module "
        "is installed (and its version), and whether the hypervisor is running "
        "(``Win32_ComputerSystem.HypervisorPresent``). Returns a flat dict with "
        "the vendor (``microsoft``), product (``hyper-v``), the OS version + "
        "build, and the host name. Use to confirm the target is a reachable "
        "Hyper-V host before issuing higher-level host / vms / disks / "
        "checkpoints / export / power ops; no params; safe on any healthy "
        "target."
    ),
    parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
    response_schema={
        "type": "object",
        "properties": {
            "vendor": {"type": "string"},
            "product": {"type": "string"},
            "version": {"type": ["string", "null"]},
            "build": {"type": ["string", "null"]},
            "hostname": {"type": ["string", "null"]},
            "os_caption": {"type": ["string", "null"]},
            "powershell_version": {"type": ["string", "null"]},
            "hyperv_module": {"type": ["boolean", "null"]},
            "hyperv_module_version": {"type": ["string", "null"]},
            "hypervisor_present": {"type": ["boolean", "null"]},
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="host",
    tags=("read-only", "identity", "hyper-v"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the Hyper-V host behind a "
            "target — its OS caption / version / build, the Hyper-V module "
            "version, whether the hypervisor is running — or to confirm the "
            "target is a reachable Hyper-V host before issuing higher-level "
            "ops. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; ``version`` carries the host OS version (e.g. "
            "``10.0.20348``), ``hyperv_module_version`` the Hyper-V module "
            "version, ``hypervisor_present`` whether the hypervisor is running, "
            "``hostname`` the machine name."
        ),
    },
)


def _hyperv_ops() -> tuple[HypervOp, ...]:
    """Return the merged registration tuple (identity canary + per-group tuples).

    Implemented as a function call (not a module-level literal-and-splat) so the
    import order stays linear: ``ops.py`` defines :class:`HypervOp` +
    ``_HYPERV_ABOUT_OP`` + the shared helpers, then imports the per-domain op
    tuples from their modules. Mirrors
    :func:`meho_backplane.connectors.winsrv.ops._winsrv_ops`.
    """
    from meho_backplane.connectors.hyperv.ops_checkpoints import CHECKPOINT_OPS
    from meho_backplane.connectors.hyperv.ops_disks import DISK_OPS
    from meho_backplane.connectors.hyperv.ops_export import EXPORT_OPS
    from meho_backplane.connectors.hyperv.ops_host import HOST_OPS
    from meho_backplane.connectors.hyperv.ops_power import POWER_OPS
    from meho_backplane.connectors.hyperv.ops_vms import VMS_OPS

    return (
        _HYPERV_ABOUT_OP,
        *HOST_OPS,
        *VMS_OPS,
        *DISK_OPS,
        *CHECKPOINT_OPS,
        *EXPORT_OPS,
        *POWER_OPS,
    )


#: The ops :class:`HypervConnector` registers at lifespan startup.
HYPERV_OPS: tuple[HypervOp, ...] = _hyperv_ops()

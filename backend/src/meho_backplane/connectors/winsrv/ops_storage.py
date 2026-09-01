# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""winsrv storage ops — disk/volume/iSCSI reads (safe) + iSCSI connect / disk format.

Storage is driven by the ``Storage`` module (``Get-Disk`` / ``Get-Volume`` /
``Initialize-Disk`` / ``New-Partition`` / ``Format-Volume``) and the ``iSCSI``
module (``Get-IscsiTarget`` / ``Connect-IscsiTarget``) over the shared
PowerShell-over-SSH transport. This is the c1sql1 SQL-FCI shared-disk path:
attach an iSCSI target, then provision the raw disk into a volume.

``iscsi.connect`` and ``disk.format`` are ``caution`` (recoverable
provisioning). ``disk.format`` additionally **fails closed** on a disk that
already carries a partition table (``PartitionStyle != RAW``) unless
``force=true`` is passed — it will not silently clobber data.

Deferred: iSCSI CHAP
--------------------

``Connect-IscsiTarget`` supports CHAP (``-ChapUsername`` / ``-ChapSecret``),
but the secret would land in the ``-EncodedCommand`` script — a violation of
the shared transport's secret-hygiene contract (see
:mod:`~meho_backplane.connectors._shared.pwsh`). CHAP is therefore out of
scope for this cut (connect against a trusted lab fabric); a Vault-brokered
CHAP flow is a follow-up.

PowerShell injection safety
---------------------------

Operator-supplied strings (IQN / portal address / volume label) are
interpolated only inside single-quoted PowerShell literals via
:func:`~meho_backplane.connectors._shared.pwsh.ps_single_quote`. The disk
number, portal port, and drive letter are Python-validated (int / bounded
enum / single ``[A-Za-z]``); booleans render as ``$true`` / ``$false``.

References
----------

* ``Get-Disk`` / ``Get-Volume`` / ``Initialize-Disk`` / ``New-Partition`` /
  ``Format-Volume`` (Storage, Windows Server 2022) and ``Get-IscsiTarget`` /
  ``Connect-IscsiTarget`` (iSCSI):
  https://learn.microsoft.com/en-us/powershell/module/storage/ ,
  https://learn.microsoft.com/en-us/powershell/module/iscsi/
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import ps_single_quote, pwsh_run
from meho_backplane.connectors.winsrv.ops import SSH_TRANSPORT_NOTE, WinsrvOp, normalise_json_rows

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.winsrv.connector import WinsrvConnector

__all__ = [
    "STORAGE_OPS",
    "winsrv_disk_format",
    "winsrv_disk_list",
    "winsrv_iscsi_connect",
    "winsrv_iscsi_list",
    "winsrv_volume_list",
]

_DISK_SELECT: str = (
    "Number, FriendlyName, SerialNumber, PartitionStyle, "
    "OperationalStatus, HealthStatus, Size, BusType"
)
_VOLUME_SELECT: str = (
    "DriveLetter, FileSystemLabel, FileSystem, DriveType, HealthStatus, SizeRemaining, Size"
)
_ISCSI_SELECT: str = "NodeAddress, IsConnected"

_SUPPORTED_FILESYSTEMS: frozenset[str] = frozenset({"NTFS", "ReFS"})


async def _list_read(
    connector: WinsrvConnector,
    target: Any,
    cmdlet: str,
    select: str,
    operator: Operator | None,
) -> dict[str, Any]:
    """Run a read cmdlet under ``'Stop'`` and return the ``{rows, total}`` envelope."""
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$x = @({cmdlet} | Select-Object {select}); "
        "ConvertTo-Json -Depth 3 -InputObject @{ rows = $x; total = $x.Count }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    raw_rows = payload.get("rows") if isinstance(payload, dict) else payload
    rows = normalise_json_rows(raw_rows)
    return {"rows": rows, "total": len(rows)}


async def winsrv_disk_list(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.storage.disk.list`` — ``Get-Disk`` (list-shaped)."""
    del params
    return await _list_read(connector, target, "Get-Disk", _DISK_SELECT, operator)


async def winsrv_volume_list(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.storage.volume.list`` — ``Get-Volume`` (list-shaped)."""
    del params
    return await _list_read(connector, target, "Get-Volume", _VOLUME_SELECT, operator)


async def winsrv_iscsi_list(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.storage.iscsi.list`` — ``Get-IscsiTarget`` (list-shaped)."""
    del params
    return await _list_read(connector, target, "Get-IscsiTarget", _ISCSI_SELECT, operator)


async def winsrv_iscsi_connect(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.storage.iscsi.connect`` (caution) — ``Connect-IscsiTarget``.

    Connects the local initiator to the iSCSI target identified by its IQN
    (``node_address``), optionally naming the target portal address / port
    and whether the session persists across reboots. No CHAP (see the module
    docstring).
    """
    node_address: str = params["node_address"]
    is_persistent = "$true" if params.get("is_persistent", True) else "$false"
    clauses = [f"Connect-IscsiTarget -NodeAddress {ps_single_quote(node_address)}"]
    if params.get("target_portal_address"):
        clauses.append(f"-TargetPortalAddress {ps_single_quote(params['target_portal_address'])}")
    port = params.get("target_portal_port")
    if port is not None:
        if not isinstance(port, int) or isinstance(port, bool) or not (0 < port <= 65535):
            raise ValueError(f"target_portal_port must be a TCP port 1-65535; got {port!r}")
        clauses.append(f"-TargetPortalPortNumber {port}")
    clauses.append(f"-IsPersistent {is_persistent}")
    connect_expr = " ".join(clauses)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$s = {connect_expr}; "
        "ConvertTo-Json -Depth 3 -Compress -InputObject @{ "
        "ok = $true; node_address = $s.TargetNodeAddress; "
        "is_connected = [bool]$s.IsConnected; is_persistent = [bool]$s.IsPersistent }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {
        "node_address": node_address,
        "action": "connect",
        "is_connected": bool(data.get("is_connected")),
        "is_persistent": bool(data.get("is_persistent")),
        "op_class": "write",
    }


def _validate_drive_letter(raw: Any) -> str | None:
    """Return an uppercased single-letter drive letter, or ``None`` when absent."""
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) != 1 or not raw.isalpha():
        raise ValueError(f"drive_letter must be a single letter A-Z; got {raw!r}")
    return raw.upper()


async def winsrv_disk_format(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.storage.disk.format`` (caution) — provision a raw disk.

    Initializes disk ``disk_number`` (GPT, only when it is ``RAW``), creates a
    max-size partition (with ``drive_letter`` or an auto-assigned letter), and
    formats it (``NTFS`` default / ``ReFS``, optional ``label``). Fails closed
    on a disk that already carries a partition table unless ``force=true`` —
    so an accidental format never silently clobbers data.
    """
    disk_number = params["disk_number"]
    if not isinstance(disk_number, int) or isinstance(disk_number, bool) or disk_number < 0:
        raise ValueError(f"disk_number must be a non-negative integer; got {disk_number!r}")
    filesystem = params.get("filesystem", "NTFS")
    if filesystem not in _SUPPORTED_FILESYSTEMS:
        raise ValueError(
            f"filesystem must be one of {sorted(_SUPPORTED_FILESYSTEMS)}; got {filesystem!r}"
        )
    drive_letter = _validate_drive_letter(params.get("drive_letter"))
    force = "$true" if params.get("force") is True else "$false"

    letter_clause = f"-DriveLetter {drive_letter}" if drive_letter else "-AssignDriveLetter"
    label_clause = ""
    if params.get("label"):
        label_clause = f" -NewFileSystemLabel {ps_single_quote(params['label'])}"

    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$d = Get-Disk -Number {disk_number}; "
        f"if ($d.PartitionStyle -ne 'RAW' -and -not {force}) {{ "
        f"throw 'disk {disk_number} is not RAW (PartitionStyle=' + $d.PartitionStyle + "
        "'); pass force=true to reformat' }; "
        f"if ($d.PartitionStyle -eq 'RAW') {{ "
        f"Initialize-Disk -Number {disk_number} -PartitionStyle GPT | Out-Null }}; "
        f"$p = New-Partition -DiskNumber {disk_number} -UseMaximumSize {letter_clause}; "
        f"$v = Format-Volume -Partition $p -FileSystem {ps_single_quote(filesystem)}"
        f"{label_clause} -Confirm:$false -Force; "
        "ConvertTo-Json -Compress -InputObject @{ ok = $true; "
        f"disk = {disk_number}; drive_letter = $v.DriveLetter.ToString(); "
        "filesystem = $v.FileSystem }"
    )
    payload = await pwsh_run(connector, target, script, operator=operator)
    data = payload if isinstance(payload, dict) else {}
    return {
        "disk_number": disk_number,
        "action": "format",
        "drive_letter": data.get("drive_letter"),
        "filesystem": data.get("filesystem"),
        "op_class": "write",
    }


def _list_op(op_id: str, handler_attr: str, noun: str, cmdlet: str, fields: str) -> WinsrvOp:
    """Build one safe storage list op."""
    return WinsrvOp(
        op_id=op_id,
        handler_attr=handler_attr,
        summary=f"List {noun} via ``{cmdlet}`` (list-shaped, JSONFlux-reduced).",
        description=(
            f"Runs ``{cmdlet}`` and returns one row per {noun} ({fields}). "
            "Read-only; the standard ``{rows, total}`` envelope is JSONFlux-"
            "reduced to a handle for large result sets."
        ),
        parameter_schema={"type": "object", "properties": {}, "additionalProperties": False},
        response_schema={
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": {"type": "object"}},
                "total": {"type": "integer"},
            },
            "required": ["rows", "total"],
            "additionalProperties": True,
        },
        group_key="storage",
        tags=("read-only", "storage", "inventory"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                f"Call to enumerate {noun} on a Windows Server host (e.g. "
                "before connecting an iSCSI target or formatting a new disk). "
                "Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": f"{{'rows': [{{{fields}}}], 'total': <int>}}.",
        },
    )


STORAGE_OPS: tuple[WinsrvOp, ...] = (
    _list_op(
        "winsrv.storage.disk.list",
        "winsrv_disk_list",
        "physical disks",
        "Get-Disk",
        "Number, FriendlyName, PartitionStyle, OperationalStatus, Size, BusType",
    ),
    _list_op(
        "winsrv.storage.volume.list",
        "winsrv_volume_list",
        "volumes",
        "Get-Volume",
        "DriveLetter, FileSystemLabel, FileSystem, SizeRemaining, Size",
    ),
    _list_op(
        "winsrv.storage.iscsi.list",
        "winsrv_iscsi_list",
        "iSCSI initiator targets",
        "Get-IscsiTarget",
        "NodeAddress, IsConnected",
    ),
    WinsrvOp(
        op_id="winsrv.storage.iscsi.connect",
        handler_attr="winsrv_iscsi_connect",
        summary="Connect the initiator to an iSCSI target via ``Connect-IscsiTarget`` (caution).",
        description=(
            "Runs ``Connect-IscsiTarget -NodeAddress <iqn>`` (optionally "
            "``-TargetPortalAddress`` / ``-TargetPortalPortNumber`` / "
            "``-IsPersistent``). No CHAP (the secret can't ride the pwsh "
            "transport — connect against a trusted fabric). safety_level="
            "caution — this is the SQL-FCI shared-disk attach step."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "node_address": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": "\\S",
                    "description": "The discovered target IQN (``-NodeAddress``).",
                },
                "target_portal_address": {
                    "type": "string",
                    "description": "Optional target portal IP/DNS (``-TargetPortalAddress``).",
                },
                "target_portal_port": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 65535,
                    "description": "Optional target portal TCP port (default 3260).",
                },
                "is_persistent": {
                    "type": "boolean",
                    "description": (
                        "Reconnect the session automatically after reboot "
                        "(``-IsPersistent``). Default true — SQL FCI shared "
                        "disks want persistent sessions."
                    ),
                },
            },
            "required": ["node_address"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "node_address": {"type": "string"},
                "action": {"type": "string"},
                "is_connected": {"type": "boolean"},
                "is_persistent": {"type": "boolean"},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["node_address", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="storage",
        tags=("write", "storage", "iscsi", "connect"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Attach an iSCSI target to a Windows Server host (e.g. a SQL "
                "FCI shared disk). Recoverable; safety_level=caution. No CHAP "
                "in this cut. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "node_address": "Required. The target IQN.",
                "target_portal_address": "Optional. Target portal IP/DNS.",
                "is_persistent": "Optional bool; default true.",
            },
            "output_shape": (
                "{'node_address', 'action': 'connect', 'is_connected', "
                "'is_persistent', 'op_class': 'write'}."
            ),
        },
    ),
    WinsrvOp(
        op_id="winsrv.storage.disk.format",
        handler_attr="winsrv_disk_format",
        summary="Initialize + partition + format a raw disk (caution; RAW-guarded).",
        description=(
            "Provisions disk ``disk_number`` into a volume: initializes it "
            "GPT (only when RAW), creates a max-size partition (with "
            "``drive_letter`` or an auto-assigned letter), and formats it "
            "(``NTFS`` default / ``ReFS``, optional ``label``). FAILS CLOSED "
            "on a disk that already carries a partition table unless "
            "``force=true`` — it never silently clobbers data. safety_level="
            "caution — the SQL-FCI shared-disk provisioning step after "
            "``iscsi.connect``."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "disk_number": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "The ``Get-Disk`` Number of the disk to provision.",
                },
                "drive_letter": {
                    "type": "string",
                    "pattern": "^[A-Za-z]$",
                    "description": "Optional drive letter to assign; omit to auto-assign.",
                },
                "filesystem": {
                    "type": "string",
                    "enum": sorted(_SUPPORTED_FILESYSTEMS),
                    "default": "NTFS",
                    "description": "File system: ``NTFS`` (default) or ``ReFS``.",
                },
                "label": {"type": "string", "description": "Optional volume label."},
                "force": {
                    "type": "boolean",
                    "description": (
                        "Reformat a disk that already carries a partition "
                        "table (DESTROYS its data). Default false — a "
                        "non-RAW disk is refused without this."
                    ),
                },
            },
            "required": ["disk_number"],
            "additionalProperties": False,
        },
        response_schema={
            "type": "object",
            "properties": {
                "disk_number": {"type": "integer"},
                "action": {"type": "string"},
                "drive_letter": {"type": ["string", "null"]},
                "filesystem": {"type": ["string", "null"]},
                "op_class": {"type": "string", "enum": ["write"]},
            },
            "required": ["disk_number", "action", "op_class"],
            "additionalProperties": True,
        },
        group_key="storage",
        tags=("write", "storage", "disk", "format"),
        safety_level="caution",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Provision a newly-attached raw disk (e.g. after "
                "``iscsi.connect``) into a formatted volume. Refuses a disk "
                "with an existing partition table unless force=true. "
                "safety_level=caution. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {
                "disk_number": "Required. The Get-Disk Number.",
                "drive_letter": "Optional single letter; omit to auto-assign.",
                "filesystem": "Optional; NTFS (default) or ReFS.",
                "force": "Optional bool; required to reformat a non-RAW disk.",
            },
            "output_shape": (
                "{'disk_number', 'action': 'format', 'drive_letter', "
                "'filesystem', 'op_class': 'write'}."
            ),
        },
    ),
)

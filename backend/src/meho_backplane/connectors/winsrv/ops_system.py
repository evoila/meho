# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""winsrv system reads — ``os-info`` / ``uptime`` / ``pending-reboot`` (all safe).

Read-only system facts via ``Get-CimInstance Win32_OperatingSystem`` and the
pending-reboot marker registry keys, routed through the shared
PowerShell-over-SSH transport
(:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`). No operator input
is interpolated into any of these scripts (they are constants), so the
system-read group has no injection surface.

References
----------

* ``Win32_OperatingSystem`` CIM class:
  https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-operatingsystem
* Pending-reboot markers (CBS RebootPending, WU RebootRequired,
  PendingFileRenameOperations) — the widely-documented detection set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meho_backplane.connectors._shared.pwsh import pwsh_run
from meho_backplane.connectors.winsrv.ops import SSH_TRANSPORT_NOTE, WinsrvOp

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.connectors.winsrv.connector import WinsrvConnector

__all__ = [
    "SYSTEM_OPS",
    "winsrv_os_info",
    "winsrv_pending_reboot",
    "winsrv_uptime",
]


_OS_INFO_SCRIPT: str = (
    "$os = Get-CimInstance -ClassName Win32_OperatingSystem; "
    "ConvertTo-Json -Depth 3 -Compress -InputObject @{ "
    "Hostname = [System.Net.Dns]::GetHostName(); "
    "Caption = $os.Caption; "
    "Version = $os.Version; "
    "BuildNumber = $os.BuildNumber; "
    "OSArchitecture = $os.OSArchitecture; "
    "InstallDate = if ($os.InstallDate) { $os.InstallDate.ToString('o') } else { $null }; "
    "LastBootUpTime = if ($os.LastBootUpTime) { $os.LastBootUpTime.ToString('o') } else { $null }; "
    "PowerShellVersion = $PSVersionTable.PSVersion.ToString() }"
)

_UPTIME_SCRIPT: str = (
    "$os = Get-CimInstance -ClassName Win32_OperatingSystem; "
    "$boot = $os.LastBootUpTime; "
    "$span = (Get-Date) - $boot; "
    "ConvertTo-Json -Compress -InputObject @{ "
    "LastBootUpTime = if ($boot) { $boot.ToString('o') } else { $null }; "
    "UptimeSeconds = [int64]$span.TotalSeconds; "
    "UptimeDays = [int]$span.Days }"
)

# Each marker is a read-only Test-Path / property probe; any True flips the
# aggregate ``pending_reboot`` to true. ``PendingFileRenameOperations`` is a
# value under the Session Manager key, so it is probed with Get-ItemProperty.
_PENDING_REBOOT_SCRIPT: str = (
    "$cbs = Test-Path "
    "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based "
    "Servicing\\RebootPending'; "
    "$wu = Test-Path "
    "'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\"
    "Auto Update\\RebootRequired'; "
    "$pfro = [bool]((Get-ItemProperty "
    "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager' "
    "-Name PendingFileRenameOperations -ErrorAction SilentlyContinue)"
    ".PendingFileRenameOperations); "
    "ConvertTo-Json -Compress -InputObject @{ "
    "pending_reboot = ($cbs -or $wu -or $pfro); "
    "component_based_servicing = $cbs; "
    "windows_update = $wu; "
    "pending_file_rename = $pfro }"
)


async def winsrv_os_info(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.system.os-info`` — the full CIM OS projection.

    Runs ``Get-CimInstance Win32_OperatingSystem`` and returns the caption,
    version, build number, architecture, install date, last-boot time, and
    PowerShell version. Read-only.
    """
    del params  # declared empty in schema; intentionally ignored
    payload = await pwsh_run(connector, target, _OS_INFO_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def winsrv_uptime(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.system.uptime`` — derived from LastBootUpTime.

    Runs a script that reads ``Win32_OperatingSystem.LastBootUpTime`` and
    returns the boot time (ISO-8601) plus the elapsed uptime in seconds and
    whole days. Read-only.
    """
    del params  # declared empty in schema; intentionally ignored
    payload = await pwsh_run(connector, target, _UPTIME_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


async def winsrv_pending_reboot(
    connector: WinsrvConnector,
    target: Any,
    params: dict[str, Any],
    operator: Operator | None = None,
) -> dict[str, Any]:
    """Handler for ``winsrv.system.pending-reboot`` — marker-registry probe.

    Probes the three widely-documented pending-reboot markers (Component
    Based Servicing ``RebootPending``, Windows Update ``RebootRequired``,
    Session Manager ``PendingFileRenameOperations``) and returns the
    aggregate ``pending_reboot`` boolean plus the per-marker breakdown.
    Read-only.
    """
    del params  # declared empty in schema; intentionally ignored
    payload = await pwsh_run(connector, target, _PENDING_REBOOT_SCRIPT, operator=operator)
    return payload if isinstance(payload, dict) else {"value": payload}


_EMPTY_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


SYSTEM_OPS: tuple[WinsrvOp, ...] = (
    WinsrvOp(
        op_id="winsrv.system.os-info",
        handler_attr="winsrv_os_info",
        summary="Read the full Win32_OperatingSystem CIM projection.",
        description=(
            "Runs ``Get-CimInstance Win32_OperatingSystem`` and returns the "
            "OS caption, version, build number, architecture, install date, "
            "last-boot time, and PowerShell version. Read-only; a superset "
            "of ``winsrv.about`` for callers that need the full OS facts."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema={
            "type": "object",
            "properties": {
                "Hostname": {"type": ["string", "null"]},
                "Caption": {"type": ["string", "null"]},
                "Version": {"type": ["string", "null"]},
                "BuildNumber": {"type": ["string", "null"]},
                "OSArchitecture": {"type": ["string", "null"]},
                "InstallDate": {"type": ["string", "null"]},
                "LastBootUpTime": {"type": ["string", "null"]},
                "PowerShellVersion": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        },
        group_key="system",
        tags=("read-only", "system", "os"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call for the full OS identity of a Windows Server host — "
                "caption, version, build, architecture, install/boot times. "
                "Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "Flat dict of CIM OS fields (Caption, Version, BuildNumber, "
                "OSArchitecture, InstallDate, LastBootUpTime, ...)."
            ),
        },
    ),
    WinsrvOp(
        op_id="winsrv.system.uptime",
        handler_attr="winsrv_uptime",
        summary="Report host uptime derived from Win32_OperatingSystem.LastBootUpTime.",
        description=(
            "Reads ``Win32_OperatingSystem.LastBootUpTime`` and returns the "
            "boot timestamp (ISO-8601) plus the elapsed uptime in seconds "
            "and whole days. Read-only."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema={
            "type": "object",
            "properties": {
                "LastBootUpTime": {"type": ["string", "null"]},
                "UptimeSeconds": {"type": "integer"},
                "UptimeDays": {"type": "integer"},
            },
            "additionalProperties": True,
        },
        group_key="system",
        tags=("read-only", "system", "uptime"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call to check how long a Windows Server host has been up "
                "(e.g. to confirm a reboot landed, or diagnose an unexpected "
                "restart). Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": "{'LastBootUpTime', 'UptimeSeconds', 'UptimeDays'}.",
        },
    ),
    WinsrvOp(
        op_id="winsrv.system.pending-reboot",
        handler_attr="winsrv_pending_reboot",
        summary="Report whether the host has a pending reboot (marker-registry probe).",
        description=(
            "Probes the Component Based Servicing ``RebootPending``, Windows "
            "Update ``RebootRequired``, and Session Manager "
            "``PendingFileRenameOperations`` markers and returns the "
            "aggregate ``pending_reboot`` boolean plus the per-marker "
            "breakdown. Read-only — the right pre-flight before a feature "
            "install or a governed reboot."
        ),
        parameter_schema=_EMPTY_PARAMS_SCHEMA,
        response_schema={
            "type": "object",
            "properties": {
                "pending_reboot": {"type": "boolean"},
                "component_based_servicing": {"type": "boolean"},
                "windows_update": {"type": "boolean"},
                "pending_file_rename": {"type": "boolean"},
            },
            "required": ["pending_reboot"],
            "additionalProperties": True,
        },
        group_key="system",
        tags=("read-only", "system", "reboot"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": (
                "Call before a feature install or a governed reboot to check "
                "whether a reboot is already pending, or after one to confirm "
                "the marker cleared. Read-only. " + SSH_TRANSPORT_NOTE
            ),
            "parameter_hints": {},
            "output_shape": (
                "{'pending_reboot': <bool>, 'component_based_servicing', "
                "'windows_update', 'pending_file_rename'}."
            ),
        },
    ),
)

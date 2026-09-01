# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Typed operations exposed by :class:`WinsrvConnector`.

The connector manages **Windows Server core** — system facts, service and
feature management, reboot/shutdown, local users, and disk / iSCSI-initiator
storage — over SSH → PowerShell (``powershell`` running Windows PowerShell
5.1 cmdlets), routed through
:func:`~meho_backplane.connectors._shared.pwsh.pwsh_run` (the shared
PowerShell-over-SSH transport hoisted by #3260). Structurally it mirrors the
windows_dns connector (identity canary + per-domain op groups on the
``SshConnector`` base) and adopts the rke2 approval-parked-write mold for
its destructive ops.

Op surface (23 ops across six groups; safety tier per the Initiative #3259
satellite table — reads ``safe``, recoverable writes ``caution``,
destructive ``dangerous`` + ``requires_approval``):

* ``winsrv.about``               [safe]      identity canary (CIM OS + PS version).
* ``winsrv.system.os-info``      [safe]      Get-CimInstance Win32_OperatingSystem.
* ``winsrv.system.uptime``       [safe]      LastBootUpTime → uptime.
* ``winsrv.system.pending-reboot`` [safe]    pending-reboot registry markers.
* ``winsrv.service.list``        [safe]      Get-Service (list-shaped → JSONFlux).
* ``winsrv.service.get``         [safe]      Get-Service -Name.
* ``winsrv.service.start``       [caution]   Start-Service.
* ``winsrv.service.stop``        [caution]   Stop-Service.
* ``winsrv.service.restart``     [caution]   Restart-Service.
* ``winsrv.feature.list``        [safe]      Get-WindowsFeature (list-shaped → JSONFlux).
* ``winsrv.feature.install``     [caution]   Install-WindowsFeature.
* ``winsrv.feature.remove``      [caution]   Uninstall-WindowsFeature.
* ``winsrv.power.reboot``        [dangerous+approval]  Restart-Computer -Force.
* ``winsrv.power.shutdown``      [dangerous+approval]  Stop-Computer -Force.
* ``winsrv.localuser.list``      [safe]      Get-LocalUser (list-shaped → JSONFlux).
* ``winsrv.localuser.create``    [caution]   New-LocalUser -NoPassword.
* ``winsrv.localuser.set``       [caution]   Set-LocalUser / Enable-/Disable-LocalUser.
* ``winsrv.localuser.delete``    [dangerous+approval]  Remove-LocalUser.
* ``winsrv.storage.disk.list``   [safe]      Get-Disk (list-shaped → JSONFlux).
* ``winsrv.storage.volume.list`` [safe]      Get-Volume (list-shaped → JSONFlux).
* ``winsrv.storage.iscsi.list``  [safe]      Get-IscsiTarget (list-shaped → JSONFlux).
* ``winsrv.storage.iscsi.connect`` [caution] Connect-IscsiTarget.
* ``winsrv.storage.disk.format`` [caution]   Initialize-Disk + New-Partition + Format-Volume.

The dataclass + tuple shape mirrors
:mod:`~meho_backplane.connectors.windows_dns.ops` and
:mod:`~meho_backplane.connectors.rke2.ops` so the registration walk reads
identically across SSH-transport connectors. The composition pattern
(``_winsrv_ops()`` importing per-domain op tuples) mirrors windows_dns's
``ops.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "SSH_TRANSPORT_NOTE",
    "WINSRV_OPS",
    "WINSRV_WHEN_TO_USE_BY_GROUP",
    "WinsrvOp",
    "normalise_json_rows",
]


#: Curated ``when_to_use`` strings per group key, consumed by
#: :meth:`WinsrvConnector.register_operations` (imported into the connector,
#: the rke2 ``RKE2_WHEN_TO_USE_WRITE_BY_GROUP`` precedent — the blurbs live
#: with the op metadata, not the transport class). Each entry covers a
#: ``group_key`` declared across the winsrv op tuples; the registration walk
#: fails closed with a :class:`ValueError` if a ``group_key`` lacks a curated
#: entry (the windows_dns / rke2 precedent).
WINSRV_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "system": (
        "Use for Windows Server host facts before any higher-level drill-in: "
        "identity (``winsrv.about``), the full CIM OS projection "
        "(``winsrv.system.os-info``), uptime (``winsrv.system.uptime``), and "
        "whether a reboot is pending (``winsrv.system.pending-reboot``). All "
        "read-only. Call ``winsrv.about`` first to confirm the host is "
        "reachable over SSH + PowerShell; check ``pending-reboot`` before a "
        "feature install or a governed reboot."
    ),
    "services": (
        "Use to inventory and control Windows services: list all "
        "(``winsrv.service.list``) or read one (``winsrv.service.get``) -- "
        "both read-only -- and start / stop / restart a named service "
        "(``caution``). The right group to check or bounce a service (e.g. "
        "``MSSQLSERVER``) around a config change."
    ),
    "features": (
        "Use to inventory and manage Windows roles/features: list "
        "(``winsrv.feature.list``, read-only) and install / remove "
        "(``caution``) via the ServerManager cmdlets. This is the c1sql1 "
        "Failover-Clustering install path. Install/remove never auto-restart "
        "-- check ``restart_needed`` and use the approval-gated "
        "``winsrv.power.reboot``."
    ),
    "power": (
        "Use to reboot or shut down a Windows Server host. BOTH ops are "
        "``dangerous`` + ``requires_approval`` -- a dispatch parks for a "
        "human decision before anything happens, and the tier ladder "
        "excludes them from satellite runners (central-dial / on-site only). "
        "Reboot lands a pending feature-install reboot; shutdown leaves the "
        "host off until powered back on out of band."
    ),
    "localusers": (
        "Use to inventory and manage local user accounts: list "
        "(``winsrv.localuser.list``, read-only, no password material), create "
        "/ set attributes (``caution`` -- passwordless; a plaintext password "
        "cannot ride the pwsh transport), and delete (``dangerous`` + "
        "``requires_approval`` -- irreversible removal parks for approval)."
    ),
    "storage": (
        "Use to inventory and provision storage: list disks / volumes / iSCSI "
        "targets (read-only), connect an iSCSI target, and format a raw disk "
        "into a volume (both ``caution``). This is the SQL-FCI shared-disk "
        "path: ``iscsi.connect`` then ``disk.format`` (which refuses a "
        "non-RAW disk unless force=true). No CHAP in this cut."
    ),
}


#: Canonical SSH-only / pwsh transport reminder copied into every op's
#: ``llm_instructions``. Mirrors windows_dns's ``SSH_TRANSPORT_NOTE`` — the
#: agent-facing descriptions must call out the PowerShell-over-SSH transport
#: so an LLM doesn't compose against a non-existent REST surface (Windows
#: Server core management has no unified REST API).
SSH_TRANSPORT_NOTE: str = (
    "Windows Server core management has no unified REST API; the underlying "
    "transport is PowerShell-over-SSH (powershell -EncodedCommand routed "
    "through asyncssh) driving Windows PowerShell 5.1 cmdlets."
)


@dataclass(frozen=True)
class WinsrvOp:
    """Metadata for one winsrv op the connector registers at startup.

    Fields mirror the keyword arguments
    :func:`~meho_backplane.operations.typed_register.register_typed_operation`
    accepts so the connector's ``register_operations()`` classmethod can
    splat the dataclass into the helper without per-op boilerplate.
    ``handler_attr`` is the attribute name on
    :class:`~meho_backplane.connectors.winsrv.connector.WinsrvConnector`
    that exposes the async handler; the connector resolves the bound method
    against itself at registration time so the dispatcher's
    :func:`~meho_backplane.operations._handler_resolve.import_handler` walk
    recovers the callable from the persisted ``module.ClassName.method``
    dotted path.
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

    PowerShell's ``ConvertTo-Json`` renders a **single**-element result as a
    flat object and a **multi**-element result as a JSON array; a
    zero-element result renders as ``null`` (an empty stdout is caught
    upstream by :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`
    before it reaches here). This helper collapses all three shapes to a
    ``list[dict]`` so the list handlers walk a uniform structure — the
    windows_dns ``normalise_json_rows`` shape, shared across every
    winsrv list op so the ``{rows, total}`` envelope the JSONFlux reducer
    keys on is built identically.
    """
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


#: The identity canary op. ``winsrv.about`` is the operator-facing wrapper
#: around :meth:`WinsrvConnector.fingerprint` — same vendor / product /
#: version / host payload, surfaced through the typed-op dispatcher so
#: callers see the standard :class:`OperationResult` envelope instead of the
#: raw :class:`FingerprintResult`. Mirrors windows_dns's ``windns.about``.
_WINSRV_ABOUT_OP = WinsrvOp(
    op_id="winsrv.about",
    handler_attr="about",
    summary="Return the Windows Server host's OS caption/version/build + PowerShell version.",
    description=(
        "Connects to the Windows Server host over SSH and runs a single "
        "``powershell -EncodedCommand`` script that reads the machine "
        "hostname, the ``Win32_OperatingSystem`` CIM instance (caption / "
        "version / build), and the Windows PowerShell version. Returns a "
        "flat dict with the vendor (``microsoft``), product "
        "(``windows-server``), the OS version + build, and the host name. "
        "Use to confirm the host is reachable over SSH + PowerShell and to "
        "read its OS identity before issuing higher-level system / service "
        "/ feature ops; no params; safe on any healthy target."
    ),
    parameter_schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
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
        },
        "required": ["vendor", "product"],
        "additionalProperties": True,
    },
    group_key="system",
    tags=("read-only", "identity", "windows-server"),
    safety_level="safe",
    requires_approval=False,
    llm_instructions={
        "when_to_use": (
            "Call when the operator wants to identify the Windows Server "
            "host behind a target — its OS caption / version / build — or "
            "to confirm the host is reachable via SSH + PowerShell before "
            "issuing higher-level system / service / feature ops. " + SSH_TRANSPORT_NOTE
        ),
        "parameter_hints": {},
        "output_shape": (
            "Flat dict; ``version`` carries the OS version (e.g. "
            "``10.0.20348``), ``build`` the build number (e.g. ``20348``), "
            "``os_caption`` the friendly caption (e.g. ``Microsoft Windows "
            "Server 2022 Datacenter``), ``hostname`` the machine name."
        ),
    },
)


def _winsrv_ops() -> tuple[WinsrvOp, ...]:
    """Return the merged registration tuple.

    Composition: ``winsrv.about`` (identity canary) + the per-domain op
    tuples (system / services / features / power / localusers / storage).
    Implemented as a function call rather than a literal-and-splat at module
    level so the import order stays linear: ``ops.py`` defines
    :class:`WinsrvOp` + ``_WINSRV_ABOUT_OP`` + :func:`normalise_json_rows`,
    then imports the per-domain op tuples from their modules (each of which
    only depends on this module plus its own helpers). Mirrors
    :func:`meho_backplane.connectors.windows_dns.ops._windows_dns_ops`.
    """
    from meho_backplane.connectors.winsrv.ops_features import FEATURE_OPS
    from meho_backplane.connectors.winsrv.ops_localusers import LOCALUSER_OPS
    from meho_backplane.connectors.winsrv.ops_power import POWER_OPS
    from meho_backplane.connectors.winsrv.ops_services import SERVICE_OPS
    from meho_backplane.connectors.winsrv.ops_storage import STORAGE_OPS
    from meho_backplane.connectors.winsrv.ops_system import SYSTEM_OPS

    return (
        _WINSRV_ABOUT_OP,
        *SYSTEM_OPS,
        *SERVICE_OPS,
        *FEATURE_OPS,
        *POWER_OPS,
        *LOCALUSER_OPS,
        *STORAGE_OPS,
    )


#: The ops :class:`WinsrvConnector` registers at lifespan startup. The shape
#: of each follow-on op is "import a new module-level tuple and splat it into
#: :data:`WINSRV_OPS` via :func:`_winsrv_ops`" — the registration walk in
#: :meth:`WinsrvConnector.register_operations` does not change.
WINSRV_OPS: tuple[WinsrvOp, ...] = _winsrv_ops()

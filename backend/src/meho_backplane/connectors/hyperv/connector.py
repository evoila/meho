# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""HypervConnector -- typed SSH-transport connector for the Hyper-V migration source.

Reads a Hyper-V host's inventory, VM configuration, and virtual-disk / checkpoint
facts -- the inputs to a Hyper-V→VMware migration plan -- plus the guarded export
that seeds that migration, over SSH → PowerShell: the Hyper-V host runs an
OpenSSH server whose shell drives ``powershell`` (Windows PowerShell 5.1)
executing the built-in ``Hyper-V`` module cmdlets. The connector is a structured
copy of :class:`~meho_backplane.connectors.winsrv.connector.WinsrvConnector` (the
estate mold -- same ``SshConnector`` base + shared PowerShell-over-SSH transport
:mod:`~meho_backplane.connectors._shared.pwsh`) and adopts the
:mod:`~meho_backplane.connectors.rke2.ops_write` approval-parked-write mold for
its destructive ops (checkpoint revert / delete).

The **target is a single Hyper-V host** (standalone or a cluster node).
Cluster-wide views of a Hyper-V *cluster*'s nodes belong to the
:class:`~meho_backplane.connectors.wsfc.connector.WsfcConnector` (#3263) on the
same nodes -- not duplicated here. Explicit non-goals in this increment: VM
create / delete on Hyper-V, SCVMM, and Hyper-V Replica.

This module ships :class:`HypervConnector` (registry-v2 triple ``("hyperv",
"2022.x", "hyperv-ssh")``): :meth:`fingerprint` (one round-trip reading the
hostname + OS + Hyper-V module presence + hypervisor state), :meth:`probe` (six
distinct ``ProbeResult.reason`` values, two of them Hyper-V specific),
:meth:`about` (the ``hyperv.about`` op), the bound-method op shims for the six
groups, and the operator-less :meth:`execute` dispatcher shim (same shape as
winsrv / wsfc / windows_dns / rke2).

Like winsrv, this connector sets ``POWERSHELL_EXECUTABLE = "powershell"``
explicitly: the shared transport's fallback is ``pwsh`` (PS7), which a Windows
Server / Hyper-V host does NOT ship (see ``docs/codebase/connectors-winsrv.md``
-- "building the next estate connector").
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import asyncssh
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.pwsh import PwshRunError, pwsh_run
from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.adapters.ssh import SshConnector
from meho_backplane.connectors.hyperv.ops import HYPERV_OPS, HYPERV_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["HypervConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- mirrors the placeholder in the SSH adapter and the
# winsrv / wsfc / windows_dns / rke2 siblings.
type Target = Any


#: The fingerprint / about script -- hostname + OS version/build + PowerShell
#: version + Hyper-V module presence/version + hypervisor state in one
#: round-trip. No operator input is interpolated (constant script), so the
#: identity path has no injection surface.
_FINGERPRINT_SCRIPT: str = (
    "$os = Get-CimInstance -ClassName Win32_OperatingSystem; "
    "$cs = Get-CimInstance -ClassName Win32_ComputerSystem; "
    "$mod = Get-Module -ListAvailable -Name Hyper-V | Select-Object -First 1; "
    "ConvertTo-Json -Compress -InputObject @{ "
    "Hostname = [System.Net.Dns]::GetHostName(); "
    "Caption = $os.Caption; "
    "OsVersion = $os.Version; "
    "BuildNumber = $os.BuildNumber; "
    "PowerShellVersion = $PSVersionTable.PSVersion.ToString(); "
    "HyperVModule = [bool]$mod; "
    "HyperVModuleVersion = if ($mod) { $mod.Version.ToString() } else { $null }; "
    "HypervisorPresent = [bool]$cs.HypervisorPresent }"
)

#: The probe script -- Hyper-V module presence + hypervisor-running state. Always
#: emits JSON. ``Win32_ComputerSystem.HypervisorPresent`` is True when the
#: hypervisor is actually running, which distinguishes an installed-but-not-a-
#: Hyper-V-host box from a live Hyper-V host.
_PROBE_SCRIPT: str = (
    "$mod = [bool](Get-Module -ListAvailable -Name Hyper-V); "
    "$cs = Get-CimInstance -ClassName Win32_ComputerSystem -ErrorAction SilentlyContinue; "
    "$hyp = [bool]($cs.HypervisorPresent); "
    "ConvertTo-Json -Compress -InputObject @{ module = $mod; hypervisor = $hyp }"
)


class HypervConnector(SshConnector):
    """Hyper-V migration-source connector on the :class:`SshConnector` adapter.

    Registry v2 triple: ``("hyperv", "2022.x", "hyperv-ssh")``.

    * ``product="hyperv"`` -- a short, separator-free product token (the repo
      convention: ``parse_connector_id`` derives the product from the first
      hyphen-segment of ``impl_id``, so ``connector_id="hyperv-ssh-2022.x"``
      round-trips; ``hyper-v`` -- with a hyphen -- would break the registry's
      round-trip guard at boot).
    * ``version="2022.x"`` -- the ``Hyper-V`` cmdlet surface targets Windows
      Server 2022 (stable across 2019 → 2025).
    * ``impl_id="hyperv-ssh"`` -- leaves room for a future ``hyperv-winrm``
      sibling.

    **Auth: password-default + key-fallback.** Inherits the base
    :class:`SshConnector` ``_auth_config`` unchanged.

    **Transport: PowerShell-over-SSH.** Cmdlets reach the host through
    ``powershell -EncodedCommand <base64-utf16le>`` (Windows PowerShell 5.1 --
    see :attr:`POWERSHELL_EXECUTABLE`) routed by
    :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`; output is parsed
    via stdlib :mod:`json` from the cmdlet's ``ConvertTo-Json`` pipe.
    """

    product = "hyperv"
    version = "2022.x"
    impl_id = "hyperv-ssh"

    #: The PowerShell executable the shared transport invokes. ``powershell``
    #: (Windows PowerShell 5.1) -- NOT ``pwsh`` (PS7). Set explicitly because the
    #: shared transport's fallback is ``pwsh``; every Windows-estate connector
    #: MUST set this (see the connectors-winsrv doc).
    POWERSHELL_EXECUTABLE = "powershell"

    #: Prepended to every script so the Hyper-V module's first-use progress
    #: stream does not CLIXML-serialise onto the remote streams.
    POWERSHELL_SCRIPT_PREFIX = "$ProgressPreference = 'SilentlyContinue'; "

    #: The structured-log event name the shared transport emits per run, kept
    #: connector-scoped so log queries stay per-connector.
    POWERSHELL_LOG_EVENT = "hyperv_pwsh_executed"

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read the host OS + Hyper-V module + hypervisor state -- the fingerprint.

        Runs :data:`_FINGERPRINT_SCRIPT` (one round-trip) and parses the JSON
        object. Unreachable / SSH-failed / cmdlet-failed → ``reachable=False`` +
        ``extras["error"]`` (the #986 discipline). ``operator`` is threaded to
        the SSH adapter for the Vault read on a pool miss; ``None`` fails closed.
        Whether the Hyper-V role is *present* is metadata here
        (``hyperv_module`` / ``hypervisor_present``); :meth:`probe` is where its
        absence becomes a distinct reason.
        """
        probed_at = datetime.now(UTC)
        method = "ssh: powershell Get-Module Hyper-V / Win32_ComputerSystem"
        try:
            payload = await pwsh_run(self, target, _FINGERPRINT_SCRIPT, operator=operator)
        except (
            OSError,
            asyncssh.Error,
            ValueError,
            VaultClientError,
            CredentialsReadError,
            PwshRunError,
        ) as exc:
            _log.warning(
                "hyperv_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="microsoft",
                product="hyper-v",
                reachable=False,
                probed_at=probed_at,
                probe_method=method,
                extras={"error": str(exc)},
            )

        data = payload if isinstance(payload, dict) else {}
        return FingerprintResult(
            vendor="microsoft",
            product="hyper-v",
            version=_as_str(data.get("OsVersion")),
            build=_as_str(data.get("BuildNumber")),
            reachable=True,
            probed_at=probed_at,
            probe_method=method,
            extras={
                "hostname": _as_str(data.get("Hostname")),
                "os_caption": _as_str(data.get("Caption")),
                "powershell_version": _as_str(data.get("PowerShellVersion")),
                "hyperv_module": _as_bool(data.get("HyperVModule")),
                "hyperv_module_version": _as_str(data.get("HyperVModuleVersion")),
                "hypervisor_present": _as_bool(data.get("HypervisorPresent")),
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + PowerShell + Hyper-V module + hypervisor check.

        Failure modes (each surfaces a distinct ``reason``):

        * ``tcp_unreachable`` -- the SSH TCP socket cannot connect.
        * ``ssh_auth_failed`` -- credentials rejected, the handshake failed, or
          the Vault credential could not be resolved.
        * ``powershell_unavailable`` -- SSH succeeded but the reachability script
          failed (``powershell`` not on the shell, or a pwsh error).
        * ``command_failed`` -- a post-connect transport failure (connection drop
          / timeout / socket error) after a successful handshake.
        * ``hyperv_module_absent`` -- PowerShell runs but the ``Hyper-V`` module
          is not installed (not a Hyper-V host).
        * ``hypervisor_role_absent`` -- the module is present but the hypervisor
          is not running (``Win32_ComputerSystem.HypervisorPresent`` is false --
          the Hyper-V role is not enabled / the host has not booted into it).

        The probe does not mutate state -- both reads are read-only.
        """
        start = time.monotonic()
        probed_at = datetime.now(UTC)

        def _result(ok: bool, reason: str | None) -> ProbeResult:
            latency_ms = (time.monotonic() - start) * 1000.0
            return ProbeResult(ok=ok, reason=reason, latency_ms=latency_ms, probed_at=probed_at)

        # Order matters: PermissionDenied (subclass of DisconnectError) before
        # DisconnectError; OSError is the TCP-level failure.
        try:
            await self._connect(target)
        except asyncssh.PermissionDenied:
            return _result(False, "ssh_auth_failed")
        except asyncssh.DisconnectError:
            return _result(False, "ssh_auth_failed")
        except OSError:
            return _result(False, "tcp_unreachable")
        except (ValueError, VaultClientError, CredentialsReadError):
            return _result(False, "ssh_auth_failed")

        try:
            payload = await pwsh_run(self, target, _PROBE_SCRIPT)
        except PwshRunError:
            return _result(False, "powershell_unavailable")
        except (OSError, asyncssh.Error):
            return _result(False, "command_failed")

        data = payload if isinstance(payload, dict) else {}
        if data.get("module") is not True:
            return _result(False, "hyperv_module_absent")
        if data.get("hypervisor") is not True:
            return _result(False, "hypervisor_role_absent")
        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Return the Hyper-V host's identity snapshot.

        Op-id: ``hyperv.about``. Reuses :meth:`fingerprint` so the
        operator-facing op and the canonical fingerprint share one round-trip.
        ``_assert_reachable`` re-raises an unreachable fingerprint as a
        :exc:`~meho_backplane.connectors.adapters.ssh.ConnectorUnreachableError`
        so the dispatcher reports a non-ok op rather than empty identity fields
        (#986).
        """
        del params  # declared empty in schema; intentionally ignored
        result = await self.fingerprint(target, operator)
        self._assert_reachable(result)
        return {
            "vendor": result.vendor,
            "product": result.product,
            "version": result.version,
            "build": result.build,
            "hostname": result.extras.get("hostname"),
            "os_caption": result.extras.get("os_caption"),
            "powershell_version": result.extras.get("powershell_version"),
            "hyperv_module": result.extras.get("hyperv_module"),
            "hyperv_module_version": result.extras.get("hyperv_module_version"),
            "hypervisor_present": result.extras.get("hypervisor_present"),
        }

    # -- host group shims ---------------------------------------------------

    async def hyperv_host_info(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.host.info``."""
        from meho_backplane.connectors.hyperv.ops_host import hyperv_host_info as _h

        return await _h(self, target, params, operator)

    async def hyperv_host_numa(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.host.numa``."""
        from meho_backplane.connectors.hyperv.ops_host import hyperv_host_numa as _h

        return await _h(self, target, params, operator)

    async def hyperv_host_vswitch_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.host.vswitch.list``."""
        from meho_backplane.connectors.hyperv.ops_host import hyperv_host_vswitch_list as _h

        return await _h(self, target, params, operator)

    # -- vms group shims ----------------------------------------------------

    async def hyperv_vms_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.vms.list``."""
        from meho_backplane.connectors.hyperv.ops_vms import hyperv_vms_list as _h

        return await _h(self, target, params, operator)

    async def hyperv_vms_get(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.vms.get``."""
        from meho_backplane.connectors.hyperv.ops_vms import hyperv_vms_get as _h

        return await _h(self, target, params, operator)

    async def hyperv_vms_config(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.vms.config``."""
        from meho_backplane.connectors.hyperv.ops_vms import hyperv_vms_config as _h

        return await _h(self, target, params, operator)

    async def hyperv_vms_state(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.vms.state``."""
        from meho_backplane.connectors.hyperv.ops_vms import hyperv_vms_state as _h

        return await _h(self, target, params, operator)

    # -- disks group shims --------------------------------------------------

    async def hyperv_disks_vm_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.disks.vm.list``."""
        from meho_backplane.connectors.hyperv.ops_disks import hyperv_disks_vm_list as _h

        return await _h(self, target, params, operator)

    async def hyperv_disks_vhd_get(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.disks.vhd.get``."""
        from meho_backplane.connectors.hyperv.ops_disks import hyperv_disks_vhd_get as _h

        return await _h(self, target, params, operator)

    async def hyperv_disks_vhd_chain(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.disks.vhd.chain``."""
        from meho_backplane.connectors.hyperv.ops_disks import hyperv_disks_vhd_chain as _h

        return await _h(self, target, params, operator)

    # -- checkpoints group shims -------------------------------------------

    async def hyperv_checkpoints_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.checkpoints.list``."""
        from meho_backplane.connectors.hyperv.ops_checkpoints import hyperv_checkpoints_list as _h

        return await _h(self, target, params, operator)

    async def hyperv_checkpoints_create(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.checkpoints.create`` (caution)."""
        from meho_backplane.connectors.hyperv.ops_checkpoints import hyperv_checkpoints_create as _h

        return await _h(self, target, params, operator)

    async def hyperv_checkpoints_revert(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.checkpoints.revert`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.hyperv.ops_checkpoints import hyperv_checkpoints_revert as _h

        return await _h(self, target, params, operator)

    async def hyperv_checkpoints_delete(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.checkpoints.delete`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.hyperv.ops_checkpoints import hyperv_checkpoints_delete as _h

        return await _h(self, target, params, operator)

    # -- export group shim --------------------------------------------------

    async def hyperv_export_vm(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.export.vm`` (caution; long-running)."""
        from meho_backplane.connectors.hyperv.ops_export import hyperv_export_vm as _h

        return await _h(self, target, params, operator)

    # -- power group shims --------------------------------------------------

    async def hyperv_power_start(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.power.start`` (caution)."""
        from meho_backplane.connectors.hyperv.ops_power import hyperv_power_start as _h

        return await _h(self, target, params, operator)

    async def hyperv_power_stop(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``hyperv.power.stop`` (caution)."""
        from meho_backplane.connectors.hyperv.ops_power import hyperv_power_stop as _h

        return await _h(self, target, params, operator)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`HYPERV_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has eager-
        imported every connector module. Walks
        :data:`~meho_backplane.connectors.hyperv.ops.HYPERV_OPS` and routes each
        row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
        Idempotent across pod restarts -- mirrors the winsrv / wsfc shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in HYPERV_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"HypervConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = HYPERV_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"HypervConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated when_to_use "
                        f"exists for that key. Add an entry to "
                        f"HYPERV_WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.hyperv.ops."
                    )
            await register_typed_operation(
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                op_id=op.op_id,
                handler=handler,
                summary=op.summary,
                description=op.description,
                parameter_schema=op.parameter_schema,
                response_schema=op.response_schema,
                group_key=op.group_key,
                when_to_use=when_to_use,
                tags=list(op.tags),
                safety_level=op.safety_level,
                requires_approval=op.requires_approval,
                llm_instructions=op.llm_instructions,
            )
        _log.info(
            "hyperv_operations_registered",
            count=len(bindings),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def execute(
        self,
        target: Target,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Dispatcher shim -- delegate to the G0.6 lookup + invoke path.

        Mirrors :meth:`WinsrvConnector.execute`. Operator-less (no policy gate,
        no audit row, no broadcast); the operator-aware surface is
        ``POST /api/v1/operations/call`` via the G0.6 meta-tools.
        """
        from sqlalchemy import select

        from meho_backplane.db.engine import get_sessionmaker
        from meho_backplane.db.models import EndpointDescriptor
        from meho_backplane.operations._errors import (
            result_connector_error,
            result_invalid_params,
            result_unknown_op,
        )
        from meho_backplane.operations._handler_resolve import (
            import_handler,
            is_unbound_method,
        )
        from meho_backplane.operations._lookup import count_known_ops
        from meho_backplane.operations._validate import validate_params

        start = time.monotonic()

        def _elapsed() -> float:
            return (time.monotonic() - start) * 1000.0

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            result = await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.product == self.product,
                    EndpointDescriptor.version == self.version,
                    EndpointDescriptor.impl_id == self.impl_id,
                    EndpointDescriptor.op_id == op_id,
                    EndpointDescriptor.is_enabled.is_(True),
                )
            )
            descriptor = result.scalar_one_or_none()

        if descriptor is None:
            known_op_count = await count_known_ops(
                tenant_id=None,  # operator-less chassis path: global rows only
                product=self.product,
                version=self.version,
                impl_id=self.impl_id,
            )
            return result_unknown_op(op_id, known_op_count, _elapsed())

        validation_errors = validate_params(descriptor.parameter_schema, params)
        if validation_errors:
            return result_invalid_params(op_id, validation_errors, _elapsed())

        handler = import_handler(descriptor.handler_ref or "")
        if is_unbound_method(handler, type(self)):
            handler = handler.__get__(self, type(self))

        try:
            raw = await handler(target=target, params=params)
        except Exception as exc:
            return result_connector_error(op_id, exc, _elapsed())

        return OperationResult(
            status="ok",
            op_id=op_id,
            result=raw if isinstance(raw, (dict, list)) else {"value": raw},
            duration_ms=_elapsed(),
        )


def _as_str(value: Any) -> str | None:
    """Return *value* when it is a non-empty ``str``, else ``None`` (guards the
    fingerprint projection: ``ConvertTo-Json`` can render an absent field as
    ``null``, and :class:`FingerprintResult` fields are typed ``str | None``)."""
    return value if isinstance(value, str) and value else None


def _as_bool(value: Any) -> bool | None:
    """Return *value* when it is a ``bool``, else ``None`` (guards the extras
    projection: an absent CIM / module field renders as ``null``)."""
    return value if isinstance(value, bool) else None

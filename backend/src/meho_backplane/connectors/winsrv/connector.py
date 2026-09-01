# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""WinsrvConnector -- typed SSH-transport connector for Windows Server core.

Manages Windows Server hosts (system facts, services, roles/features, reboot,
local users, disk / iSCSI storage) over SSH → PowerShell: the Windows host
runs an OpenSSH server whose shell drives ``powershell`` (Windows PowerShell
5.1) executing the built-in management cmdlets. The connector is a structural
sibling of
:class:`~meho_backplane.connectors.windows_dns.connector.WindowsDnsConnector`
(same ``SshConnector`` base + shared PowerShell-over-SSH transport
:mod:`~meho_backplane.connectors._shared.pwsh`) and adopts the
:mod:`~meho_backplane.connectors.rke2.ops_write` approval-parked-write mold
for its destructive ops. It is the *estate mold*: msad / wsfc / hyperv (the
rest of Initiative #3259) are structured copies of this package with
different cmdlet modules.

This module ships :class:`WinsrvConnector` (registry-v2 triple ``("winsrv",
"2022.x", "winsrv-ssh")``): :meth:`fingerprint` (one round-trip reading the
hostname + ``Win32_OperatingSystem`` CIM instance → ``vendor="microsoft"`` /
``product="windows-server"``), :meth:`probe` (five distinct
``ProbeResult.reason`` values), :meth:`about` (the ``winsrv.about`` op), the
bound-method op shims for the six groups, and the operator-less
:meth:`execute` dispatcher shim (same shape as windows_dns / rke2).

Building the next estate connector: the shared transport's
``POWERSHELL_EXECUTABLE`` fallback is ``pwsh`` (PS7), which a Windows Server
host does NOT ship. This connector sets ``POWERSHELL_EXECUTABLE =
"powershell"`` explicitly, and msad / wsfc / hyperv MUST do the same (see
``docs/codebase/connectors-winsrv.md`` -- "building the next estate
connector"). Auth uses the base ``SshConnector._auth_config`` unchanged.
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
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)
from meho_backplane.connectors.winsrv.ops import WINSRV_OPS, WINSRV_WHEN_TO_USE_BY_GROUP

__all__ = ["WinsrvConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- mirrors the placeholder in the SSH adapter and the
# windows_dns / rke2 siblings.
type Target = Any


#: The fingerprint / about script -- hostname + Win32_OperatingSystem CIM
#: fields + PowerShell version in one round-trip. No operator input is
#: interpolated (constant script), so the identity path has no injection
#: surface.
_FINGERPRINT_SCRIPT: str = (
    "$os = Get-CimInstance -ClassName Win32_OperatingSystem; "
    "ConvertTo-Json -Compress -InputObject @{ "
    "Hostname = [System.Net.Dns]::GetHostName(); "
    "Caption = $os.Caption; "
    "Version = $os.Version; "
    "BuildNumber = $os.BuildNumber; "
    "PowerShellVersion = $PSVersionTable.PSVersion.ToString() }"
)

#: The probe reachability script -- a minimal CIM-OS presence check that
#: always emits JSON.
_PROBE_SCRIPT: str = (
    "ConvertTo-Json -Compress -InputObject @{ present = [bool]("
    "Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue) }"
)


class WinsrvConnector(SshConnector):
    """Windows Server core connector built on the :class:`SshConnector` adapter.

    Registry v2 triple: ``("winsrv", "2022.x", "winsrv-ssh")``.

    * ``product="winsrv"`` -- a short, separator-free product token (the repo
      convention: ``parse_connector_id`` derives the product from the first
      hyphen-segment of ``impl_id``, so ``connector_id="winsrv-ssh-2022.x"``
      round-trips; a hyphen/underscore in the token breaks the registry's
      round-trip guard at boot).
    * ``version="2022.x"`` -- the cmdlet surface targets Windows Server 2022
      (stable across 2019 → 2025). A future release-specific impl can register
      alongside.
    * ``impl_id="winsrv-ssh"`` -- leaves room for a future ``winsrv-winrm``
      sibling once a non-SSH control surface lands.

    **Auth: password-default + key-fallback.** Inherits the base
    :class:`SshConnector` ``_auth_config`` unchanged.

    **Transport: PowerShell-over-SSH.** Cmdlets reach the host through
    ``powershell -EncodedCommand <base64-utf16le>`` (Windows PowerShell 5.1 --
    see :attr:`POWERSHELL_EXECUTABLE`) routed by
    :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`; output is parsed
    via stdlib :mod:`json` from the cmdlet's ``ConvertTo-Json`` pipe.
    """

    product = "winsrv"
    version = "2022.x"
    impl_id = "winsrv-ssh"

    #: The PowerShell executable the shared ``_shared.pwsh`` transport invokes.
    #: ``powershell`` (Windows PowerShell 5.1) -- NOT ``pwsh`` (PS7), which is
    #: absent by default on a Windows Server host. Set explicitly because the
    #: shared transport's fallback is ``pwsh``; the estate connectors (msad /
    #: wsfc / hyperv) MUST set this too (see the connector doc).
    POWERSHELL_EXECUTABLE = "powershell"

    #: Prepended to every script so the ServerManager / CIM modules' first-use
    #: progress stream does not CLIXML-serialise onto the remote streams (the
    #: windows_dns guard; the transport's ``strip_clixml`` net is a backstop).
    POWERSHELL_SCRIPT_PREFIX = "$ProgressPreference = 'SilentlyContinue'; "

    #: The structured-log event name the shared transport emits per run, kept
    #: connector-scoped so log queries stay per-connector.
    POWERSHELL_LOG_EVENT = "winsrv_pwsh_executed"

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read hostname + Win32_OperatingSystem -- the canonical fingerprint.

        Runs :data:`_FINGERPRINT_SCRIPT` (one round-trip) and parses the JSON
        object. Unreachable / SSH-failed / cmdlet-failed → ``reachable=False``
        + ``extras["error"]`` (the #986 discipline: an unresolvable credential
        is an unreachable target, not an unhandled exception). ``operator`` is
        threaded to the SSH adapter for the Vault read on a pool miss; ``None``
        fails closed.
        """
        probed_at = datetime.now(UTC)
        method = "ssh: powershell Get-CimInstance Win32_OperatingSystem"
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
                "winsrv_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="microsoft",
                product="windows-server",
                reachable=False,
                probed_at=probed_at,
                probe_method=method,
                extras={"error": str(exc)},
            )

        data = payload if isinstance(payload, dict) else {}
        return FingerprintResult(
            vendor="microsoft",
            product="windows-server",
            version=_as_str(data.get("Version")),
            build=_as_str(data.get("BuildNumber")),
            reachable=True,
            probed_at=probed_at,
            probe_method=method,
            extras={
                "hostname": _as_str(data.get("Hostname")),
                "os_caption": _as_str(data.get("Caption")),
                "powershell_version": _as_str(data.get("PowerShellVersion")),
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + PowerShell + CIM-OS readability check.

        Failure modes (each surfaces a distinct ``reason``):

        * ``tcp_unreachable`` -- the SSH TCP socket cannot connect.
        * ``ssh_auth_failed`` -- credentials rejected, the handshake failed,
          or the Vault credential could not be resolved.
        * ``powershell_unavailable`` -- SSH succeeded but the reachability
          script failed (``powershell`` not on the shell, or a pwsh error).
        * ``command_failed`` -- a post-connect transport failure (connection
          drop / timeout / socket error) after a successful handshake (the
          #986 post-connect guard).
        * ``os_query_failed`` -- PowerShell runs but ``Win32_OperatingSystem``
          is not readable (not a healthy Windows host, or WMI/CIM broken).

        The probe does not mutate state -- ``Get-CimInstance`` is read-only.
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

        present = payload.get("present") if isinstance(payload, dict) else None
        if present is not True:
            return _result(False, "os_query_failed")

        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Return the Windows Server host's identity snapshot.

        Op-id: ``winsrv.about``. Reuses :meth:`fingerprint` so the
        operator-facing op and the canonical fingerprint share one round-trip.
        ``_assert_reachable`` re-raises an unreachable fingerprint as a
        :exc:`~meho_backplane.connectors.adapters.ssh.ConnectorUnreachableError`
        so the dispatcher reports a non-ok op rather than empty identity
        fields (#986).
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
        }

    # -- system group shims -------------------------------------------------

    async def winsrv_os_info(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.system.os-info``."""
        from meho_backplane.connectors.winsrv.ops_system import winsrv_os_info as _h

        return await _h(self, target, params, operator)

    async def winsrv_uptime(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.system.uptime``."""
        from meho_backplane.connectors.winsrv.ops_system import winsrv_uptime as _h

        return await _h(self, target, params, operator)

    async def winsrv_pending_reboot(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.system.pending-reboot``."""
        from meho_backplane.connectors.winsrv.ops_system import winsrv_pending_reboot as _h

        return await _h(self, target, params, operator)

    # -- services group shims ----------------------------------------------

    async def winsrv_service_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.service.list``."""
        from meho_backplane.connectors.winsrv.ops_services import winsrv_service_list as _h

        return await _h(self, target, params, operator)

    async def winsrv_service_get(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.service.get``."""
        from meho_backplane.connectors.winsrv.ops_services import winsrv_service_get as _h

        return await _h(self, target, params, operator)

    async def winsrv_service_start(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.service.start`` (caution)."""
        from meho_backplane.connectors.winsrv.ops_services import winsrv_service_start as _h

        return await _h(self, target, params, operator)

    async def winsrv_service_stop(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.service.stop`` (caution)."""
        from meho_backplane.connectors.winsrv.ops_services import winsrv_service_stop as _h

        return await _h(self, target, params, operator)

    async def winsrv_service_restart(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.service.restart`` (caution)."""
        from meho_backplane.connectors.winsrv.ops_services import winsrv_service_restart as _h

        return await _h(self, target, params, operator)

    # -- features group shims ----------------------------------------------

    async def winsrv_feature_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.feature.list``."""
        from meho_backplane.connectors.winsrv.ops_features import winsrv_feature_list as _h

        return await _h(self, target, params, operator)

    async def winsrv_feature_install(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.feature.install`` (caution)."""
        from meho_backplane.connectors.winsrv.ops_features import winsrv_feature_install as _h

        return await _h(self, target, params, operator)

    async def winsrv_feature_remove(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.feature.remove`` (caution)."""
        from meho_backplane.connectors.winsrv.ops_features import winsrv_feature_remove as _h

        return await _h(self, target, params, operator)

    # -- power group shims (dangerous + requires_approval) -----------------

    async def winsrv_power_reboot(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.power.reboot`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.winsrv.ops_power import winsrv_power_reboot as _h

        return await _h(self, target, params, operator)

    async def winsrv_power_shutdown(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.power.shutdown`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.winsrv.ops_power import winsrv_power_shutdown as _h

        return await _h(self, target, params, operator)

    # -- localusers group shims --------------------------------------------

    async def winsrv_localuser_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.localuser.list``."""
        from meho_backplane.connectors.winsrv.ops_localusers import winsrv_localuser_list as _h

        return await _h(self, target, params, operator)

    async def winsrv_localuser_create(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.localuser.create`` (caution)."""
        from meho_backplane.connectors.winsrv.ops_localusers import winsrv_localuser_create as _h

        return await _h(self, target, params, operator)

    async def winsrv_localuser_set(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.localuser.set`` (caution)."""
        from meho_backplane.connectors.winsrv.ops_localusers import winsrv_localuser_set as _h

        return await _h(self, target, params, operator)

    async def winsrv_localuser_delete(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.localuser.delete`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.winsrv.ops_localusers import winsrv_localuser_delete as _h

        return await _h(self, target, params, operator)

    # -- storage group shims -----------------------------------------------

    async def winsrv_disk_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.storage.disk.list``."""
        from meho_backplane.connectors.winsrv.ops_storage import winsrv_disk_list as _h

        return await _h(self, target, params, operator)

    async def winsrv_volume_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.storage.volume.list``."""
        from meho_backplane.connectors.winsrv.ops_storage import winsrv_volume_list as _h

        return await _h(self, target, params, operator)

    async def winsrv_iscsi_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.storage.iscsi.list``."""
        from meho_backplane.connectors.winsrv.ops_storage import winsrv_iscsi_list as _h

        return await _h(self, target, params, operator)

    async def winsrv_iscsi_connect(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.storage.iscsi.connect`` (caution)."""
        from meho_backplane.connectors.winsrv.ops_storage import winsrv_iscsi_connect as _h

        return await _h(self, target, params, operator)

    async def winsrv_disk_format(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``winsrv.storage.disk.format`` (caution)."""
        from meho_backplane.connectors.winsrv.ops_storage import winsrv_disk_format as _h

        return await _h(self, target, params, operator)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`WINSRV_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.winsrv.ops.WINSRV_OPS` and routes
        each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
        Idempotent across pod restarts -- mirrors the windows_dns / rke2 shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in WINSRV_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"WinsrvConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = WINSRV_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"WinsrvConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated when_to_use "
                        f"exists for that key. Add an entry to "
                        f"WINSRV_WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.winsrv.ops."
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
            "winsrv_operations_registered",
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

        Mirrors :meth:`WindowsDnsConnector.execute`. Operator-less (no policy
        gate, no audit row, no broadcast); the operator-aware surface is
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
    fingerprint projection: ``ConvertTo-Json`` can render an absent CIM field
    as ``null``, and :class:`FingerprintResult` fields are typed ``str | None``)."""
    return value if isinstance(value, str) and value else None

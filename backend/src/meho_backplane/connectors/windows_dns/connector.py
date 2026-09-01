# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""WindowsDnsConnector -- typed SSH-transport connector for Windows AD-DNS.

Manages Windows DNS Server (AD-DNS) records over SSH → PowerShell: the
Windows host runs an OpenSSH server whose shell drives ``powershell``
(Windows PowerShell 5.1) executing
the ``DnsServer`` module cmdlets. The connector is a structural sibling of
:class:`~meho_backplane.connectors.bind9.connector.Bind9Connector` (it
mirrors bind9's identity + zone + record op surface and safety levels)
built on the PowerShell-over-SSH transport
:class:`~meho_backplane.connectors.holodeck.connector.HolodeckConnector`
established (base64 UTF-16LE ``-EncodedCommand`` + stdlib JSON parse, via
:mod:`meho_backplane.connectors._shared.pwsh`).

This module ships:

* :class:`WindowsDnsConnector`, subclass of
  :class:`~meho_backplane.connectors.adapters.ssh.SshConnector`, carrying
  the registry-v2 triple ``("windns", "2016.x", "windns-ssh")``.
* :meth:`WindowsDnsConnector.fingerprint` -- a single
  ``powershell -EncodedCommand`` script reading the hostname and inspecting the
  ``DnsServer`` module (presence + version). Returns the canonical
  :class:`FingerprintResult` with ``vendor="microsoft"`` /
  ``product="windows-dns"``; ``version`` is the DnsServer module version;
  ``extras`` carries ``hostname`` and ``dnsserver_module_present``.
  Unreachable / SSH-failed / cmdlet-failed targets surface as
  ``reachable=False`` + ``extras["error"]``.
* :meth:`WindowsDnsConnector.probe` -- TCP + SSH handshake + pwsh
  reachability + DnsServer-module presence check, surfacing five distinct
  ``ProbeResult.reason`` values: ``tcp_unreachable``, ``ssh_auth_failed``,
  ``pwsh_unavailable``, ``command_failed``, ``dnsserver_module_missing``.
* :meth:`WindowsDnsConnector.about` -- operator-facing wrapper around
  :meth:`fingerprint`, registered as the ``windns.about`` typed op.
* the bound-method op shims (zone / record) + the dispatcher
  :meth:`execute` shim (same shape as bind9 / Holodeck).

Auth uses the base
:class:`~meho_backplane.connectors.adapters.ssh.SshConnector._auth_config`
unchanged: a Vault secret with ``ssh_private_key`` prefers key auth,
otherwise password auth runs. Same password-default + key-fallback shape
as Holodeck.
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
from meho_backplane.connectors.windows_dns.ops import WINDOWS_DNS_OPS

__all__ = ["WindowsDnsConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import Target`
# in G0.3's Target model rollout. Mirrors the placeholder in the SSH adapter
# and the bind9 / Holodeck siblings.
type Target = Any


#: The PowerShell script the fingerprint / about surface runs. Reads the machine
#: hostname and inspects the DnsServer module in one round-trip; the
#: ``ConvertTo-Json`` tail keeps stdout JSON-shaped so the pwsh helper
#: parses it directly. No operator input is interpolated -- the script is
#: a constant, so there is no injection surface on the identity path.
_FINGERPRINT_SCRIPT: str = (
    "$m = Get-Module -ListAvailable DnsServer | Select-Object -First 1; "
    "ConvertTo-Json -Compress -InputObject @{ "
    "Hostname = [System.Net.Dns]::GetHostName(); "
    "DnsServerModulePresent = [bool]$m; "
    "DnsServerModuleVersion = if ($m) { $m.Version.ToString() } else { $null } }"
)

#: The PowerShell script the probe runs after the SSH handshake succeeds --
#: a minimal DnsServer-module presence check that always emits JSON.
_PROBE_SCRIPT: str = (
    "ConvertTo-Json -Compress -InputObject "
    "@{ present = [bool](Get-Module -ListAvailable DnsServer) }"
)


#: Curated ``when_to_use`` strings per group key, indexed by
#: :meth:`WindowsDnsConnector.register_operations`. Each entry covers a
#: ``group_key`` declared in :data:`WINDOWS_DNS_OPS`; the registration
#: walk fails closed with a :class:`ValueError` if a ``group_key`` lacks a
#: curated entry (the bind9 / Holodeck precedent).
_WHEN_TO_USE_BY_GROUP: dict[str, str] = {
    "identity": (
        "Use for Windows DNS host identity questions before any per-zone "
        "or per-record drill-in: 'which host is this, is the DnsServer "
        "module installed, and at what version?'. The single "
        "``windns.about`` op returns vendor / product / DnsServer module "
        "version, a presence flag, and the hostname. Call this first to "
        "confirm the host is reachable via SSH + PowerShell and the DnsServer "
        "tooling is present -- Windows AD-DNS has no record-management "
        "REST API, so this op also confirms the PowerShell-over-SSH "
        "transport is functional."
    ),
    "zone": (
        "Use for zone-level inventory reads: list every zone the server "
        "hosts (``windns.zone.list``) via ``Get-DnsServerZone``. "
        "Read-only. The right group when the agent doesn't yet know which "
        "zone to target, or needs the zone-level context before drilling "
        "into records. Pair with the 'record' group once a zone is "
        "identified."
    ),
    "record": (
        "Use for record-level reads and mutations inside a known zone: "
        "query records (``windns.record.get``), add an A / CNAME record "
        "(``windns.record.add``), or remove matching records "
        "(``windns.record.remove``). ``add`` / ``remove`` are mutating "
        "ops -- the future policy gate keys on their ``caution`` "
        "safety_level. Transport: pwsh-over-SSH driving the DnsServer "
        "cmdlets."
    ),
}


class WindowsDnsConnector(SshConnector):
    """Windows AD-DNS connector built on the :class:`SshConnector` adapter.

    Registry v2 triple: ``("windns", "2016.x", "windns-ssh")``.

    * ``product="windns"`` -- a short, separator-free product token, per
      the repo convention for multi-word connectors (``sddc_manager`` →
      ``sddc``, ``vcf_logs`` → ``vrli``). A hyphen/underscore in the
      product token breaks the ``connector_id`` round-trip the registry
      enforces (``parse_connector_id`` derives the product from the first
      hyphen-segment of ``impl_id``), which is exactly why ``vcf_logs``
      retired its historical ``vcf-logs`` token. ``windns`` reads as
      "Windows DNS".
    * ``version="2016.x"`` -- the DnsServer cmdlet surface this connector
      uses (``Get``/``Add``/``Remove-DnsServerResourceRecord``,
      ``Get-DnsServerZone``) is stable across Windows Server 2016 → 2025;
      the ``9.x``-style shape mirrors bind9's version-line marker. A
      future release-specific impl can register alongside.
    * ``impl_id="windns-ssh"`` -- leaves room for a future
      ``("windns", "2016.x", "windns-winrm")`` sibling once a non-SSH
      control surface lands, mirroring bind9's ``bind9-ssh`` /
      ``bind9-rest`` foresight.

    **Auth: password-default + key-fallback.** Inherits the base
    :class:`SshConnector` ``_auth_config`` unchanged (Vault secret with
    ``ssh_private_key`` → key auth, otherwise password auth).

    **Transport: PowerShell-over-SSH.** DnsServer cmdlets reach the host
    through ``powershell -EncodedCommand <base64-utf16le>`` (Windows
    PowerShell 5.1 -- see :attr:`POWERSHELL_EXECUTABLE`) routed by
    :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`; output
    is parsed via stdlib :mod:`json` from the cmdlet's ``ConvertTo-Json``
    pipe.
    """

    product = "windns"
    version = "2016.x"
    impl_id = "windns-ssh"

    #: The PowerShell executable the shared ``_shared.pwsh`` transport
    #: invokes on the remote host. ``powershell`` (Windows PowerShell 5.1)
    #: rather than ``pwsh`` (PS7), which is absent by default on a Windows
    #: Server DC -- verified 2026-08-03 against a live WS2022 DC (``pwsh``
    #: → "not recognized"; ``powershell`` → DnsServer module 2.0.0.0).
    #: Override on a subclass / future impl if a target ships PS7.
    POWERSHELL_EXECUTABLE = "powershell"

    #: Prepended to every script before encoding so the ``DnsServer``
    #: module's first-use progress stream ("Preparing modules for first
    #: use") does not CLIXML-serialise onto the remote streams. Windows
    #: PowerShell emits that progress to stderr; suppressing it at source
    #: keeps a :class:`~meho_backplane.connectors._shared.pwsh.PwshRunError`'s
    #: stderr fragment free of CLIXML noise (the transport's
    #: :func:`~meho_backplane.connectors._shared.pwsh.strip_clixml` net is a
    #: belt-and-braces backstop). Holodeck sets no prefix (its PS7 appliance
    #: uses per-op ``$WarningPreference`` instead).
    POWERSHELL_SCRIPT_PREFIX = "$ProgressPreference = 'SilentlyContinue'; "

    #: The structured-log event name the shared transport emits per run,
    #: kept connector-scoped so log queries stay per-connector.
    POWERSHELL_LOG_EVENT = "windows_dns_pwsh_executed"

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read hostname + DnsServer module -- the canonical fingerprint.

        Runs :data:`_FINGERPRINT_SCRIPT` (one ``powershell -EncodedCommand``
        round-trip) and parses the JSON object. ``probe_method`` is
        ``"ssh: powershell Get-Module DnsServer"``.

        Unreachable / SSH-failed / cmdlet-failed → ``reachable=False`` +
        ``extras["error"]``. The catch tuple covers the transport
        (OSError / asyncssh.Error) plus the credential-resolution failures
        ``_auth_config`` raises (ValueError / VaultClientError /
        CredentialsReadError) -- an unresolvable credential is an
        unreachable target, not an unhandled exception (#986 discipline).
        ``operator`` is threaded to the SSH adapter for the Vault
        credential read on a pool miss; ``None`` fails closed.
        """
        probed_at = datetime.now(UTC)
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
                "windows_dns_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="microsoft",
                product="windows-dns",
                reachable=False,
                probed_at=probed_at,
                probe_method="ssh: powershell Get-Module DnsServer",
                extras={"error": str(exc)},
            )

        hostname: str | None = None
        module_present: bool | None = None
        module_version: str | None = None
        if isinstance(payload, dict):
            raw_host = payload.get("Hostname")
            hostname = raw_host if isinstance(raw_host, str) else None
            raw_present = payload.get("DnsServerModulePresent")
            module_present = raw_present if isinstance(raw_present, bool) else None
            raw_version = payload.get("DnsServerModuleVersion")
            module_version = raw_version if isinstance(raw_version, str) else None

        return FingerprintResult(
            vendor="microsoft",
            product="windows-dns",
            version=module_version,
            reachable=True,
            probed_at=probed_at,
            probe_method="ssh: powershell Get-Module DnsServer",
            extras={
                "hostname": hostname,
                "dnsserver_module_present": module_present,
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + pwsh + DnsServer-module presence check.

        Failure modes (each surfaces a distinct ``reason``):

        * ``tcp_unreachable`` -- the SSH TCP socket cannot connect.
        * ``ssh_auth_failed`` -- credentials rejected, the handshake
          failed, or the Vault credential could not be resolved. ``probe``
          carries no operator, so the Vault read runs under the
          synthesised system operator and fails closed -- the operator's
          remediation is the same as for a rejected password.
        * ``pwsh_unavailable`` -- SSH succeeded but the ``pwsh`` reachability
          script failed (pwsh not installed, or the SSH shell can't run it).
        * ``command_failed`` -- the probe script could not be executed at
          the transport level after a successful handshake: the connection
          dropped (``asyncssh.Error``), or the command timed out /
          the socket failed (``OSError``, which covers
          ``asyncio.wait_for``'s ``TimeoutError``). Mirrors bind9's
          post-connect guard (#986).
        * ``dnsserver_module_missing`` -- pwsh runs but the ``DnsServer``
          module is not installed (not a DNS server, or the RSAT / role
          tooling is absent).

        The probe does not mutate state -- ``Get-Module -ListAvailable``
        is read-only.
        """
        start = time.monotonic()
        probed_at = datetime.now(UTC)

        def _result(ok: bool, reason: str | None) -> ProbeResult:
            latency_ms = (time.monotonic() - start) * 1000.0
            return ProbeResult(ok=ok, reason=reason, latency_ms=latency_ms, probed_at=probed_at)

        # Order matters: PermissionDenied (subclass of DisconnectError)
        # before DisconnectError; OSError is the TCP-level failure.
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

        # Post-connect commands are guarded (#986): a connection drop, an
        # ``asyncssh.Error``, or a timeout after a successful handshake
        # maps to ``command_failed`` rather than propagating an unhandled
        # exception out of ``probe``. ``TimeoutError`` is an ``OSError``
        # subclass, so ``_run_command``'s ``asyncio.wait_for`` expiry is
        # covered. Mirrors bind9's post-connect guard.
        try:
            payload = await pwsh_run(self, target, _PROBE_SCRIPT)
        except PwshRunError:
            return _result(False, "pwsh_unavailable")
        except (OSError, asyncssh.Error):
            return _result(False, "command_failed")

        present = payload.get("present") if isinstance(payload, dict) else None
        if present is not True:
            return _result(False, "dnsserver_module_missing")

        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Return the Windows DNS host's identity snapshot.

        Op-id: ``windns.about``. Reuses :meth:`fingerprint` so the
        operator-facing op and the canonical fingerprint share one network
        round-trip. ``_assert_reachable`` re-raises an unreachable
        fingerprint as a
        :exc:`~meho_backplane.connectors.adapters.ssh.ConnectorUnreachableError`
        so the dispatcher reports a non-ok op rather than a successful op
        carrying empty identity fields (#986).
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
            "dnsserver_module_present": result.extras.get("dnsserver_module_present"),
        }

    async def windows_dns_zone_list(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for the ``windns.zone.list`` op.

        Delegates to
        :func:`~meho_backplane.connectors.windows_dns.ops_zone.windows_dns_zone_list`.
        The bound-method shim shape mirrors bind9's ``bind9_zone_list``:
        the per-op module owns the handler logic and the registration
        metadata; the connector class exposes a thin shim so the
        descriptor's ``handler_ref`` round-trips through the dispatcher's
        ``import_handler`` walk against a ``module.ClassName.method`` path.
        """
        from meho_backplane.connectors.windows_dns.ops_zone import (
            windows_dns_zone_list as _windows_dns_zone_list,
        )

        return await _windows_dns_zone_list(self, target, params, operator)

    async def windows_dns_record_get(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for the ``windns.record.get`` op."""
        from meho_backplane.connectors.windows_dns.ops_record import (
            windows_dns_record_get as _windows_dns_record_get,
        )

        return await _windows_dns_record_get(self, target, params, operator)

    async def windows_dns_record_add(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for the ``windns.record.add`` op (caution)."""
        from meho_backplane.connectors.windows_dns.ops_record import (
            windows_dns_record_add as _windows_dns_record_add,
        )

        return await _windows_dns_record_add(self, target, params, operator)

    async def windows_dns_record_remove(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Bound-method shim for the ``windns.record.remove`` op (caution)."""
        from meho_backplane.connectors.windows_dns.ops_record import (
            windows_dns_record_remove as _windows_dns_record_remove,
        )

        return await _windows_dns_record_remove(self, target, params, operator)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`WINDOWS_DNS_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.windows_dns.ops.WINDOWS_DNS_OPS`
        and routes each row through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
        Idempotent across pod restarts -- mirrors the
        :meth:`Bind9Connector.register_operations` /
        :meth:`HolodeckConnector.register_operations` shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in WINDOWS_DNS_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"WindowsDnsConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = _WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"WindowsDnsConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated "
                        f"when_to_use exists for that key. Add an entry to "
                        f"_WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.windows_dns.connector."
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
            "windows_dns_operations_registered",
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

        Mirrors :meth:`Bind9Connector.execute` and
        :meth:`HolodeckConnector.execute`. Operator-less (no policy gate,
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

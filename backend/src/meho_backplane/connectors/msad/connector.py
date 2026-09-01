# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MsadConnector -- typed SSH-transport connector for Active Directory.

Manages an AD domain (domain / forest / DC facts, and day-2 reads + guarded
writes over users, groups, computers, OUs) through the ``ActiveDirectory``
PowerShell module (Windows PowerShell 5.1) run on a **domain controller** over
SSH, routed through the shared transport
:mod:`~meho_backplane.connectors._shared.pwsh`. It is a structured copy of the
``winsrv`` estate mold (#3261): same ``SshConnector`` base, the ``{rows, total}``
JSONFlux envelope for list reads, and the rke2 approval-parked-write mold for
destructive ops. Registry-v2 triple ``("msad", "2022.x", "msad-ssh")`` (+
wildcard); the separator-free product token round-trips through
``parse_connector_id``.

**DC-targeting decision (in-task):** every AD cmdlet runs on the SSH target
**without** an explicit ``-Server`` -- the only supported topology is "the SSH
target host IS a domain controller". A jump-host pattern (``-Server`` + a second
``-Credential`` secret) is deferred; see ``docs/codebase/connectors-msad.md``.
``POWERSHELL_EXECUTABLE`` is set to ``powershell`` explicitly (the load-bearing
winsrv trap -- the shared transport's ``pwsh`` fallback is absent on a DC).
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
from meho_backplane.connectors.msad.ops import MSAD_OPS, MSAD_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["MsadConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- mirrors the SSH adapter + winsrv / windows_dns siblings.
type Target = Any


#: The fingerprint / about script -- Get-ADDomain identity + AD module version
#: in one round-trip. No operator input is interpolated (constant script), so
#: the identity path has no injection surface.
_FINGERPRINT_SCRIPT: str = (
    "$ErrorActionPreference = 'Stop'; "
    "$d = Get-ADDomain; "
    "$m = (Get-Module -ListAvailable -Name ActiveDirectory | Select-Object -First 1).Version; "
    "ConvertTo-Json -Compress -InputObject @{ "
    "DNSRoot = $d.DNSRoot; "
    "NetBIOSName = $d.NetBIOSName; "
    "Forest = $d.Forest; "
    "DomainMode = $d.DomainMode.ToString(); "
    "PDCEmulator = $d.PDCEmulator; "
    "ADModuleVersion = if ($m) { $m.ToString() } else { $null } }"
)

#: The probe script -- AD module presence + domain readability, always emitting
#: JSON (the Get-ADDomain failure is caught so a missing module / dead domain
#: does not raise, letting the probe distinguish those two reasons).
_PROBE_SCRIPT: str = (
    "$m = [bool](Get-Module -ListAvailable -Name ActiveDirectory); "
    "$dom = $false; "
    "if ($m) { try { $null = Get-ADDomain -ErrorAction Stop; $dom = $true } "
    "catch { $dom = $false } }; "
    "ConvertTo-Json -Compress -InputObject @{ ad_module = $m; domain = $dom }"
)


class MsadConnector(SshConnector):
    """Active Directory connector on the :class:`SshConnector` adapter.

    ``version="2022.x"`` marks the cmdlet surface (stable across Windows Server
    2019 -> 2025 DCs); ``impl_id="msad-ssh"`` leaves room for a future
    ``msad-winrm`` sibling. Transport is ``powershell -EncodedCommand`` (PS 5.1)
    via ``pwsh_run``; output parses with stdlib :mod:`json`.
    """

    product = "msad"
    version = "2022.x"
    impl_id = "msad-ssh"

    #: Windows PowerShell 5.1 -- NOT ``pwsh`` (PS7), absent on a Windows DC. Set
    #: explicitly because the shared transport's fallback is ``pwsh`` (the
    #: load-bearing estate-connector trap; see the connector doc).
    POWERSHELL_EXECUTABLE = "powershell"

    #: Prepended to every script so the ActiveDirectory module's first-use
    #: progress / import warning does not CLIXML-serialise onto the streams.
    POWERSHELL_SCRIPT_PREFIX = "$ProgressPreference = 'SilentlyContinue'; "

    #: Connector-scoped structured-log event name for the shared transport.
    POWERSHELL_LOG_EVENT = "msad_pwsh_executed"

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read ``Get-ADDomain`` -- the canonical AD fingerprint.

        Runs :data:`_FINGERPRINT_SCRIPT` (one round-trip) and parses the JSON.
        Unreachable / SSH-failed / cmdlet-failed -> ``reachable=False`` +
        ``extras["error"]`` (the #986 discipline: an unresolvable credential or
        a non-DC target is an unreachable target, not an unhandled exception).
        """
        probed_at = datetime.now(UTC)
        method = "ssh: powershell Get-ADDomain"
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
                "msad_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="microsoft",
                product="active-directory",
                reachable=False,
                probed_at=probed_at,
                probe_method=method,
                extras={"error": str(exc)},
            )

        data = payload if isinstance(payload, dict) else {}
        return FingerprintResult(
            vendor="microsoft",
            product="active-directory",
            version=_as_str(data.get("DomainMode")),
            reachable=True,
            probed_at=probed_at,
            probe_method=method,
            extras={
                "dns_root": _as_str(data.get("DNSRoot")),
                "netbios_name": _as_str(data.get("NetBIOSName")),
                "forest": _as_str(data.get("Forest")),
                "pdc_emulator": _as_str(data.get("PDCEmulator")),
                "ad_module_version": _as_str(data.get("ADModuleVersion")),
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + PowerShell + AD-module + domain-readability check.

        Failure modes (each a distinct ``reason``): ``tcp_unreachable`` (socket),
        ``ssh_auth_failed`` (creds / handshake / Vault), ``powershell_unavailable``
        (SSH ok but the reachability script failed), ``command_failed`` (a
        post-connect transport failure -- the #986 guard), ``ad_module_unavailable``
        (PowerShell runs but the ActiveDirectory module is absent -- not a DC), and
        ``domain_unreachable`` (module present but ``Get-ADDomain`` fails).

        The probe does not mutate state -- both cmdlets are read-only.
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
        if data.get("ad_module") is not True:
            return _result(False, "ad_module_unavailable")
        if data.get("domain") is not True:
            return _result(False, "domain_unreachable")
        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Return the AD domain identity snapshot (op-id ``msad.about``).

        Reuses :meth:`fingerprint` so the op and the canonical fingerprint share
        one round-trip. ``_assert_reachable`` re-raises an unreachable
        fingerprint as a ``ConnectorUnreachableError`` so the dispatcher reports
        a non-ok op rather than empty identity fields.
        """
        del params  # declared empty in schema; intentionally ignored
        result = await self.fingerprint(target, operator)
        self._assert_reachable(result)
        return {
            "vendor": result.vendor,
            "product": result.product,
            "version": result.version,
            "dns_root": result.extras.get("dns_root"),
            "netbios_name": result.extras.get("netbios_name"),
            "forest": result.extras.get("forest"),
            "pdc_emulator": result.extras.get("pdc_emulator"),
            "ad_module_version": result.extras.get("ad_module_version"),
        }

    # -- domain group shims -------------------------------------------------

    async def msad_domain_info(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.domain.info``."""
        from meho_backplane.connectors.msad.ops_domain import msad_domain_info as _h

        return await _h(self, target, params, operator)

    async def msad_domain_forest(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.domain.forest``."""
        from meho_backplane.connectors.msad.ops_domain import msad_domain_forest as _h

        return await _h(self, target, params, operator)

    async def msad_domain_controllers(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.domain.controllers``."""
        from meho_backplane.connectors.msad.ops_domain import msad_domain_controllers as _h

        return await _h(self, target, params, operator)

    async def msad_domain_replication(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.domain.replication``."""
        from meho_backplane.connectors.msad.ops_domain import msad_domain_replication as _h

        return await _h(self, target, params, operator)

    # -- users group shims --------------------------------------------------

    async def msad_user_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.user.list``."""
        from meho_backplane.connectors.msad.ops_users import msad_user_list as _h

        return await _h(self, target, params, operator)

    async def msad_user_get(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.user.get``."""
        from meho_backplane.connectors.msad.ops_users import msad_user_get as _h

        return await _h(self, target, params, operator)

    async def msad_user_search(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.user.search``."""
        from meho_backplane.connectors.msad.ops_users import msad_user_search as _h

        return await _h(self, target, params, operator)

    async def msad_user_create(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.user.create`` (caution)."""
        from meho_backplane.connectors.msad.ops_users import msad_user_create as _h

        return await _h(self, target, params, operator)

    async def msad_user_set(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.user.set`` (caution)."""
        from meho_backplane.connectors.msad.ops_users import msad_user_set as _h

        return await _h(self, target, params, operator)

    async def msad_user_enable(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.user.enable`` (caution)."""
        from meho_backplane.connectors.msad.ops_users import msad_user_enable as _h

        return await _h(self, target, params, operator)

    async def msad_user_disable(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.user.disable`` (caution)."""
        from meho_backplane.connectors.msad.ops_users import msad_user_disable as _h

        return await _h(self, target, params, operator)

    async def msad_user_delete(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.user.delete`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.msad.ops_users import msad_user_delete as _h

        return await _h(self, target, params, operator)

    # -- groups group shims -------------------------------------------------

    async def msad_group_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.group.list``."""
        from meho_backplane.connectors.msad.ops_groups import msad_group_list as _h

        return await _h(self, target, params, operator)

    async def msad_group_get(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.group.get``."""
        from meho_backplane.connectors.msad.ops_groups import msad_group_get as _h

        return await _h(self, target, params, operator)

    async def msad_group_members(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.group.members``."""
        from meho_backplane.connectors.msad.ops_groups import msad_group_members as _h

        return await _h(self, target, params, operator)

    async def msad_group_add_member(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.group.add-member`` (caution)."""
        from meho_backplane.connectors.msad.ops_groups import msad_group_add_member as _h

        return await _h(self, target, params, operator)

    async def msad_group_remove_member(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.group.remove-member`` (caution)."""
        from meho_backplane.connectors.msad.ops_groups import msad_group_remove_member as _h

        return await _h(self, target, params, operator)

    async def msad_group_delete(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.group.delete`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.msad.ops_groups import msad_group_delete as _h

        return await _h(self, target, params, operator)

    # -- computers group shims ----------------------------------------------

    async def msad_computer_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.computer.list``."""
        from meho_backplane.connectors.msad.ops_computers import msad_computer_list as _h

        return await _h(self, target, params, operator)

    async def msad_computer_get(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.computer.get``."""
        from meho_backplane.connectors.msad.ops_computers import msad_computer_get as _h

        return await _h(self, target, params, operator)

    async def msad_computer_join_prestage(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.computer.join-prestage`` (caution)."""
        from meho_backplane.connectors.msad.ops_computers import msad_computer_join_prestage as _h

        return await _h(self, target, params, operator)

    async def msad_computer_unjoin(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.computer.unjoin`` (caution)."""
        from meho_backplane.connectors.msad.ops_computers import msad_computer_unjoin as _h

        return await _h(self, target, params, operator)

    async def msad_computer_delete(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.computer.delete`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.msad.ops_computers import msad_computer_delete as _h

        return await _h(self, target, params, operator)

    # -- ou group shims -----------------------------------------------------

    async def msad_ou_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.ou.list``."""
        from meho_backplane.connectors.msad.ops_ou import msad_ou_list as _h

        return await _h(self, target, params, operator)

    async def msad_ou_create(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.ou.create`` (caution)."""
        from meho_backplane.connectors.msad.ops_ou import msad_ou_create as _h

        return await _h(self, target, params, operator)

    async def msad_ou_move(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``msad.ou.move`` (caution)."""
        from meho_backplane.connectors.msad.ops_ou import msad_ou_move as _h

        return await _h(self, target, params, operator)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`MSAD_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Idempotent across pod restarts;
        mirrors the winsrv / windows_dns / rke2 shape. Fails closed if an op
        declares a ``group_key`` with no curated ``when_to_use``.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in MSAD_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"MsadConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = MSAD_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"MsadConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated when_to_use "
                        f"exists for that key. Add an entry to "
                        f"MSAD_WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.msad.ops."
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
            "msad_operations_registered",
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
    """Return *value* when it is a non-empty ``str``, else ``None``.

    Guards the fingerprint projection: ``ConvertTo-Json`` can render an absent
    field as ``null``, and :class:`FingerprintResult` fields are ``str | None``.
    """
    return value if isinstance(value, str) and value else None

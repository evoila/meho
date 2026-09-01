# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""WsfcConnector -- typed SSH-transport connector for Windows Server Failover Clustering.

Manages a Windows Server Failover Cluster (cluster / node / group / resource /
quorum state, validation, guarded role-move / failover) over SSH → PowerShell:
the target cluster node runs an OpenSSH server whose shell drives
``powershell`` (Windows PowerShell 5.1) executing the built-in
``FailoverClusters`` module cmdlets. The connector is a structured copy of
:class:`~meho_backplane.connectors.winsrv.connector.WinsrvConnector` (the
estate mold — same ``SshConnector`` base + shared PowerShell-over-SSH transport
:mod:`~meho_backplane.connectors._shared.pwsh`) and adopts the
:mod:`~meho_backplane.connectors.rke2.ops_write` approval-parked-write mold for
its destructive ops.

The **target is any single cluster node**: the ``FailoverClusters`` cmdlets
talk to the local Cluster Service (the cluster-wide database), so they fan out
cluster-wide from whichever node runs them — one node is a sufficient control
point for the whole cluster.

This module ships :class:`WsfcConnector` (registry-v2 triple ``("wsfc",
"2022.x", "wsfc-ssh")``): :meth:`fingerprint` (one round-trip reading the
hostname + OS + ``FailoverClusters`` module presence + cluster membership),
:meth:`probe` (six distinct ``ProbeResult.reason`` values, two of them
cluster-membership specific), :meth:`about` (the ``wsfc.about`` op), the
bound-method op shims for the five groups, and the operator-less
:meth:`execute` dispatcher shim (same shape as winsrv / windows_dns / rke2).

Like winsrv, this connector sets ``POWERSHELL_EXECUTABLE = "powershell"``
explicitly: the shared transport's fallback is ``pwsh`` (PS7), which a Windows
Server host does NOT ship (see ``docs/codebase/connectors-winsrv.md`` --
"building the next estate connector").
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
from meho_backplane.connectors.wsfc.ops import WSFC_OPS, WSFC_WHEN_TO_USE_BY_GROUP

__all__ = ["WsfcConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- mirrors the placeholder in the SSH adapter and the
# winsrv / windows_dns / rke2 siblings.
type Target = Any


#: The fingerprint / about script -- hostname + OS version/build + PowerShell
#: version + FailoverClusters module presence + cluster membership in one
#: round-trip. No operator input is interpolated (constant script), so the
#: identity path has no injection surface. ``Get-Cluster`` is wrapped in
#: try/catch so a non-clustered node reports ``ClusterName = $null`` instead of
#: failing the whole fingerprint.
_FINGERPRINT_SCRIPT: str = (
    "$os = Get-CimInstance -ClassName Win32_OperatingSystem; "
    "$mod = [bool](Get-Module -ListAvailable -Name FailoverClusters); "
    "$cn = $null; $cfl = $null; "
    'if ($mod) { try { $cl = Get-Cluster -ErrorAction Stop; $cn = "$($cl.Name)"; '
    "$cfl = [int]$cl.ClusterFunctionalLevel } catch { $cn = $null } }; "
    "ConvertTo-Json -Compress -InputObject @{ "
    "Hostname = [System.Net.Dns]::GetHostName(); "
    "OsVersion = $os.Version; "
    "BuildNumber = $os.BuildNumber; "
    "PowerShellVersion = $PSVersionTable.PSVersion.ToString(); "
    "FailoverClustersModule = $mod; "
    "ClusterName = $cn; "
    "ClusterFunctionalLevel = $cfl }"
)

#: The probe script -- FailoverClusters module presence + cluster membership.
#: Always emits JSON. ``Get-Cluster`` is only attempted when the module is
#: present (it is the module's cmdlet), and a non-membership failure is caught.
_PROBE_SCRIPT: str = (
    "$mod = [bool](Get-Module -ListAvailable -Name FailoverClusters); "
    "$cn = $null; "
    'if ($mod) { try { $cn = "$((Get-Cluster -ErrorAction Stop).Name)" } catch { $cn = $null } }; '
    "ConvertTo-Json -Compress -InputObject @{ module = $mod; cluster = $cn }"
)


class WsfcConnector(SshConnector):
    """Windows Server Failover Clustering connector on the :class:`SshConnector` adapter.

    Registry v2 triple: ``("wsfc", "2022.x", "wsfc-ssh")``.

    * ``product="wsfc"`` -- a short, separator-free product token (the repo
      convention: ``parse_connector_id`` derives the product from the first
      hyphen-segment of ``impl_id``, so ``connector_id="wsfc-ssh-2022.x"``
      round-trips; a hyphen/underscore in the token breaks the registry's
      round-trip guard at boot).
    * ``version="2022.x"`` -- the ``FailoverClusters`` cmdlet surface targets
      Windows Server 2022 (stable across 2019 → 2025).
    * ``impl_id="wsfc-ssh"`` -- leaves room for a future ``wsfc-winrm`` sibling.

    **Auth: password-default + key-fallback.** Inherits the base
    :class:`SshConnector` ``_auth_config`` unchanged.

    **Transport: PowerShell-over-SSH.** Cmdlets reach the node through
    ``powershell -EncodedCommand <base64-utf16le>`` (Windows PowerShell 5.1 --
    see :attr:`POWERSHELL_EXECUTABLE`) routed by
    :func:`~meho_backplane.connectors._shared.pwsh.pwsh_run`; output is parsed
    via stdlib :mod:`json` from the cmdlet's ``ConvertTo-Json`` pipe.
    """

    product = "wsfc"
    version = "2022.x"
    impl_id = "wsfc-ssh"

    #: The PowerShell executable the shared transport invokes. ``powershell``
    #: (Windows PowerShell 5.1) -- NOT ``pwsh`` (PS7). Set explicitly because
    #: the shared transport's fallback is ``pwsh``; every Windows-estate
    #: connector MUST set this (see the connectors-winsrv doc).
    POWERSHELL_EXECUTABLE = "powershell"

    #: Prepended to every script so the FailoverClusters module's first-use
    #: progress stream does not CLIXML-serialise onto the remote streams.
    POWERSHELL_SCRIPT_PREFIX = "$ProgressPreference = 'SilentlyContinue'; "

    #: The structured-log event name the shared transport emits per run, kept
    #: connector-scoped so log queries stay per-connector.
    POWERSHELL_LOG_EVENT = "wsfc_pwsh_executed"

    async def fingerprint(
        self,
        target: Target,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Read the node OS + FailoverClusters module + cluster membership.

        Runs :data:`_FINGERPRINT_SCRIPT` (one round-trip) and parses the JSON
        object. Unreachable / SSH-failed / cmdlet-failed → ``reachable=False``
        + ``extras["error"]`` (the #986 discipline). ``operator`` is threaded to
        the SSH adapter for the Vault read on a pool miss; ``None`` fails
        closed. Whether the node is *clustered* is metadata (``cluster_name``),
        not a reachability signal — an unclustered but reachable node is
        ``reachable=True`` here; :meth:`probe` is where non-membership becomes a
        distinct reason.
        """
        probed_at = datetime.now(UTC)
        method = "ssh: powershell Get-Cluster / Win32_OperatingSystem"
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
                "wsfc_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=str(exc),
            )
            return FingerprintResult(
                vendor="microsoft",
                product="windows-failover-cluster",
                reachable=False,
                probed_at=probed_at,
                probe_method=method,
                extras={"error": str(exc)},
            )

        data = payload if isinstance(payload, dict) else {}
        return FingerprintResult(
            vendor="microsoft",
            product="windows-failover-cluster",
            version=_as_str(data.get("OsVersion")),
            build=_as_str(data.get("BuildNumber")),
            reachable=True,
            probed_at=probed_at,
            probe_method=method,
            extras={
                "hostname": _as_str(data.get("Hostname")),
                "cluster_name": _as_str(data.get("ClusterName")),
                "cluster_functional_level": _as_int(data.get("ClusterFunctionalLevel")),
                "failover_clusters_module": _as_bool(data.get("FailoverClustersModule")),
                "powershell_version": _as_str(data.get("PowerShellVersion")),
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability + auth + PowerShell + cluster-membership check.

        Failure modes (each surfaces a distinct ``reason``):

        * ``tcp_unreachable`` -- the SSH TCP socket cannot connect.
        * ``ssh_auth_failed`` -- credentials rejected, the handshake failed,
          or the Vault credential could not be resolved.
        * ``powershell_unavailable`` -- SSH succeeded but the reachability
          script failed (``powershell`` not on the shell, or a pwsh error).
        * ``command_failed`` -- a post-connect transport failure (connection
          drop / timeout / socket error) after a successful handshake.
        * ``failover_module_absent`` -- PowerShell runs but the
          ``FailoverClusters`` module is not installed (not a cluster node).
        * ``not_cluster_member`` -- the module is present but the node is not a
          member of a running cluster (``Get-Cluster`` failed).

        The probe does not mutate state -- ``Get-Cluster`` is read-only.
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
            return _result(False, "failover_module_absent")
        if not _as_str(data.get("cluster")):
            return _result(False, "not_cluster_member")
        return _result(True, None)

    async def about(
        self,
        target: Target,
        params: dict[str, Any],
        operator: Operator | None = None,
    ) -> dict[str, Any]:
        """Return the cluster node's identity + membership snapshot.

        Op-id: ``wsfc.about``. Reuses :meth:`fingerprint` so the operator-facing
        op and the canonical fingerprint share one round-trip.
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
            "cluster_name": result.extras.get("cluster_name"),
            "cluster_functional_level": result.extras.get("cluster_functional_level"),
            "failover_clusters_module": result.extras.get("failover_clusters_module"),
            "powershell_version": result.extras.get("powershell_version"),
        }

    # -- cluster group shims -----------------------------------------------

    async def wsfc_cluster_get(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.cluster.get``."""
        from meho_backplane.connectors.wsfc.ops_cluster import wsfc_cluster_get as _h

        return await _h(self, target, params, operator)

    async def wsfc_cluster_quorum(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.cluster.quorum``."""
        from meho_backplane.connectors.wsfc.ops_cluster import wsfc_cluster_quorum as _h

        return await _h(self, target, params, operator)

    async def wsfc_cluster_validation_report(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.cluster.validation-report``."""
        from meho_backplane.connectors.wsfc.ops_cluster import wsfc_cluster_validation_report as _h

        return await _h(self, target, params, operator)

    async def wsfc_cluster_test(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.cluster.test`` (caution, long-running)."""
        from meho_backplane.connectors.wsfc.ops_cluster import wsfc_cluster_test as _h

        return await _h(self, target, params, operator)

    # -- nodes group shims -------------------------------------------------

    async def wsfc_node_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.nodes.list``."""
        from meho_backplane.connectors.wsfc.ops_nodes import wsfc_node_list as _h

        return await _h(self, target, params, operator)

    async def wsfc_node_state(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.nodes.state``."""
        from meho_backplane.connectors.wsfc.ops_nodes import wsfc_node_state as _h

        return await _h(self, target, params, operator)

    async def wsfc_node_pause(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.nodes.pause`` (caution)."""
        from meho_backplane.connectors.wsfc.ops_nodes import wsfc_node_pause as _h

        return await _h(self, target, params, operator)

    async def wsfc_node_resume(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.nodes.resume`` (caution)."""
        from meho_backplane.connectors.wsfc.ops_nodes import wsfc_node_resume as _h

        return await _h(self, target, params, operator)

    async def wsfc_node_evict(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.nodes.evict`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.wsfc.ops_nodes import wsfc_node_evict as _h

        return await _h(self, target, params, operator)

    # -- groups group shims ------------------------------------------------

    async def wsfc_group_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.groups.list``."""
        from meho_backplane.connectors.wsfc.ops_groups import wsfc_group_list as _h

        return await _h(self, target, params, operator)

    async def wsfc_group_state(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.groups.state``."""
        from meho_backplane.connectors.wsfc.ops_groups import wsfc_group_state as _h

        return await _h(self, target, params, operator)

    async def wsfc_group_move(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.groups.move`` (caution)."""
        from meho_backplane.connectors.wsfc.ops_groups import wsfc_group_move as _h

        return await _h(self, target, params, operator)

    async def wsfc_group_offline(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.groups.offline`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.wsfc.ops_groups import wsfc_group_offline as _h

        return await _h(self, target, params, operator)

    async def wsfc_group_online(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.groups.online`` (dangerous, approval-gated)."""
        from meho_backplane.connectors.wsfc.ops_groups import wsfc_group_online as _h

        return await _h(self, target, params, operator)

    # -- resources group shims ---------------------------------------------

    async def wsfc_resource_list(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.resources.list``."""
        from meho_backplane.connectors.wsfc.ops_resources import wsfc_resource_list as _h

        return await _h(self, target, params, operator)

    async def wsfc_resource_dependency_report(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.resources.dependency-report``."""
        from meho_backplane.connectors.wsfc.ops_resources import (
            wsfc_resource_dependency_report as _h,
        )

        return await _h(self, target, params, operator)

    # -- witness group shims -----------------------------------------------

    async def wsfc_witness_get(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.witness.get``."""
        from meho_backplane.connectors.wsfc.ops_witness import wsfc_witness_get as _h

        return await _h(self, target, params, operator)

    async def wsfc_witness_set(
        self, target: Target, params: dict[str, Any], operator: Operator | None = None
    ) -> dict[str, Any]:
        """Bound-method shim for ``wsfc.witness.set`` (caution)."""
        from meho_backplane.connectors.wsfc.ops_witness import wsfc_witness_set as _h

        return await _h(self, target, params, operator)

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`WSFC_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan after the registry has
        eager-imported every connector module. Walks
        :data:`~meho_backplane.connectors.wsfc.ops.WSFC_OPS` and routes each row
        through
        :func:`~meho_backplane.operations.typed_register.register_typed_operation`.
        Idempotent across pod restarts -- mirrors the winsrv / rke2 shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        bindings: list[tuple[Any, Any]] = []
        for op in WSFC_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"WsfcConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            bindings.append((op, handler))

        for op, handler in bindings:
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = WSFC_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"WsfcConnector op {op.op_id!r} declares "
                        f"group_key={op.group_key!r} but no curated when_to_use "
                        f"exists for that key. Add an entry to "
                        f"WSFC_WHEN_TO_USE_BY_GROUP in "
                        f"meho_backplane.connectors.wsfc.ops."
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
            "wsfc_operations_registered",
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
    field as ``null``, and :class:`FingerprintResult` fields are typed
    ``str | None``.
    """
    return value if isinstance(value, str) and value else None


def _as_int(value: Any) -> int | None:
    """Return *value* as an ``int`` (excluding ``bool``), else ``None``."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_bool(value: Any) -> bool | None:
    """Return *value* when it is a ``bool``, else ``None``."""
    return value if isinstance(value, bool) else None

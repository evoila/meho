# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MssqlConnector -- typed TDS-direct connector for Microsoft SQL Server (#3264).

MEHO's SQL Server connector. Like the postgres (#2236) and mongodb (#2237)
siblings it subclasses the generic
:class:`~meho_backplane.connectors.base.Connector` ABC (not the SSH / HTTP
adapters) and drives a **direct TDS connection** on port 1433 via the
pure-Python ``python-tds`` driver (import :mod:`pytds`) -- the transport
decision recorded in ``docs/codebase/connectors-mssql.md``. Registry v2 triple
``("mssql", "2022.x", "mssql-tds")``.

Design
------

* **Curated op table, no freeform query.** 14 ops across four groups
  (``instance`` / ``databases`` / ``ha`` / ``backup``), each individually
  safety-tiered per the Initiative #3259 satellite table. There is deliberately
  **no** raw-T-SQL escape hatch (the narrow-waist doctrine): the agent calls
  curated ops, never composes arbitrary SQL.

* **Two-credential SQL auth.** The target's Vault secret carries
  ``sql_username`` / ``sql_password`` (the first two-credential connector on the
  estate seam; the ``sql_`` prefix leaves room for a later
  dbatools-over-PowerShell increment to store SSH creds in the same secret).
  The pair flows only into the ``pytds`` connect params -- never a log line or
  an :class:`OperationResult`. See
  :func:`~meho_backplane.connectors.mssql.session.resolve_sql_credentials`.

* **Injection safety.** Operator-supplied values bind through ``pytds`` pyformat
  placeholders; operator-supplied database identifiers ride
  :func:`~meho_backplane.connectors.mssql.session.quote_identifier` (validated +
  bracket-escaped). See the session module docstring.

Fingerprint reads SERVERPROPERTY (product version / edition / clustering);
probe is a ``@@VERSION`` reachability check with distinct failure reasons.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import pytds
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.fingerprint import (
    FingerprintFailureReason,
    redact_fingerprint_error,
)
from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.mssql import queries
from meho_backplane.connectors.mssql.ops import MSSQL_OPS, MSSQL_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["MssqlConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- mirrors the postgres / mongodb siblings until G0.3's
# Target model rollout lands.
type Target = Any


def _classify_fingerprint_error(exc: Exception) -> FingerprintFailureReason:
    """Map a fingerprint failure exception to a classified reason (#3297).

    Mirrors the :meth:`MssqlConnector.probe` taxonomy so a target degrades
    the same way whether the failure surfaces on ``fingerprint`` or ``probe``:
    a rejected / unresolvable credential is ``auth_failed`` (never the raw
    ``LoginError: Login failed for user '<u>'`` message), a TCP-level failure
    is ``tcp_unreachable``, and any other TDS-handshake failure is
    ``connect_failed``. Only ever called for an exception the fingerprint arm
    catches, so :exc:`pytds.Error` is the residual.
    """
    if isinstance(exc, CredentialsReadError | VaultClientError | ValueError | pytds.LoginError):
        return "auth_failed"
    if isinstance(exc, OSError):
        return "tcp_unreachable"
    return "connect_failed"


class MssqlConnector(Connector):
    """TDS-direct SQL Server connector via ``python-tds``.

    Registry v2 triple ``("mssql", "2022.x", "mssql-tds")``. ``priority`` is
    ``1`` so a future ``GenericRestConnector`` auto-shim registering the same
    product loses the resolver tie-break. ``supported_version_range`` covers SQL
    Server 2016 (13) through 2022 (16): the catalog / DMV surface the read ops
    query (``sys.databases`` / ``sys.configurations`` / ``sys.dm_hadr_*`` /
    ``sys.dm_os_cluster_nodes`` / ``msdb.dbo.backupset``) is stable across those
    releases, so the connector also serves an older migration *source* while the
    ``2022.x`` label names its primary target.
    """

    product = "mssql"
    version = "2022.x"
    impl_id = "mssql-tds"
    supported_version_range = ">=13,<17"
    priority = 1

    # ------------------------------------------------------------------
    # instance group
    # ------------------------------------------------------------------

    async def instance_about(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.instance.about`` -- SERVERPROPERTY identity."""
        del params  # declared empty in schema
        return await queries.fetch_server_identity(target, operator)

    async def instance_version(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.instance.version`` -- full ``@@VERSION`` + parsed product version."""
        del params
        return await queries.fetch_version(target, operator)

    async def instance_config(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.instance.config`` -- ``sys.configurations`` (list-shaped)."""
        del params
        return await queries.fetch_configurations(target, operator)

    async def instance_logins(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.instance.logins`` -- server logins (no password material)."""
        del params
        return await queries.fetch_logins(target, operator)

    # ------------------------------------------------------------------
    # databases group
    # ------------------------------------------------------------------

    async def databases_list(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.databases.list`` -- databases with state, recovery model, size."""
        del params
        return await queries.fetch_databases(target, operator)

    async def databases_files(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.databases.files`` -- data/log files, optionally per-database."""
        return await queries.fetch_database_files(target, operator, params.get("database"))

    async def databases_create(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.databases.create`` (dangerous, approval-gated) -- ``CREATE DATABASE``."""
        return await queries.create_database(target, operator, params["database"])

    async def databases_drop(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.databases.drop`` (dangerous, approval-gated) -- ``DROP DATABASE``."""
        return await queries.drop_database(target, operator, params["database"])

    # ------------------------------------------------------------------
    # ha group -- migration-validation reads
    # ------------------------------------------------------------------

    async def ha_availability_groups(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.ha.availability-groups`` -- AG topology + per-replica state."""
        del params
        return await queries.fetch_availability_groups(target, operator)

    async def ha_fci(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.ha.fci`` -- Failover Cluster Instance nodes + clustering flags."""
        del params
        return await queries.fetch_fci_nodes(target, operator)

    async def ha_sync_health(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.ha.sync-health`` -- per-database AG replication sync health."""
        del params
        return await queries.fetch_sync_health(target, operator)

    # ------------------------------------------------------------------
    # backup group
    # ------------------------------------------------------------------

    async def backup_history(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.backup.history`` -- recent backup history (list-shaped)."""
        return await queries.fetch_backup_history(
            target, operator, params.get("database"), params.get("limit")
        )

    async def backup_database(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.backup.database`` (caution) -- ``BACKUP DATABASE ... TO DISK``."""
        return await queries.backup_database(target, operator, params["database"], params["path"])

    async def backup_restore(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``mssql.backup.restore`` (dangerous, approval-gated) -- ``RESTORE DATABASE``."""
        return await queries.restore_database(
            target,
            operator,
            params["database"],
            params["path"],
            bool(params.get("replace", False)),
        )

    # ------------------------------------------------------------------
    # Fingerprint / probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self, target: Target, operator: Operator | None = None
    ) -> FingerprintResult:
        """Canonical fingerprint: SERVERPROPERTY product version / edition / clustering.

        A credentialled target reads its secret under *operator* (``None`` fails
        closed inside :func:`resolve_sql_credentials`). Any connection /
        credential failure maps to ``reachable=False`` with the error under
        ``extras`` rather than raising (#986 discipline).
        """
        probed_at = datetime.now(UTC)
        method = "pytds: SERVERPROPERTY"
        try:
            identity = await queries.fetch_server_identity(target, operator)
        except (OSError, pytds.Error, CredentialsReadError, VaultClientError, ValueError) as exc:
            error = redact_fingerprint_error(exc, _classify_fingerprint_error(exc))
            _log.warning(
                "mssql_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=error,
            )
            return FingerprintResult(
                vendor="microsoft",
                product="mssql",
                reachable=False,
                probed_at=probed_at,
                probe_method=method,
                extras={"error": error},
            )

        return FingerprintResult(
            vendor="microsoft",
            product="mssql",
            version=_as_str(identity.get("product_version")),
            build=_as_str(identity.get("product_level")),
            reachable=True,
            probed_at=probed_at,
            probe_method=method,
            extras={
                "edition": _as_str(identity.get("edition")),
                "machine_name": _as_str(identity.get("machine_name")),
                "server_name": _as_str(identity.get("server_name")),
                "instance_name": _as_str(identity.get("instance_name")),
                "is_clustered": identity.get("is_clustered"),
                "is_hadr_enabled": identity.get("is_hadr_enabled"),
            },
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability check via a ``SELECT @@VERSION`` handshake.

        Distinct ``reason`` values on failure:

        * ``auth_failed`` -- the server rejected the credential
          (:exc:`pytds.LoginError`) or the credential could not be resolved
          (:class:`CredentialsReadError` / :class:`VaultClientError` /
          :exc:`ValueError` -- a credentialled target on an operator-less
          probe).
        * ``tcp_unreachable`` -- the TCP connect failed or timed out (host down,
          firewall, wrong port; :exc:`OSError`, which covers
          :exc:`TimeoutError`).
        * ``connect_failed`` -- the socket opened but the TDS handshake failed
          for another reason (:exc:`pytds.Error` -- e.g. the server requires
          encryption the connector did not offer).

        ``probe`` carries no operator, so a credentialled target's secret read
        runs without an authenticated operator and fails closed to
        ``auth_failed``; reachability of a credentialled target is confirmed on
        the operator-carrying fingerprint / op path (the postgres precedent).
        """
        start = time.monotonic()
        probed_at = datetime.now(UTC)

        def _result(ok: bool, reason: str | None) -> ProbeResult:
            return ProbeResult(
                ok=ok,
                reason=reason,
                latency_ms=(time.monotonic() - start) * 1000.0,
                probed_at=probed_at,
            )

        try:
            await queries.fetch_version(target, None)
        except (CredentialsReadError, VaultClientError, ValueError, pytds.LoginError):
            return _result(False, "auth_failed")
        except OSError:
            return _result(False, "tcp_unreachable")
        except pytds.Error:
            return _result(False, "connect_failed")
        return _result(True, None)

    # ------------------------------------------------------------------
    # Registration + dispatch shim
    # ------------------------------------------------------------------

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`MSSQL_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan (via the registrar queued in
        :mod:`meho_backplane.connectors.mssql.__init__`) after the registry has
        eager-imported every connector module. Idempotent across pod restarts --
        the postgres / winsrv shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        for op in MSSQL_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"MssqlConnector op {op.op_id!r} declares handler_attr="
                    f"{op.handler_attr!r} but the class has no such attribute"
                )
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = MSSQL_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"MssqlConnector op {op.op_id!r} declares group_key="
                        f"{op.group_key!r} but no curated when_to_use exists for that key. "
                        "Add an entry to MSSQL_WHEN_TO_USE_BY_GROUP in mssql/ops.py."
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
            "mssql_operations_registered",
            count=len(MSSQL_OPS),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def execute(self, target: Target, op_id: str, params: dict[str, Any]) -> OperationResult:
        """Legacy shim -- delegates to the G0.6 dispatcher.

        Mirrors :meth:`PostgresConnector.execute`. Post-G0.6 callers construct a
        real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly. The synthetic
        operator carries ``raw_jwt=""``, so a credentialled target reached
        through this shim fails closed in the credential loader -- the
        operator-context path is the real dispatch surface. The natural key
        encodes as ``"mssql-tds-2022.x"`` per ``parse_connector_id``.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:mssql-tds-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        connector_id = f"{self.impl_id}-{self.version}"
        return await dispatch(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=params,
        )


def _as_str(value: Any) -> str | None:
    """Return *value* when it is a non-empty ``str``, else ``None``.

    Guards the fingerprint projection: a SERVERPROPERTY read can render an
    absent field as ``None``, and :class:`FingerprintResult`'s string fields
    are typed ``str | None``.
    """
    return value if isinstance(value, str) and value else None

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""PostgresConnector -- read-only wire-protocol connector for PostgreSQL (#2236).

MEHO's **first wire-protocol (non-HTTP) connector**: it subclasses the generic
:class:`~meho_backplane.connectors.base.Connector` ABC (not
:class:`~meho_backplane.connectors.adapters.http.HttpConnector`) and drives an
``asyncpg`` client over the PostgreSQL frontend/backend protocol v3 (default
port 5432). Registry v2 triple ``("postgres", "16", "postgres-wire")``.

Design
------

* **Read-only, doubly enforced.** Every connection is opened with
  ``default_transaction_read_only=on`` (server-enforced), and the free-form
  ``postgres.query`` op runs its statement through a first-keyword allowlist
  (:func:`~meho_backplane.connectors.postgres.session.assert_read_only_sql`)
  before it reaches the wire. No write op is registered.

* **Optional auth.** A target with ``secret_ref=None`` is a trust-auth
  instance and connects with no password; a target with a ``secret_ref``
  resolves ``{username, password}`` under the operator's identity. The
  password flows only into asyncpg connect params -- never a log line or an
  :class:`OperationResult`. See
  :func:`~meho_backplane.connectors.postgres.session.connect_read_only`.

* **The DB-connector shape.** The package split (``session.py`` = wire +
  read-only gate, ``queries.py`` = SQL + row shaping, ``ops.py`` = op
  metadata, ``connector.py`` = the thin op surface) establishes the pattern
  the mongodb sibling (#2237) reuses for a different wire driver.

Fingerprint reads ``server_version`` / ``pg_is_in_recovery()`` /
``server_encoding`` / ``data_checksums`` plus per-database sizes; probe is a
``SELECT 1`` reachability check with structured failure reasons.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import asyncpg
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vault_creds import CredentialsReadError
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.postgres import queries
from meho_backplane.connectors.postgres.ops import PG_OPS, PG_WHEN_TO_USE_BY_GROUP
from meho_backplane.connectors.postgres.session import (
    assert_read_only_sql,
    connect_read_only,
)
from meho_backplane.connectors.schemas import (
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["PostgresConnector"]

_log = structlog.get_logger(__name__)

# Forward declaration -- replaced with `from meho_backplane.targets import Target`
# once G0.3's Target model rollout lands, mirroring the sibling connectors.
type Target = Any

#: Default row cap for the free-form ``postgres.query`` op.
_DEFAULT_MAX_ROWS = 1000


class PostgresConnector(Connector):
    """Read-only PostgreSQL connector over the wire protocol via ``asyncpg``.

    Registry v2 triple ``("postgres", "16", "postgres-wire")``. ``priority`` is
    ``1`` so a future ``GenericRestConnector`` auto-shim registering the same
    product loses the resolver tie-break. ``supported_version_range`` covers
    the maintained PostgreSQL releases; the catalog + statistics views this
    connector reads are stable across them.
    """

    product = "postgres"
    version = "16"
    impl_id = "postgres-wire"
    supported_version_range = ">=12,<18"
    priority = 1

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _connection(
        self,
        target: Target,
        operator: Operator | None,
        *,
        database: str | None = None,
    ) -> AsyncIterator[asyncpg.Connection]:
        """Yield a read-only asyncpg connection, always closed on exit.

        Thin wrapper over
        :func:`~meho_backplane.connectors.postgres.session.connect_read_only`
        so every op shares the connect (server-enforced read-only + optional
        auth) and teardown path.
        """
        conn = await connect_read_only(target, operator, database=database)
        try:
            yield conn
        finally:
            await conn.close()

    # ------------------------------------------------------------------
    # Curated read ops -- each threads ``operator`` for the (optional) auth read
    # ------------------------------------------------------------------

    async def list_databases(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``postgres.databases`` -- non-template databases with sizes."""
        del params  # declared empty in schema
        async with self._connection(target, operator) as conn:
            return await queries.fetch_databases(conn)

    async def list_schemas(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``postgres.schemas`` -- user schemas in the connected database."""
        async with self._connection(target, operator, database=params.get("database")) as conn:
            return await queries.fetch_schemas(conn)

    async def list_tables(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``postgres.tables`` -- user tables with vacuum/analyze stats + sizes."""
        async with self._connection(target, operator, database=params.get("database")) as conn:
            return await queries.fetch_tables(conn, params.get("schema"))

    async def list_indexes(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``postgres.indexes`` -- user indexes with scan counters + sizes."""
        async with self._connection(target, operator, database=params.get("database")) as conn:
            return await queries.fetch_indexes(conn, params.get("schema"))

    async def activity(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``postgres.activity`` -- current sessions (no query text)."""
        del params  # declared empty in schema
        async with self._connection(target, operator) as conn:
            return await queries.fetch_activity(conn)

    async def settings(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``postgres.settings`` -- curated (or caller-named) runtime settings."""
        async with self._connection(target, operator) as conn:
            return await queries.fetch_settings(conn, params.get("names"))

    async def run_query(
        self, operator: Operator, target: Target, params: dict[str, Any]
    ) -> dict[str, Any]:
        """``postgres.query`` -- a guarded free-form read-only SELECT.

        The statement is allowlisted by :func:`assert_read_only_sql` before a
        connection is opened, then executed on a server-enforced read-only
        session -- the double read-only guarantee.
        """
        sql = params["sql"]
        assert_read_only_sql(sql)
        max_rows = int(params.get("max_rows") or _DEFAULT_MAX_ROWS)
        async with self._connection(target, operator, database=params.get("database")) as conn:
            return await queries.run_select(conn, sql, max_rows)

    # ------------------------------------------------------------------
    # Fingerprint / probe
    # ------------------------------------------------------------------

    async def fingerprint(
        self, target: Target, operator: Operator | None = None
    ) -> FingerprintResult:
        """Canonical fingerprint: version, recovery, encoding, checksums, db sizes.

        Connects to the default maintenance database and reads
        ``server_version``, ``pg_is_in_recovery()``, ``server_encoding``,
        ``data_checksums``, and per-database sizes. A trust-auth target
        fingerprints with no operator; a credentialled target reads its secret
        under *operator* (``None`` fails closed inside the loader on a Vault
        backend). Any connection or credential failure maps to
        ``reachable=False`` with the error under ``extras`` rather than raising
        (#986 discipline) — the credential arm catches the backend-neutral
        :class:`CredentialsReadError`, so a ``gsm:`` read failure degrades the
        same way a Vault one does.
        """
        probed_at = datetime.now(UTC)
        try:
            async with self._connection(target, operator) as conn:
                identity = await queries.fetch_fingerprint(conn)
        except (
            OSError,
            asyncpg.PostgresError,
            CredentialsReadError,
            ValueError,
        ) as exc:
            _log.warning(
                "postgres_fingerprint_unreachable",
                target=getattr(target, "name", None),
                error=f"{type(exc).__name__}: {exc}",
            )
            return FingerprintResult(
                vendor="postgresql",
                product="postgres",
                reachable=False,
                probed_at=probed_at,
                probe_method="asyncpg: server_version",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )

        version = identity.pop("server_version", None)
        build = identity.pop("version_full", None)
        return FingerprintResult(
            vendor="postgresql",
            product="postgres",
            version=version,
            build=build,
            reachable=True,
            probed_at=probed_at,
            probe_method="asyncpg: server_version",
            extras=identity,
        )

    async def probe(self, target: Target) -> ProbeResult:
        """Reachability check via a ``SELECT 1`` handshake.

        Distinct ``reason`` values on failure:

        * ``auth_failed`` -- the server rejected the credential
          (:exc:`asyncpg.InvalidPasswordError` /
          :exc:`asyncpg.InvalidAuthorizationSpecificationError`) or the
          credential could not be resolved
          (:class:`CredentialsReadError` / :exc:`ValueError` -- a
          credentialled target on an operator-less probe).
        * ``tcp_unreachable`` -- the TCP connect failed (host down, firewall,
          wrong port; :exc:`OSError`, which covers
          :exc:`asyncio.TimeoutError`).
        * ``connect_failed`` -- the socket opened but the startup handshake
          failed for another server-side reason
          (:exc:`asyncpg.PostgresError`).

        ``probe`` carries no operator, so a credentialled target's secret read
        runs without an authenticated operator; on a Vault backend that fails
        closed to ``auth_failed`` -- reachability of a credentialled target is
        confirmed on the operator-carrying fingerprint / op path.
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
            async with self._connection(target, None) as conn:
                await conn.fetchval("SELECT 1")
        except (
            asyncpg.InvalidPasswordError,
            asyncpg.InvalidAuthorizationSpecificationError,
            CredentialsReadError,
            ValueError,
        ):
            return _result(False, "auth_failed")
        except OSError:
            return _result(False, "tcp_unreachable")
        except asyncpg.PostgresError:
            return _result(False, "connect_failed")
        return _result(True, None)

    # ------------------------------------------------------------------
    # Registration + dispatch shim
    # ------------------------------------------------------------------

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`PG_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan (via the registrar queued in
        :mod:`meho_backplane.connectors.postgres.__init__`) after the registry
        has eager-imported every connector module. Idempotent across pod
        restarts, mirroring the loki / bind9 shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        for op in PG_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"PostgresConnector op {op.op_id!r} declares handler_attr="
                    f"{op.handler_attr!r} but the class has no such attribute"
                )
            when_to_use: str | None
            if op.group_key is None:
                when_to_use = None
            else:
                when_to_use = PG_WHEN_TO_USE_BY_GROUP.get(op.group_key)
                if when_to_use is None:
                    raise ValueError(
                        f"PostgresConnector op {op.op_id!r} declares group_key="
                        f"{op.group_key!r} but no curated when_to_use exists for that key. "
                        "Add an entry to PG_WHEN_TO_USE_BY_GROUP in postgres/ops.py."
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
            "postgres_operations_registered",
            count=len(PG_OPS),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def execute(self, target: Target, op_id: str, params: dict[str, Any]) -> OperationResult:
        """Legacy shim -- delegates to the G0.6 dispatcher.

        Mirrors :meth:`LokiConnector.execute`. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI verbs)
        construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly. The connector's
        natural key encodes as ``"postgres-wire-16"`` per ``parse_connector_id``.
        The synthetic operator carries ``raw_jwt=""``, so a credentialled
        target reached through this shim fails closed in the credential loader
        -- the operator-context path is the real dispatch surface.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:postgres-wire-connector-shim",
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

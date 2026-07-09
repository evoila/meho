# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the read-only PostgreSQL wire-protocol connector (#2236).

Coverage matrix (per Task #2236 acceptance criteria):

* **Registration** -- ``postgres`` resolves via ``register_connector_v2``
  (versioned triple + wildcard), appears in ``all_connectors_v2()`` and
  ``registered_product_tokens()``; every op is safe/read-only/no-approval with
  a closed schema.
* **Double read-only enforcement** -- a non-allowlisted first keyword
  (``INSERT`` / ``UPDATE`` / ``DELETE`` / ``CREATE`` / ...) is rejected by
  :func:`assert_read_only_sql` *before* a connection is opened; the
  server-enforced half (``default_transaction_read_only=on``) is asserted at
  connect time (the session flag is a startup parameter) and exercised
  live in :mod:`tests.integration.test_connectors_postgres_container`.
* **Optional auth** -- a ``secret_ref=None`` (trust-auth) target connects with
  no password; a credentialled target passes the password to asyncpg and it
  never appears in logs (``capture_logs`` assertion).
* **Op payloads** -- ``list_tables`` returns vacuum/analyze stats; the row
  normaliser coerces temporals to ISO-8601; ``fingerprint`` returns the
  version/recovery/encoding/checksum fields + per-database sizes.
* **Live dispatch** -- the connector registers, an unknown op returns the
  ``unknown_op`` envelope, and a curated op dispatches end-to-end.

The wire is faked with an in-memory connection double; the in-process Vault
fake exercises the real credential loader. Mirrors
:mod:`tests.test_connectors_loki`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.postgres import (
    PG_OPS,
    PostgresConnector,
    PostgresReadOnlyError,
    assert_read_only_sql,
)
from meho_backplane.connectors.postgres import connector as connector_module
from meho_backplane.connectors.postgres import session as session_module
from meho_backplane.connectors.postgres.session import (
    ALLOWED_FIRST_KEYWORDS,
    DEFAULT_TRUST_USER,
    connect_read_only,
    first_significant_keyword,
)
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
    registered_product_tokens,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.settings import get_settings

from ._vault_fakes import install_fake_client

_PRODUCT = "postgres"
_VERSION = "16"
_IMPL_ID = "postgres-wire"
_CONNECTOR_ID = "postgres-wire-16"

_PG_HOST = "pg.test.invalid"
_PG_PORT = 5432

#: A clearly-fake password that must never reach a log line.
_CANARY_PASSWORD = "pg-canary-must-not-leak-12345"  # trufflehog:ignore


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars Settings reads (Vault client + dispatcher)."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    """Reset dispatcher/handler caches + connector registry around every test."""
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=PostgresConnector
    )
    register_connector_v2(product=_PRODUCT, version="", impl_id="", cls=PostgresConnector)
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """Deterministic embedding stub so registration doesn't pull ONNX."""
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    return AsyncMock()


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """AsyncSession against the autouse-migrated per-worker SQLite engine."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as s:
        yield s


class _PgTarget:
    """Target satisfying both the connector shape and the resolver shape."""

    def __init__(
        self,
        *,
        secret_ref: str | None = None,
        host: str = _PG_HOST,
        port: int | None = _PG_PORT,
    ) -> None:
        self.product = _PRODUCT
        self.fingerprint = type("_FP", (), {"version": _VERSION})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000c0")
        self.name = "pg-reads"
        self.host = host
        self.port = port
        self.secret_ref = secret_ref
        self.auth_model = None
        self.verify_tls = True
        self.tls_ca_pin = None
        self.tls_server_name = None
        self.extras: dict[str, Any] = {}


def _make_operator() -> Operator:
    """Operator carrying a non-empty raw_jwt (the fail-closed gate passes)."""
    return Operator(
        sub="op-reads-pg",
        name="PG Reads Operator",
        email=None,
        raw_jwt="op.reads.pg.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000c4"),
        tenant_role=TenantRole.OPERATOR,
    )


class _FakeCursor:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records

    async def fetch(self, n: int) -> list[dict[str, Any]]:
        return self._records[:n]


class _FakeTxn:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeTxn:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    """Minimal asyncpg.Connection double for the query surface."""

    def __init__(
        self,
        *,
        fetch_result: list[dict[str, Any]] | None = None,
        fetchrow_result: dict[str, Any] | None = None,
        fetchval_result: Any = 1,
        cursor_records: list[dict[str, Any]] | None = None,
    ) -> None:
        self._fetch_result = fetch_result or []
        self._fetchrow_result = fetchrow_result
        self._fetchval_result = fetchval_result
        self._cursor_records = cursor_records or []
        self.closed = False
        self.readonly_txn: bool | None = None
        self.cursor_sql: str | None = None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self._fetch_result

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return self._fetchrow_result

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._fetchval_result

    def transaction(self, *, readonly: bool = False) -> _FakeTxn:
        self.readonly_txn = readonly
        return _FakeTxn(self)

    async def cursor(self, sql: str) -> _FakeCursor:
        self.cursor_sql = sql
        return _FakeCursor(self._cursor_records)

    async def close(self) -> None:
        self.closed = True


def _patch_connection(monkeypatch: pytest.MonkeyPatch, conn: _FakeConn) -> AsyncMock:
    """Patch the connector's ``connect_read_only`` to yield *conn*."""
    mock = AsyncMock(return_value=conn)
    monkeypatch.setattr(connector_module, "connect_read_only", mock)
    return mock


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_postgres_resolves_versioned_and_wildcard_and_appears_in_registry() -> None:
    """AC: postgres resolves via register_connector_v2 (versioned + wildcard)."""
    registry = all_connectors_v2()
    assert registry[("postgres", "16", "postgres-wire")] is PostgresConnector
    assert registry[("postgres", "", "")] is PostgresConnector
    assert "postgres" in registered_product_tokens()

    assert resolve_connector(_PgTarget()) is PostgresConnector
    fresh = _PgTarget()
    fresh.fingerprint = type("_FP", (), {"version": None})()
    assert resolve_connector(fresh) is PostgresConnector


def test_every_op_is_safe_read_only_with_closed_schema() -> None:
    """AC: no write op -- every registered op is safe/read-only/no-approval."""
    assert {op.op_id for op in PG_OPS} == {
        "postgres.databases",
        "postgres.schemas",
        "postgres.tables",
        "postgres.indexes",
        "postgres.activity",
        "postgres.settings",
        "postgres.query",
    }
    for op in PG_OPS:
        assert op.safety_level == "safe", op.op_id
        assert op.requires_approval is False, op.op_id
        assert "read-only" in op.tags, op.op_id
        assert op.parameter_schema.get("additionalProperties") is False, op.op_id


# ---------------------------------------------------------------------------
# Read-only keyword gate -- rejected before a connection is opened
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO t VALUES (1)",
        "update t set x = 1",
        "DELETE FROM t",
        "CREATE TABLE t (i int)",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN c int",
        "TRUNCATE t",
        "GRANT SELECT ON t TO r",
        "COPY t FROM STDIN",
        "CALL do_thing()",
        "DO $$ BEGIN END $$",
        "REFRESH MATERIALIZED VIEW mv",
        "",
        "   ",
        ";;;",
    ],
)
def test_read_only_gate_rejects_non_read_first_keyword(sql: str) -> None:
    """AC: a non-allowlisted first keyword is rejected before execution."""
    with pytest.raises(PostgresReadOnlyError):
        assert_read_only_sql(sql)


@pytest.mark.parametrize(
    ("sql", "keyword"),
    [
        ("SELECT 1", "SELECT"),
        ("  select now()", "SELECT"),
        ("/* audit */ (SELECT 1)", "SELECT"),
        ("-- lead comment\nSHOW all", "SHOW"),
        ("WITH x AS (SELECT 1) SELECT * FROM x", "WITH"),
        ("EXPLAIN SELECT 1", "EXPLAIN"),
        ("TABLE pg_class", "TABLE"),
        ("VALUES (1), (2)", "VALUES"),
    ],
)
def test_read_only_gate_accepts_read_verbs(sql: str, keyword: str) -> None:
    """The six read verbs (with comments/parens skipped) pass the gate."""
    assert first_significant_keyword(sql) == keyword
    assert keyword in ALLOWED_FIRST_KEYWORDS
    assert_read_only_sql(sql)  # returns None; raises on violation


@pytest.mark.asyncio
async def test_run_query_rejects_write_without_opening_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write via postgres.query is refused before any connection is opened."""
    mock = _patch_connection(monkeypatch, _FakeConn())
    connector = PostgresConnector()
    with pytest.raises(PostgresReadOnlyError):
        await connector.run_query(_make_operator(), _PgTarget(), {"sql": "DELETE FROM t"})
    mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Server-enforced read-only + optional auth (connect_read_only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_trust_auth_sends_no_password_and_read_only_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a secret_ref=None (trust-auth) target connects with no password.

    Also asserts the server-enforced half of read-only: the connection is
    opened with ``default_transaction_read_only=on`` as a startup parameter.
    """
    fake_connect = AsyncMock(return_value=_FakeConn())
    monkeypatch.setattr(session_module.asyncpg, "connect", fake_connect)

    conn = await connect_read_only(_PgTarget(secret_ref=None), None)
    assert conn is not None

    kwargs = fake_connect.await_args.kwargs
    assert kwargs["user"] == DEFAULT_TRUST_USER
    assert "password" not in kwargs
    assert kwargs["server_settings"]["default_transaction_read_only"] == "on"
    assert kwargs["port"] == _PG_PORT


@pytest.mark.asyncio
async def test_connect_credentialled_passes_password_and_never_logs_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a credentialled target passes the password to asyncpg, unlogged."""
    fake_connect = AsyncMock(return_value=_FakeConn())
    monkeypatch.setattr(session_module.asyncpg, "connect", fake_connect)
    install_fake_client(monkeypatch, secret={"username": "pg_ro", "password": _CANARY_PASSWORD})

    with structlog.testing.capture_logs() as logs:
        await connect_read_only(_PgTarget(secret_ref="targets/op-reads/pg"), _make_operator())

    kwargs = fake_connect.await_args.kwargs
    assert kwargs["user"] == "pg_ro"
    assert kwargs["password"] == _CANARY_PASSWORD
    assert kwargs["server_settings"]["default_transaction_read_only"] == "on"

    # The password must not appear anywhere in the captured structured logs.
    serialized = repr(logs)
    assert _CANARY_PASSWORD not in serialized


@pytest.mark.asyncio
async def test_connect_credentialled_without_operator_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credentialled target on an operator-less path fails closed (no connect)."""
    fake_connect = AsyncMock(return_value=_FakeConn())
    monkeypatch.setattr(session_module.asyncpg, "connect", fake_connect)

    with pytest.raises(ValueError, match="no authenticated operator"):
        await connect_read_only(_PgTarget(secret_ref="targets/x/pg"), None)
    fake_connect.assert_not_awaited()


# ---------------------------------------------------------------------------
# Op payloads -- row shaping + JSON coercion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_tables_returns_vacuum_stats_and_iso_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: list_tables returns vacuum/analyze stats; temporals become ISO-8601."""
    last_vacuum = dt.datetime(2026, 7, 9, 12, 0, tzinfo=dt.UTC)
    conn = _FakeConn(
        fetch_result=[
            {
                "schema": "public",
                "name": "orders",
                "live_tuples": 1000,
                "dead_tuples": 42,
                "mods_since_analyze": 7,
                "last_vacuum": last_vacuum,
                "last_autovacuum": None,
                "last_analyze": last_vacuum,
                "last_autoanalyze": None,
                "vacuum_count": 3,
                "autovacuum_count": 9,
                "analyze_count": 2,
                "autoanalyze_count": 8,
                "total_bytes": 8192,
                "table_bytes": 4096,
                "indexes_bytes": 4096,
            }
        ]
    )
    _patch_connection(monkeypatch, conn)

    result = await PostgresConnector().list_tables(
        _make_operator(), _PgTarget(), {"schema": "public"}
    )
    row = result["tables"][0]
    assert row["dead_tuples"] == 42
    assert row["autovacuum_count"] == 9
    assert row["last_vacuum"] == last_vacuum.isoformat()
    assert row["last_autovacuum"] is None
    assert conn.closed is True


@pytest.mark.asyncio
async def test_activity_omits_query_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """postgres.activity returns sessions but never the in-flight query text."""
    conn = _FakeConn(
        fetch_result=[{"pid": 42, "database": "app", "username": "svc", "state": "active"}]
    )
    _patch_connection(monkeypatch, conn)
    result = await PostgresConnector().activity(_make_operator(), _PgTarget(), {})
    assert result["sessions"][0]["pid"] == 42
    assert "query" not in result["sessions"][0]


@pytest.mark.asyncio
async def test_run_query_bounds_rows_and_flags_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """postgres.query caps rows at max_rows and flags truncation."""
    records = [{"n": i} for i in range(5)]
    conn = _FakeConn(cursor_records=records)
    _patch_connection(monkeypatch, conn)

    result = await PostgresConnector().run_query(
        _make_operator(),
        _PgTarget(),
        {"sql": "SELECT n FROM generate_series(1,100) n", "max_rows": 3},
    )
    assert result["row_count"] == 3
    assert result["truncated"] is True
    assert conn.readonly_txn is True


@pytest.mark.asyncio
async def test_fingerprint_returns_version_recovery_encoding_checksums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: fingerprint returns version/recovery/encoding/checksum + db sizes."""
    conn = _FakeConn(
        fetchrow_result={
            "server_version": "16.3",
            "version_full": "PostgreSQL 16.3 on x86_64-pc-linux-gnu",
            "in_recovery": False,
            "encoding": "UTF8",
            "data_checksums": "on",
        },
        fetch_result=[{"name": "app", "size_bytes": 12345}],
    )
    _patch_connection(monkeypatch, conn)

    fp = await PostgresConnector().fingerprint(_PgTarget(), _make_operator())
    assert fp.reachable is True
    assert fp.vendor == "postgresql"
    assert fp.product == "postgres"
    assert fp.version == "16.3"
    assert fp.build.startswith("PostgreSQL 16.3")
    assert fp.extras["in_recovery"] is False
    assert fp.extras["encoding"] == "UTF8"
    assert fp.extras["data_checksums"] == "on"
    assert fp.extras["database_sizes"] == [{"name": "app", "size_bytes": 12345}]


@pytest.mark.asyncio
async def test_fingerprint_unreachable_maps_to_not_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect failure maps to reachable=False with the error under extras."""
    monkeypatch.setattr(
        connector_module,
        "connect_read_only",
        AsyncMock(side_effect=OSError("connection refused")),
    )
    fp = await PostgresConnector().fingerprint(_PgTarget(), _make_operator())
    assert fp.reachable is False
    assert "error" in fp.extras


# ---------------------------------------------------------------------------
# Probe reasons
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable target with a working SELECT 1 probes ok."""
    _patch_connection(monkeypatch, _FakeConn(fetchval_result=1))
    result = await PostgresConnector().probe(_PgTarget(secret_ref=None))
    assert result.ok is True
    assert result.reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (__import__("asyncpg").InvalidPasswordError("bad password"), "auth_failed"),
        (OSError("no route to host"), "tcp_unreachable"),
        (__import__("asyncpg").PostgresError("startup failed"), "connect_failed"),
    ],
)
async def test_probe_failure_reasons(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, reason: str
) -> None:
    """Each connect failure maps to its distinct probe reason."""
    monkeypatch.setattr(connector_module, "connect_read_only", AsyncMock(side_effect=exc))
    result = await PostgresConnector().probe(_PgTarget(secret_ref=None))
    assert result.ok is False
    assert result.reason == reason


# ---------------------------------------------------------------------------
# Live dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_op_returns_unknown_op_envelope(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """After registration, an unknown op_id returns the unknown_op envelope."""
    await PostgresConnector.register_operations()
    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="postgres.nonexistent",
        target=_PgTarget(secret_ref=None),
        params={},
    )
    assert result.status == "error"
    assert result.error.startswith("unknown_op")


@pytest.mark.asyncio
async def test_databases_op_dispatches_live_and_returns_payload(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: a curated op dispatches end-to-end and returns its payload."""
    await PostgresConnector.register_operations()
    conn = _FakeConn(
        fetch_result=[{"name": "app", "owner": "postgres", "encoding": "UTF8", "size_bytes": 999}]
    )
    _patch_connection(monkeypatch, conn)

    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="postgres.databases",
        target=_PgTarget(secret_ref=None),
        params={},
    )
    assert result.status == "ok", result.error
    assert result.result == {
        "databases": [{"name": "app", "owner": "postgres", "encoding": "UTF8", "size_bytes": 999}]
    }
    assert conn.closed is True


@pytest.mark.asyncio
async def test_query_op_with_invalid_params_returns_invalid_params(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """postgres.query without the required 'sql' param returns invalid_params."""
    await PostgresConnector.register_operations()
    result = await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id="postgres.query",
        target=_PgTarget(secret_ref=None),
        params={"max_rows": 10},
    )
    assert result.status == "error"
    assert result.error.startswith("invalid_params")

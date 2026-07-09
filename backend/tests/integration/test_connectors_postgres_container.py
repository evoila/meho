# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Integration smoke test for :class:`PostgresConnector` against a real Postgres.

Boots a stock ``postgres:16-alpine`` container and exercises the connector's
:meth:`fingerprint`, curated read ops, and -- critically -- the
**server-enforced** half of the double read-only guarantee end-to-end over the
real asyncpg wire path:

* ``fingerprint`` returns ``server_version`` / ``in_recovery`` / ``encoding`` /
  ``data_checksums`` and per-database sizes (Task #2236 AC).
* ``postgres.tables`` returns vacuum/analyze statistics for a table that has
  been populated and ANALYZEd (Task #2236 AC).
* A write attempted on a :func:`connect_read_only` session is rejected by
  PostgreSQL with ``ReadOnlySqlTransactionError`` -- the
  ``default_transaction_read_only=on`` backstop (Task #2236 AC), independent of
  the first-keyword allowlist.
* ``postgres.query`` reads through the allowlist and refuses a write before the
  wire.

The credential loader is exercised through the in-process Vault fake (there is
no real Vault in this lane), which returns the container's own credentials so
the connector's operator-context read path runs for real.

Skip conditions mirror ``tests/integration/conftest.py``: no Docker socket ->
skip (agent sandbox); CI provisions Docker so the test runs there. A Docker Hub
pull rate-limit is converted to a skip so the suite's pass/fail signal stays
meaningful.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import asyncpg
import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.postgres import PostgresConnector, PostgresReadOnlyError
from meho_backplane.connectors.postgres.session import connect_read_only
from meho_backplane.settings import get_settings

from .._vault_fakes import install_fake_client


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


DOCKER_AVAILABLE: bool = _docker_socket_present()
SKIP_REASON: str = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)

pytestmark = pytest.mark.skipif(not DOCKER_AVAILABLE, reason=SKIP_REASON)


@dataclass
class _PgLiveTarget:
    """Target double carrying the container's connection coordinates.

    ``secret_ref`` is set so the connector resolves credentials through the
    operator-context loader (fed by the Vault fake); the loader keeps the
    integration-double tenant_id / id fields present so nothing downstream
    trips on a missing attribute (the integration-double trap).
    """

    host: str
    port: int
    secret_ref: str = "targets/pg-live"
    name: str = "pg-live"
    id: str = "00000000-0000-0000-0000-0000000000d0"
    tenant_id: str = "00000000-0000-0000-0000-000000000000"
    product: str = "postgres"
    version: str | None = "16"
    extras: dict[str, object] = field(default_factory=dict)


def _operator() -> Operator:
    return Operator(
        sub="op-pg-live",
        name="PG Live Operator",
        email=None,
        raw_jwt="op.pg.live.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Container:
    host: str
    port: int
    user: str
    password: str
    dbname: str


@pytest.fixture(scope="module")
def pg_container() -> Iterator[_Container]:
    """Boot a stock Postgres, create a populated + ANALYZEd table, yield coords."""
    from docker.errors import APIError as _DockerAPIError
    from testcontainers.postgres import PostgresContainer

    image = os.environ.get("MEHO_TEST_POSTGRES_IMAGE", "postgres:16-alpine")
    pg = PostgresContainer(image)
    try:
        pg.start()
    except _DockerAPIError as exc:
        msg = str(exc).lower()
        if "rate limit" in msg or "too many requests" in msg or "429" in msg:
            pytest.skip(f"Docker Hub pull rate-limited for {image!r}; set MEHO_TEST_POSTGRES_IMAGE")
        raise
    try:
        coords = _Container(
            host=pg.get_container_host_ip(),
            port=int(pg.get_exposed_port(5432)),
            user=pg.username,
            password=pg.password,
            dbname=pg.dbname,
        )
        import asyncio

        asyncio.run(_seed(coords))
        yield coords
    finally:
        pg.stop()


async def _seed(c: _Container) -> None:
    """Create + populate + ANALYZE a table so pg_stat_user_tables has stats."""
    conn = await asyncpg.connect(
        host=c.host, port=c.port, user=c.user, password=c.password, database=c.dbname
    )
    try:
        await conn.execute("CREATE TABLE orders (id int PRIMARY KEY, note text)")
        await conn.executemany(
            "INSERT INTO orders (id, note) VALUES ($1, $2)",
            [(i, f"note-{i}") for i in range(50)],
        )
        await conn.execute("ANALYZE orders")
    finally:
        await conn.close()


@pytest.fixture
def _vault(monkeypatch: pytest.MonkeyPatch, pg_container: _Container) -> None:
    """Route the credential loader at the container's own credentials."""
    get_settings.cache_clear()
    install_fake_client(
        monkeypatch,
        secret={"username": pg_container.user, "password": pg_container.password},
    )


@pytest.mark.asyncio
async def test_fingerprint_returns_identity_fields(pg_container: _Container, _vault: None) -> None:
    """AC: fingerprint returns version / recovery / encoding / checksums + sizes."""
    target = _PgLiveTarget(host=pg_container.host, port=pg_container.port)
    fp = await PostgresConnector().fingerprint(target, _operator())

    assert fp.reachable is True, fp.extras
    assert fp.vendor == "postgresql"
    assert fp.version is not None and fp.version.startswith("16")
    assert fp.extras["in_recovery"] is False
    assert fp.extras["encoding"]  # e.g. "UTF8"
    assert fp.extras["data_checksums"] in {"on", "off"}
    assert isinstance(fp.extras["database_sizes"], list) and fp.extras["database_sizes"]


@pytest.mark.asyncio
async def test_list_tables_returns_vacuum_analyze_stats(
    pg_container: _Container, _vault: None
) -> None:
    """AC: list_tables returns vacuum/analyze stats against a live Postgres."""
    target = _PgLiveTarget(host=pg_container.host, port=pg_container.port)
    result = await PostgresConnector().list_tables(
        _operator(), target, {"database": pg_container.dbname, "schema": "public"}
    )
    orders = next((r for r in result["tables"] if r["name"] == "orders"), None)
    assert orders is not None, result["tables"]
    # The vacuum/analyze statistics columns are present and typed.
    for key in (
        "live_tuples",
        "dead_tuples",
        "last_analyze",
        "vacuum_count",
        "autovacuum_count",
        "total_bytes",
    ):
        assert key in orders, key
    assert orders["last_analyze"] is not None  # we ran ANALYZE in _seed
    assert orders["total_bytes"] > 0


@pytest.mark.asyncio
async def test_session_write_fails_server_side(pg_container: _Container, _vault: None) -> None:
    """AC: a session-level write fails server-side (default_transaction_read_only).

    Independent of the keyword allowlist: the statement is executed directly on
    a ``connect_read_only`` session, so only the server-enforced read-only flag
    can reject it.
    """
    target = _PgLiveTarget(host=pg_container.host, port=pg_container.port)
    conn = await connect_read_only(target, _operator(), database=pg_container.dbname)
    try:
        with pytest.raises(asyncpg.PostgresError) as excinfo:
            await conn.execute("CREATE TABLE ro_probe (i int)")
        # SQLSTATE 25006 -- read_only_sql_transaction.
        assert getattr(excinfo.value, "sqlstate", None) == "25006"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_run_query_reads_and_blocks_write(pg_container: _Container, _vault: None) -> None:
    """postgres.query reads via the allowlist; a write is refused before the wire."""
    target = _PgLiveTarget(host=pg_container.host, port=pg_container.port)
    connector = PostgresConnector()

    ok = await connector.run_query(
        _operator(),
        target,
        {"sql": "SELECT count(*) AS n FROM orders", "database": pg_container.dbname},
    )
    assert ok["rows"][0]["n"] == 50
    assert ok["truncated"] is False

    with pytest.raises(PostgresReadOnlyError):
        await connector.run_query(
            _operator(),
            target,
            {"sql": "DELETE FROM orders", "database": pg_container.dbname},
        )

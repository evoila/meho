# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0167_add_policy_rule_columns``.

The migration only adds four nullable columns: ``policy_rule`` and
``policy_version`` on ``audit_log`` and on ``approval_request``. An old
row reads ``NULL`` in each. ``downgrade()`` removes them and keeps the
rows.

The PostgreSQL tests check that the append-only trigger on ``audit_log``
(``0100`` / ``0103``) lets ``ADD COLUMN`` through, that it is still in
place after the upgrade, and that it still refuses an ``UPDATE``, also of
a new column. They run where Docker is there (CI), like the other
PostgreSQL migration tests.

Every step targets this migration's own revision (``0167``) and its
``down_revision`` (``0166``), never ``head``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings
from tests._pgvector_image import pgvector_test_image

_REVISION = "0167"
_DOWN_REVISION = "0166"
_AUDIT_ID = "a" * 32
_REQUEST_ID = "c" * 32
_TENANT_ID = "b" * 32
_NEW_COLUMNS = {"policy_rule", "policy_version"}

_INSERT_AUDIT = (
    "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, status_code, payload) "
    "VALUES (:id, '2026-11-13 00:00:00', 'fixture', 'DISPATCH', 'x.y', 200, '{}')"
)


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0167.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, f"sqlite:///{db_path}"
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _columns(sync_url: str, table: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        return {c["name"] for c in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _run(sync_url: str, sql: str, **params: str | None) -> list[tuple[object, ...]]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()


def _insert_rows(sync_url: str) -> None:
    _run(sync_url, _INSERT_AUDIT, id=_AUDIT_ID)
    _run(
        sync_url,
        "INSERT INTO approval_request (id, tenant_id, principal_sub, op_id, connector_id, "
        "params_hash, proposed_effect, status, created_at) VALUES (:id, :tenant, 'fixture', "
        "'x.y', 'x-1.x', 'h', '{}', 'pending', '2026-11-13 00:00:00')",
        id=_REQUEST_ID,
        tenant=_TENANT_ID,
    )


def test_old_rows_read_null(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_rows(sync_url)

    command.upgrade(cfg, _REVISION)

    assert _run(sync_url, "SELECT policy_rule, policy_version FROM audit_log") == [(None, None)]
    assert _run(sync_url, "SELECT policy_rule, policy_version FROM approval_request") == [
        (None, None)
    ]


def test_up_and_down_keep_the_rows(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_rows(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url, "audit_log") >= _NEW_COLUMNS
    assert _columns(sync_url, "approval_request") >= _NEW_COLUMNS

    command.downgrade(cfg, _DOWN_REVISION)
    assert not _NEW_COLUMNS & _columns(sync_url, "audit_log")
    assert not _NEW_COLUMNS & _columns(sync_url, "approval_request")
    assert _run(sync_url, "SELECT path FROM audit_log") == [("x.y",)]
    assert _run(sync_url, "SELECT op_id FROM approval_request") == [("x.y",)]


# ---------------------------------------------------------------------------
# PostgreSQL, with the append-only trigger in place
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    """Same Docker check as the other testcontainers PostgreSQL tests."""
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_needs_docker = pytest.mark.skipif(
    not _docker_socket_present(),
    reason="Docker socket unavailable in this sandbox; runs in CI where containers are there.",
)


def _async_url_from(sync_url: str) -> str:
    """Turn the testcontainers sync URL into the asyncpg URL the app uses."""
    return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://").replace(
        "postgresql://", "postgresql+asyncpg://"
    )


def _alembic(async_url: str, action: str, revision: str) -> None:
    """Run ``alembic upgrade|downgrade <revision>`` against *async_url*."""
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("DATABASE_URL", async_url)
        getattr(command, action)(cfg, revision)


async def _pg_execute(async_url: str, sql: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(async_url)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(sql))
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        await engine.dispose()


def _pg(async_url: str, sql: str) -> list[tuple[object, ...]]:
    return asyncio.run(_pg_execute(async_url, sql))


_PG_COLUMNS = (
    "SELECT column_name, data_type FROM information_schema.columns "
    "WHERE table_name = '{table}' AND column_name IN ('policy_rule', 'policy_version') "
    "ORDER BY column_name"
)
_TRIGGER = (
    "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'audit_log'::regclass "
    "AND tgname = 'audit_log_append_only'"
)


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    """One PostgreSQL for the module, at ``0166`` with one audit row and the trigger."""
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer(pgvector_test_image()) as container:
        async_url = _async_url_from(container.get_connection_url())
        _alembic(async_url, "upgrade", _DOWN_REVISION)
        _pg(
            async_url,
            "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, status_code, "
            "payload) VALUES (gen_random_uuid(), now(), 'fixture', 'DISPATCH', 'x.y', 200, '{}')",
        )
        yield async_url


@_needs_docker
def test_postgres_add_column_passes_the_append_only_trigger(pg_url: str) -> None:
    assert _pg(pg_url, _TRIGGER) == [(1,)]

    _alembic(pg_url, "upgrade", _REVISION)

    assert _pg(pg_url, _PG_COLUMNS.format(table="audit_log")) == [
        ("policy_rule", "text"),
        ("policy_version", "integer"),
    ]
    assert _pg(pg_url, _PG_COLUMNS.format(table="approval_request")) == [
        ("policy_rule", "jsonb"),
        ("policy_version", "integer"),
    ]
    assert _pg(pg_url, "SELECT policy_rule, policy_version FROM audit_log") == [(None, None)]
    # The trigger is still there and still refuses a change, also of a new column.
    assert _pg(pg_url, _TRIGGER) == [(1,)]
    with pytest.raises(DBAPIError, match="append-only"):
        _pg(pg_url, "UPDATE audit_log SET policy_rule = 'changed'")

    _alembic(pg_url, "downgrade", _DOWN_REVISION)
    assert _pg(pg_url, _PG_COLUMNS.format(table="audit_log")) == []
    assert _pg(pg_url, _PG_COLUMNS.format(table="approval_request")) == []
    assert _pg(pg_url, "SELECT path FROM audit_log") == [("x.y",)]

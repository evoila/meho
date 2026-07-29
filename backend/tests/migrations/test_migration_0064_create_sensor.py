# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0064_create_sensor``.

Initiative #2416 (parent goal #221), Task #2503. The migration adds the
``sensor`` table -- the deterministic check layer's registration
substrate. Modelled on :mod:`tests.migrations.test_migration_0020_scheduled_trigger`.

Every upgrade target is the explicit revision ``"0064"`` (never ``"head"``)
so the column / nullability expectations stay pinned to the 0064 snapshot
regardless of how many later migrations land -- the pin-to-own-revision
discipline the migration-test suite follows.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0064.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    sync_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, sync_url
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _table_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def _table_columns(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _column_is_nullable(sync_url: str, table: str, column: str) -> bool:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            # notnull is index 3: 0 => nullable, 1 => NOT NULL.
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on {table}")


def _table_indexes(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


_EXPECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "name",
        "connector_id",
        "op_id",
        "target",
        "params",
        "assertion",
        "status",
        "status_reason",
        "cadence_kind",
        "interval_seconds",
        "cron_expr",
        "timezone",
        "next_fire_at",
        "severity",
        "for_seconds",
        "last_state",
        "last_value",
        "last_evidence",
        "last_evaluated_at",
        "state_since",
        "identity_sub",
        "created_by_sub",
        "created_at",
        "updated_at",
    }
)

_EXPECTED_INDEXES: frozenset[str] = frozenset(
    {
        "sensor_due_idx",
        "sensor_tenant_idx",
        "sensor_tenant_name_idx",
    }
)


def test_upgrade_creates_sensor_table_columns_indexes(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade 0064`` lands ``sensor`` with the 0064 columns + indexes."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0064")

    assert "sensor" in _table_names(sync_url), "migration 0064 must create the sensor table"

    columns = _table_columns(sync_url, "sensor")
    assert columns == _EXPECTED_COLUMNS, (
        f"sensor columns drifted from the documented set: "
        f"missing={_EXPECTED_COLUMNS - columns}, extra={columns - _EXPECTED_COLUMNS}"
    )

    indexes = _table_indexes(sync_url, "sensor")
    for expected in _EXPECTED_INDEXES:
        assert expected in indexes, f"migration 0064 must create index {expected!r}"


def test_column_nullability(alembic_cfg: tuple[Config, str]) -> None:
    """NOT NULL columns reject NULL; nullable columns permit it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0064")

    not_null = (
        "tenant_id",
        "name",
        "connector_id",
        "op_id",
        "params",
        "assertion",
        "status",
        "cadence_kind",
        "timezone",
        "severity",
        "for_seconds",
        "last_state",
        "identity_sub",
        "created_by_sub",
        "created_at",
        "updated_at",
    )
    for col in not_null:
        assert not _column_is_nullable(sync_url, "sensor", col), f"{col} must be NOT NULL"

    nullable = (
        "target",
        "status_reason",
        "interval_seconds",
        "cron_expr",
        "next_fire_at",
        "last_value",
        "last_evidence",
        "last_evaluated_at",
        "state_since",
    )
    for col in nullable:
        assert _column_is_nullable(sync_url, "sensor", col), f"{col} must be nullable"


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0063"`` drops the table; ``upgrade 0064`` restores it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0064")
    assert "sensor" in _table_names(sync_url)

    command.downgrade(cfg, "0063")
    assert "sensor" not in _table_names(sync_url), "downgrade must drop the sensor table"

    command.upgrade(cfg, "0064")
    assert "sensor" in _table_names(sync_url)
    assert _table_columns(sync_url, "sensor") == _EXPECTED_COLUMNS


def test_sibling_tables_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """0064 adds a new table only; the pre-existing tables survive.

    Targets the explicit revision ``"0064"`` (the head at authoring time)
    rather than ``"head"`` so the test keeps exercising *this* migration's
    upgrade even once a later migration becomes head.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0064")

    tables = _table_names(sync_url)
    assert "sensor" in tables
    # A representative sibling from an earlier migration must be intact.
    assert "scheduled_trigger" in tables
    assert "tenant" in tables

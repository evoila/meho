# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0065_create_check_dashboards``.

Initiative #2416 (parent goal #221), Task #2506. The migration adds the
Dashboard entity -- ``check_dashboards`` plus its ``check_dashboard_sensors``
membership join. Modelled on
:mod:`tests.migrations.test_migration_0064_create_sensor`.

Every upgrade target is the explicit revision ``"0065"`` (never ``"head"``)
so the column / nullability expectations stay pinned to the 0065 snapshot
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
    db_path = tmp_path / "migration_0065.db"
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


_DASHBOARD_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "name",
        "description",
        "last_rollup_state",
        "created_by_sub",
        "created_at",
        "updated_at",
    }
)

_MEMBER_COLUMNS: frozenset[str] = frozenset({"dashboard_id", "sensor_id"})


def test_upgrade_creates_dashboard_tables_columns_indexes(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade 0065`` lands both Dashboard tables with the 0065 shape."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0065")

    tables = _table_names(sync_url)
    assert "check_dashboards" in tables
    assert "check_dashboard_sensors" in tables

    assert _table_columns(sync_url, "check_dashboards") == _DASHBOARD_COLUMNS
    assert _table_columns(sync_url, "check_dashboard_sensors") == _MEMBER_COLUMNS

    dashboard_indexes = _table_indexes(sync_url, "check_dashboards")
    assert "check_dashboard_tenant_idx" in dashboard_indexes
    assert "check_dashboard_tenant_name_idx" in dashboard_indexes
    member_indexes = _table_indexes(sync_url, "check_dashboard_sensors")
    assert "check_dashboard_sensors_sensor_idx" in member_indexes


def test_column_nullability(alembic_cfg: tuple[Config, str]) -> None:
    """NOT NULL columns reject NULL; nullable columns permit it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0065")

    not_null = (
        "tenant_id",
        "name",
        "created_by_sub",
        "created_at",
        "updated_at",
    )
    for col in not_null:
        assert not _column_is_nullable(sync_url, "check_dashboards", col), f"{col} must be NOT NULL"

    nullable = ("description", "last_rollup_state")
    for col in nullable:
        assert _column_is_nullable(sync_url, "check_dashboards", col), f"{col} must be nullable"

    # Both membership columns are the composite PK -> NOT NULL.
    for col in ("dashboard_id", "sensor_id"):
        assert not _column_is_nullable(sync_url, "check_dashboard_sensors", col)


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0064"`` drops both tables; ``upgrade 0065`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0065")
    assert "check_dashboards" in _table_names(sync_url)
    assert "check_dashboard_sensors" in _table_names(sync_url)

    command.downgrade(cfg, "0064")
    tables_after_down = _table_names(sync_url)
    assert "check_dashboards" not in tables_after_down
    assert "check_dashboard_sensors" not in tables_after_down
    # The sensor table (0064) survives the 0065 downgrade.
    assert "sensor" in tables_after_down

    command.upgrade(cfg, "0065")
    assert _table_columns(sync_url, "check_dashboards") == _DASHBOARD_COLUMNS
    assert _table_columns(sync_url, "check_dashboard_sensors") == _MEMBER_COLUMNS


def test_sibling_tables_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """0065 adds new tables only; the pre-existing tables survive.

    Targets the explicit revision ``"0065"`` (the head at authoring time)
    rather than ``"head"`` so the test keeps exercising *this* migration's
    upgrade even once a later migration becomes head.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0065")

    tables = _table_names(sync_url)
    assert "check_dashboards" in tables
    # Representative siblings from earlier migrations must be intact.
    assert "sensor" in tables
    assert "scheduled_trigger" in tables
    assert "tenant" in tables

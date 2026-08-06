# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Migration 0071 -- create the ``sensor_results`` evidence-history table.

Task #2756 under Initiative #2780 (parent goal #221). Asserts the additive
create-table migration lands the documented columns + indexes, the
nullability contract holds, the downgrade round-trips, and sibling tables are
untouched. Every upgrade target is the explicit revision ``"0071"`` (never
``"head"``) so the test keeps exercising *this* migration regardless of how
many later migrations land -- the pin-to-own-revision discipline the 0064
migration test established.
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
    db_path = tmp_path / "migration_0071.db"
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
        "sensor_id",
        "evaluated_at",
        "state",
        "value",
        "evidence",
        "reason",
    }
)

_EXPECTED_INDEXES: frozenset[str] = frozenset(
    {
        "sensor_results_sensor_evaluated_idx",
        "sensor_results_evaluated_at_idx",
    }
)


def test_upgrade_creates_sensor_results_table_columns_indexes(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade 0071`` lands ``sensor_results`` with its columns + indexes."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0071")

    assert "sensor_results" in _table_names(sync_url), (
        "migration 0071 must create the sensor_results table"
    )

    columns = _table_columns(sync_url, "sensor_results")
    assert columns == _EXPECTED_COLUMNS, (
        f"sensor_results columns drifted from the documented set: "
        f"missing={_EXPECTED_COLUMNS - columns}, extra={columns - _EXPECTED_COLUMNS}"
    )

    indexes = _table_indexes(sync_url, "sensor_results")
    for expected in _EXPECTED_INDEXES:
        assert expected in indexes, f"migration 0071 must create index {expected!r}"


def test_column_nullability(alembic_cfg: tuple[Config, str]) -> None:
    """NOT NULL columns reject NULL; nullable columns permit it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0071")

    for col in ("sensor_id", "evaluated_at", "state"):
        assert not _column_is_nullable(sync_url, "sensor_results", col), f"{col} must be NOT NULL"

    for col in ("value", "evidence", "reason"):
        assert _column_is_nullable(sync_url, "sensor_results", col), f"{col} must be nullable"


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0070"`` drops the table; ``upgrade 0071`` restores it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0071")
    assert "sensor_results" in _table_names(sync_url)

    command.downgrade(cfg, "0070")
    assert "sensor_results" not in _table_names(sync_url), (
        "downgrade must drop the sensor_results table"
    )

    command.upgrade(cfg, "0071")
    assert "sensor_results" in _table_names(sync_url)
    assert _table_columns(sync_url, "sensor_results") == _EXPECTED_COLUMNS


def test_sibling_tables_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """0071 adds a new table only; the pre-existing tables survive."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0071")

    tables = _table_names(sync_url)
    assert "sensor_results" in tables
    # The parent sensor table (0064) and a representative earlier sibling.
    assert "sensor" in tables
    assert "tenant" in tables

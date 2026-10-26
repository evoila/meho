# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0107_create_request_usage_event`` (#3734).

The migration adds one new table with two indexes. The tests check that
``upgrade`` creates the table and indexes, that ``downgrade`` removes
them, and that the pair round-trips. SQLite is the test driver.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0107"
_DOWN_REVISION = "0106"
_TABLE = "request_usage_event"
_INDEXES = {
    "request_usage_event_tenant_occurred_idx",
    "request_usage_event_occurred_at_idx",
}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0107.db"
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


def _schema(sync_url: str) -> tuple[set[str], set[str], set[str]]:
    """Return ``(tables, request_usage_event columns, its index names)``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            if _TABLE not in tables:
                return tables, set(), set()
            columns = {column["name"] for column in inspector.get_columns(_TABLE)}
            indexes = {str(index["name"]) for index in inspector.get_indexes(_TABLE)}
    finally:
        sync_eng.dispose()
    return tables, columns, indexes


def test_upgrade_creates_table_and_indexes(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    tables, columns, indexes = _schema(sync_url)
    assert _TABLE in tables
    assert columns == {
        "id",
        "kind",
        "occurred_at",
        "tenant_id",
        "principal_sub",
        "session_id",
        "run_id",
        "request_id",
        "correlation_id",
        "transport",
        "route",
        "outcome",
        "duration_ms",
    }
    assert indexes >= _INDEXES


def test_downgrade_drops_table(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert _TABLE not in _schema(sync_url)[0]


def test_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)
    assert _TABLE in _schema(sync_url)[0]

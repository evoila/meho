# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0187_add_sensor_runner_name``.

The migration adds the ``sensor.runner_name`` column and an index on
``runner_check_results.received_at``. The tests check that ``upgrade`` adds
both, that ``downgrade`` removes both, and that the pair round-trips. SQLite
is the test driver.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import Sensor
from meho_backplane.settings import get_settings

_REVISION = "0187"
_DOWN_REVISION = "0186"
_TABLE = "sensor"
_RESULTS_TABLE = "runner_check_results"
_INDEX = "runner_check_results_received_at_idx"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0187.db"
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


def _schema(sync_url: str) -> tuple[set[str], set[str]]:
    """Return ``(sensor columns, runner_check_results index names)``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = inspect(conn)
            return (
                {column["name"] for column in inspector.get_columns(_TABLE)},
                {str(index["name"]) for index in inspector.get_indexes(_RESULTS_TABLE)},
            )
    finally:
        sync_eng.dispose()


def test_upgrade_adds_the_column_and_the_index(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)

    columns, indexes = _schema(sync_url)
    assert columns == set(Sensor.__table__.columns.keys())
    assert {"runner_name", "runner_produced_at"} <= columns
    assert _INDEX in indexes


def test_downgrade_removes_the_column_and_the_index(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    columns, indexes = _schema(sync_url)
    assert not {"runner_name", "runner_produced_at"} & columns
    assert _INDEX not in indexes


def test_upgrade_round_trips(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    columns, indexes = _schema(sync_url)
    assert {"runner_name", "runner_produced_at"} <= columns
    assert _INDEX in indexes

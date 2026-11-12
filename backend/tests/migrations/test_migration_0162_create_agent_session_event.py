# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0162_create_agent_session_event``.

The migration adds the table of the saved events of hosted agent runs. The
tests check that ``upgrade`` creates it with the columns of the model, that
``downgrade`` removes it, and that the pair round-trips. SQLite is the test
driver.
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
from meho_backplane.session_store.models import AgentSessionEvent
from meho_backplane.settings import get_settings

_REVISION = "0162"
_DOWN_REVISION = "0161"
_MODELS = (AgentSessionEvent,)


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0162.db"
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


def _schema(sync_url: str) -> dict[str, tuple[set[str], set[str]]]:
    """Return ``{table: (columns, index names)}`` for the tables that exist."""
    sync_eng = sa_create_engine(sync_url)
    found: dict[str, tuple[set[str], set[str]]] = {}
    try:
        with sync_eng.connect() as conn:
            inspector = inspect(conn)
            tables = set(inspector.get_table_names())
            for model in _MODELS:
                table = model.__tablename__
                if table in tables:
                    found[table] = (
                        {column["name"] for column in inspector.get_columns(table)},
                        {str(index["name"]) for index in inspector.get_indexes(table)},
                    )
    finally:
        sync_eng.dispose()
    return found


def test_upgrade_creates_the_table_with_the_model_columns(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)

    columns, _ = _schema(sync_url)["agent_session_event"]
    assert columns == set(AgentSessionEvent.__table__.columns.keys())


def test_downgrade_drops_the_table(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert _schema(sync_url) == {}


def test_upgrade_round_trips(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)
    assert set(_schema(sync_url)) == {"agent_session_event"}

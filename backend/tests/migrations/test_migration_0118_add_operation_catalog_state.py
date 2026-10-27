# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0118_add_operation_catalog_state`` (#5019).

* ``upgrade 0118`` adds the empty one-row table ``operation_catalog_state``.
* The table takes only the row ``id = 1``.
* ``downgrade 0117`` drops it, and ``upgrade 0118`` adds it again.

Every step targets this migration's own revision and its parent, never
``head``, so a later migration cannot break these tests.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0118"
_DOWN_REVISION = "0117"
_TABLE = "operation_catalog_state"
_INSERT = (
    f"INSERT INTO {_TABLE} (id, written_by_release, written_at) "
    "VALUES (:id, 'abc123', CURRENT_TIMESTAMP)"
)


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0118.db"
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


def _tables(sync_url: str) -> set[str]:
    eng = sa_create_engine(sync_url)
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def _insert(sync_url: str, row_id: int) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text(_INSERT), {"id": row_id})
    finally:
        eng.dispose()


def test_upgrade_adds_empty_table(alembic_cfg: tuple[Config, str]) -> None:
    """The table exists after the upgrade and has no row until a start writes one."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _TABLE in _tables(sync_url)
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            assert conn.execute(text(f"SELECT COUNT(*) FROM {_TABLE}")).scalar_one() == 0
    finally:
        eng.dispose()


def test_table_takes_only_row_one(alembic_cfg: tuple[Config, str]) -> None:
    """The check allows the row ``id = 1`` and refuses any other id."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    _insert(sync_url, 1)
    with pytest.raises(IntegrityError):
        _insert(sync_url, 2)


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0117`` drops the table; ``upgrade 0118`` adds it again."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert _TABLE not in _tables(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _TABLE in _tables(sync_url)

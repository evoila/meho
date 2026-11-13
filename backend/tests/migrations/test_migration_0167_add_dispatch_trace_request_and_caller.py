# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0167_add_dispatch_trace_request_and_caller``.

* The upgrade adds exactly two columns, ``dispatch_trace.request_id`` and
  ``dispatch_trace.operator_sub``, and both are nullable. No other table or
  column changes.
* The downgrade drops them again.
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
from meho_backplane.settings import get_settings

_REVISION = "0167"
_DOWN_REVISION = "0166"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
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


def _schema(sync_url: str) -> dict[str, dict[str, bool]]:
    eng = sa_create_engine(sync_url)
    try:
        inspector = inspect(eng)
        return {
            table: {col["name"]: bool(col["nullable"]) for col in inspector.get_columns(table)}
            for table in inspector.get_table_names()
        }
    finally:
        eng.dispose()


def test_upgrade_adds_only_two_nullable_columns(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    before = _schema(sync_url)

    command.upgrade(cfg, _REVISION)
    after = _schema(sync_url)

    assert set(after) == set(before)
    for table, columns in before.items():
        if table != "dispatch_trace":
            assert after[table] == columns, table
    added = set(after["dispatch_trace"]) - set(before["dispatch_trace"])
    assert added == {"request_id", "operator_sub"}
    assert after["dispatch_trace"]["request_id"] is True
    assert after["dispatch_trace"]["operator_sub"] is True


def test_downgrade_drops_the_columns(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    columns = _schema(sync_url)["dispatch_trace"]
    assert "request_id" not in columns
    assert "operator_sub" not in columns

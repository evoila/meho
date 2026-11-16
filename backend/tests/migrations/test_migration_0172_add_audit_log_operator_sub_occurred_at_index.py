# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0172_add_audit_log_operator_sub_occurred_at_index``.

* The upgrade adds one index on ``audit_log``: ``(operator_sub,
  occurred_at)``. No column changes.
* The downgrade drops the index again.
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

_REVISION = "0172"
_DOWN_REVISION = "0171"
_INDEX = "audit_log_operator_sub_occurred_at_idx"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0172.db"
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


def _audit_log(sync_url: str) -> tuple[list[str], dict[str, list[str | None]]]:
    eng = sa_create_engine(sync_url)
    try:
        inspector = inspect(eng)
        columns = [col["name"] for col in inspector.get_columns("audit_log")]
        indexes = {
            ix["name"]: list(ix["column_names"]) for ix in inspector.get_indexes("audit_log")
        }
        return columns, indexes
    finally:
        eng.dispose()


def test_upgrade_adds_the_person_and_time_index(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    columns_before, indexes_before = _audit_log(sync_url)
    assert _INDEX not in indexes_before

    command.upgrade(cfg, _REVISION)
    columns_after, indexes_after = _audit_log(sync_url)

    assert columns_after == columns_before
    assert set(indexes_after) - set(indexes_before) == {_INDEX}
    assert indexes_after[_INDEX] == ["operator_sub", "occurred_at"]


def test_downgrade_drops_the_index(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert _INDEX not in _audit_log(sync_url)[1]

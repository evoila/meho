# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0189_add_runner_enroll_refusal``.

The migration adds the ``runner_enroll_refusal`` table. The tests check that
``upgrade`` adds it with the model's columns and key, that ``downgrade``
removes it, and that the pair round-trips. SQLite is the test driver.
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
from meho_backplane.db.models import RunnerEnrollRefusal
from meho_backplane.settings import get_settings

_REVISION = "0189"
_DOWN_REVISION = "0188"
_TABLE = "runner_enroll_refusal"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0189.db"
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


def _table(sync_url: str) -> tuple[set[str], list[str], list[str]] | None:
    """Return ``(columns, primary key, foreign key targets)``, or ``None``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            inspector = inspect(conn)
            if _TABLE not in inspector.get_table_names():
                return None
            columns = {column["name"] for column in inspector.get_columns(_TABLE)}
            key = list(inspector.get_pk_constraint(_TABLE)["constrained_columns"])
            targets = [fk["referred_table"] for fk in inspector.get_foreign_keys(_TABLE)]
            return columns, key, targets
    finally:
        sync_eng.dispose()


def test_upgrade_adds_the_table(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)

    table = _table(sync_url)
    assert table is not None
    columns, key, targets = table
    assert columns == set(RunnerEnrollRefusal.__table__.columns.keys())
    assert key == ["runner_id", "reason"]
    assert targets == ["runner_principal"]


def test_downgrade_removes_the_table(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    assert _table(sync_url) is None


def test_upgrade_round_trips(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    assert _table(sync_url) is not None

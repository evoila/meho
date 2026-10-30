# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0104_add_service_principal_grant_revoked_reason``.

#3489 adds one nullable ``revoked_reason`` column to
``service_principal_grant``, so a revoke can store an optional reason. The
migration is a plain nullable ``ADD COLUMN``, so the tests check schema
shape: the column is there and nullable after ``upgrade``, gone after
``downgrade``, and the pair round-trips. SQLite is the test driver; the
migration uses only generic ``ADD COLUMN`` / ``DROP COLUMN`` DDL.
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

_REVISION = "0104"
_DOWN_REVISION = "0103"
_TABLE = "service_principal_grant"
_COLUMN = "revoked_reason"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0104.db"
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


def _column_specs(sync_url: str) -> dict[str, dict[str, object]]:
    """Return the ``service_principal_grant`` columns keyed by name."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            columns = inspect(conn).get_columns(_TABLE)
    finally:
        sync_eng.dispose()
    return {column["name"]: column for column in columns}


def test_upgrade_adds_nullable_revoked_reason(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    """``upgrade 0104`` adds ``revoked_reason`` as a nullable column."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)

    specs = _column_specs(sync_url)
    assert _COLUMN in specs
    # Nullable: live rows, older rows, and revokes without a reason carry NULL.
    assert specs[_COLUMN]["nullable"] is True


def test_downgrade_drops_revoked_reason(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    """``downgrade 0103`` drops ``revoked_reason``."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    assert _COLUMN in _column_specs(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _COLUMN not in _column_specs(sync_url)


def test_upgrade_round_trips_after_clean_downgrade(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    """``upgrade 0104`` → ``downgrade 0103`` → ``upgrade 0104`` restores the column."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    assert _COLUMN in _column_specs(sync_url)

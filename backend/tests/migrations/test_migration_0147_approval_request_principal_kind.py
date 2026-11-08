# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0147_add_approval_request_principal_kind``.

The migration adds the nullable ``principal_kind`` column to
``approval_request`` (#3885). The tests check that:

* the column does not exist before the migration and does after it;
* the downgrade drops it again.

Every step targets this revision and its ``down_revision``, never
``head``. SQLite is the test driver.
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

_REVISION = "0147"
_DOWN_REVISION = "0146"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0147.db"
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


def _column(sync_url: str) -> dict[str, object] | None:
    eng = sa_create_engine(sync_url)
    try:
        for col in inspect(eng).get_columns("approval_request"):
            if col["name"] == "principal_kind":
                return dict(col)
        return None
    finally:
        eng.dispose()


def test_upgrade_adds_a_nullable_column(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    assert _column(sync_url) is None

    command.upgrade(cfg, _REVISION)

    column = _column(sync_url)
    assert column is not None
    assert column["nullable"] is True


def test_downgrade_drops_the_column(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _DOWN_REVISION)

    assert _column(sync_url) is None

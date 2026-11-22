# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0182_add_principal_block``.

* The upgrade adds the ``principal_block`` table. Its key is ``(sub, scope)``.
* The downgrade drops the table again.
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

_REVISION = "0182"
_DOWN_REVISION = "0181"
_TABLE = "principal_block"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0182.db"
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


def _tables(sync_url: str) -> set[str]:
    eng = sa_create_engine(sync_url)
    try:
        return set(inspect(eng).get_table_names())
    finally:
        eng.dispose()


def _primary_key(sync_url: str) -> list[str]:
    eng = sa_create_engine(sync_url)
    try:
        return list(inspect(eng).get_pk_constraint(_TABLE)["constrained_columns"])
    finally:
        eng.dispose()


def test_upgrade_adds_the_table(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    assert _TABLE not in _tables(sync_url)

    command.upgrade(cfg, _REVISION)

    assert _TABLE in _tables(sync_url)
    assert sorted(_primary_key(sync_url)) == ["scope", "sub"]


def test_downgrade_drops_the_table(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert _TABLE not in _tables(sync_url)

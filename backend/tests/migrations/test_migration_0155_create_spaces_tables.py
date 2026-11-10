# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0155_create_spaces_tables``.

The migration creates ``space``, ``space_member`` and ``resource_assignment``.
``downgrade()`` drops them. Every step targets this migration's own revision
(``0155``) and its ``down_revision`` (``0154``), never ``head``.
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

_REVISION = "0155"
_DOWN_REVISION = "0154"
_TABLES = {"space", "space_member", "resource_assignment"}


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0155.db"
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


def _inspect(sync_url: str) -> tuple[set[str], dict[str, set[str]]]:
    engine = sa_create_engine(sync_url)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        indexes = {t: {i["name"] for i in insp.get_indexes(t)} for t in _TABLES & tables}
        return tables, indexes
    finally:
        engine.dispose()


def test_up_creates_the_tables_and_down_drops_them(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    assert not _TABLES & _inspect(sync_url)[0]

    command.upgrade(cfg, _REVISION)
    tables, indexes = _inspect(sync_url)
    assert tables >= _TABLES
    assert "space_tenant_name_idx" in indexes["space"]
    assert "space_member_open_idx" in indexes["space_member"]
    assert "resource_assignment_tenant_target_idx" in indexes["resource_assignment"]

    command.downgrade(cfg, _DOWN_REVISION)
    assert not _TABLES & _inspect(sync_url)[0]

    command.upgrade(cfg, _REVISION)
    assert _inspect(sync_url)[0] >= _TABLES

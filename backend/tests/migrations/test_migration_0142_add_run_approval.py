# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0142_add_run_approval`` (#4126).

The migration adds the ``run_approval`` table and the ``tenant`` setting
``run_approval_target_kinds_enabled``. These tests pin: a tenant written
before the upgrade reads ``false`` (target kinds stay off), the table
exists, and ``downgrade()`` drops both.

Every step targets this migration's own revision (``0142``) and its
``down_revision`` (``0141``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0142"
_DOWN_REVISION = "0141"
_TENANT_ID = "a" * 32


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0142.db"
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


def _tables_and_tenant_columns(sync_url: str) -> tuple[set[str], set[str]]:
    engine = sa_create_engine(sync_url)
    try:
        inspector = inspect(engine)
        tenant = {column["name"] for column in inspector.get_columns("tenant")}
        return set(inspector.get_table_names()), tenant
    finally:
        engine.dispose()


def test_old_tenant_keeps_target_kinds_off(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, 'tenant-a', 'Tenant A', '2026-11-07 00:00:00')"
                ),
                {"id": _TENANT_ID},
            )
        command.upgrade(cfg, _REVISION)
        with engine.begin() as conn:
            enabled = conn.execute(
                text("SELECT run_approval_target_kinds_enabled FROM tenant WHERE id = :id"),
                {"id": _TENANT_ID},
            ).scalar_one()
    finally:
        engine.dispose()
    assert not enabled
    tables, _ = _tables_and_tenant_columns(sync_url)
    assert "run_approval" in tables


def test_downgrade_drops_table_and_setting(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    tables, tenant_columns = _tables_and_tenant_columns(sync_url)
    assert "run_approval" not in tables
    assert "run_approval_target_kinds_enabled" not in tenant_columns

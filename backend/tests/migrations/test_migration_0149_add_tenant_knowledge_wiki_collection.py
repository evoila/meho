# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0149_add_tenant_knowledge_wiki_collection`` (#5976).

The migration adds the nullable ``tenant.knowledge_wiki_collection``
column. A tenant written before the upgrade reads ``NULL`` (lessons stay
in MEHO only), and ``downgrade()`` drops the column again.

Every step targets this migration's own revision (``0149``) and its
``down_revision`` (``0148``), never ``head``.
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

_REVISION = "0149"
_DOWN_REVISION = "0148"
_TENANT_ID = "a" * 32


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0149.db"
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


def _tenant_columns(sync_url: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        return {column["name"] for column in inspect(engine).get_columns("tenant")}
    finally:
        engine.dispose()


def test_old_tenant_reads_null(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, 'tenant-a', 'Tenant A', '2026-11-08 00:00:00')"
                ),
                {"id": _TENANT_ID},
            )
        command.upgrade(cfg, _REVISION)
        with engine.begin() as conn:
            value = conn.execute(
                text("SELECT knowledge_wiki_collection FROM tenant WHERE id = :id"),
                {"id": _TENANT_ID},
            ).scalar_one()
    finally:
        engine.dispose()
    assert value is None


def test_downgrade_drops_the_column(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert "knowledge_wiki_collection" in _tenant_columns(sync_url)
    command.downgrade(cfg, _DOWN_REVISION)
    assert "knowledge_wiki_collection" not in _tenant_columns(sync_url)

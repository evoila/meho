# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0129_add_web_session_tenant_choice`` (#5240).

The migration adds two boolean columns to ``web_session``. These tests
pin: a row written before the upgrade gets ``tenant_chosen = true`` and
``can_switch_tenant = false`` (it was made for one tenant, as before),
and ``downgrade()`` drops both columns.

Every step targets this migration's own revision (``0129``) and its
``down_revision`` (``0128``), never ``head``.
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

_REVISION = "0129"
_DOWN_REVISION = "0128"
_COLUMNS = {"tenant_chosen", "can_switch_tenant"}


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0129.db"
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


def _columns(sync_url: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        return {column["name"] for column in inspect(engine).get_columns("web_session")}
    finally:
        engine.dispose()


def test_existing_row_keeps_working_after_upgrade(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO web_session (id, operator_sub, tenant_id, created_at, "
                    "expires_at, access_token, refresh_token, last_seen_at) VALUES "
                    "(:id, 'person', :tenant, '2026-10-31 00:00:00', '2026-10-31 01:00:00', "
                    "x'00', x'00', '2026-10-31 00:00:00')"
                ),
                {"id": "1" * 32, "tenant": "a" * 32},
            )
        command.upgrade(cfg, _REVISION)
        with engine.connect() as conn:
            chosen, can_switch = conn.execute(
                text("SELECT tenant_chosen, can_switch_tenant FROM web_session")
            ).one()
    finally:
        engine.dispose()
    assert bool(chosen) is True
    assert bool(can_switch) is False


def test_downgrade_drops_the_columns(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url) >= _COLUMNS
    command.downgrade(cfg, _DOWN_REVISION)
    assert not (_COLUMNS & _columns(sync_url))

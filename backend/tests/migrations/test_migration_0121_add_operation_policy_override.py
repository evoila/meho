# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0121_add_operation_policy_override`` (#4711).

The migration adds one table for the operator's stricter risk levels and
approval flags. These tests pin: the table exists after upgrade, the two
partial unique indexes keep one row per scope and operation name (one
install-wide row, one row per tenant), the risk level check refuses an
unknown value, and ``downgrade()`` drops the table.

Every step targets this migration's own revision (``0121``) and its
``down_revision`` (``0120``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0121"
_DOWN_REVISION = "0120"
_TABLE = "operation_policy_override"
_INSERT = text(
    "INSERT INTO operation_policy_override "
    "(id, tenant_id, product, version, impl_id, op_id, safety_level, requires_approval, "
    "reason, set_by_sub, set_at) VALUES "
    "(:id, :tenant_id, 'vault', '1.x', 'vault', 'vault.kv.write', :level, NULL, NULL, "
    "'admin', '2026-10-28 00:00:00')"
)


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0121.db"
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


def _insert(sync_url: str, *, row_id: str, tenant_id: str | None, level: str = "dangerous") -> None:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(_INSERT, {"id": row_id, "tenant_id": tenant_id, "level": level})
    finally:
        engine.dispose()


def _has_table(sync_url: str) -> bool:
    engine = sa_create_engine(sync_url)
    try:
        return _TABLE in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_upgrade_creates_table_with_one_row_per_scope(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _has_table(sync_url)

    tenant = "a" * 32
    _insert(sync_url, row_id="1" * 32, tenant_id=None)
    _insert(sync_url, row_id="2" * 32, tenant_id=tenant)
    with pytest.raises(IntegrityError):
        _insert(sync_url, row_id="3" * 32, tenant_id=None)
    with pytest.raises(IntegrityError):
        _insert(sync_url, row_id="4" * 32, tenant_id=tenant)
    with pytest.raises(IntegrityError):
        _insert(sync_url, row_id="5" * 32, tenant_id="b" * 32, level="extreme")


def test_downgrade_drops_table(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert not _has_table(sync_url)
    command.upgrade(cfg, _REVISION)
    assert _has_table(sync_url)

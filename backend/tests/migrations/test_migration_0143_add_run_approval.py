# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0143_add_run_approval`` (#4126).

The migration adds the ``run_approval`` table and the ``tenant`` setting
``run_approval_target_kinds_enabled``. These tests pin: a tenant written
before the upgrade reads ``false`` (target kinds stay off), the table
exists, the unique index lets one run hold at most one open row, and
``downgrade()`` drops both.

Every step targets this migration's own revision (``0143``) and its
``down_revision`` (``0142``), never ``head``.
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

_REVISION = "0143"
_DOWN_REVISION = "0142"
_TENANT_ID = "a" * 32


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0143.db"
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


def _indexes(sync_url: str) -> dict[str, bool]:
    """Index name -> unique, for the ``run_approval`` table."""
    engine = sa_create_engine(sync_url)
    try:
        return {
            str(index["name"]): bool(index["unique"])
            for index in inspect(engine).get_indexes("run_approval")
        }
    finally:
        engine.dispose()


def test_one_open_row_per_run(alembic_cfg: tuple[Config, str]) -> None:
    """The unique index refuses a second open row of one run; a closed row frees the slot."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    insert = text(
        "INSERT INTO run_approval (id, tenant_id, principal_sub, work_ref, entries, reason, "
        "requested_at, expires_at) VALUES (:id, :tenant, 'svc', 'run-1', '[]', 'r', "
        "'2026-11-07 00:00:00', '2026-11-08 00:00:00')"
    )
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
            conn.execute(insert, {"id": "1" * 32, "tenant": _TENANT_ID})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"id": "2" * 32, "tenant": _TENANT_ID})
        # Each way a row closes frees the slot for the next request.
        for previous, column, new_id in (
            ("1" * 32, "expired_at", "2" * 32),
            ("2" * 32, "ended_at", "3" * 32),
            ("3" * 32, "revoked_at", "4" * 32),
        ):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f"UPDATE run_approval SET {column} = '2026-11-07 01:00:00' WHERE id = :id"
                    ),
                    {"id": previous},
                )
                conn.execute(insert, {"id": new_id, "tenant": _TENANT_ID})
        with engine.begin() as conn:
            open_rows = conn.execute(
                text(
                    "SELECT count(*) FROM run_approval WHERE revoked_at IS NULL "
                    "AND ended_at IS NULL AND expired_at IS NULL"
                )
            ).scalar_one()
    finally:
        engine.dispose()
    assert open_rows == 1
    assert _indexes(sync_url)["run_approval_open_run_idx"] is True


def test_downgrade_drops_table_and_setting(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    tables, tenant_columns = _tables_and_tenant_columns(sync_url)
    assert "run_approval" not in tables
    assert "run_approval_target_kinds_enabled" not in tenant_columns

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0169_add_targets_connection_changed_at``.

The migration adds one nullable column to ``targets`` and fills it from
``updated_at`` for the rows that exist. ``downgrade()`` removes it and
keeps the rows.

Every step targets this migration's own revision (``0169``) and its
``down_revision`` (``0168``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0169"
_DOWN_REVISION = "0168"
_TARGET_ID = "a" * 32
_TENANT_ID = "b" * 32
_UPDATED_AT = "2026-11-15 12:00:00.000000"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0169.db"
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
        return {c["name"] for c in inspect(engine).get_columns("targets")}
    finally:
        engine.dispose()


def _run(sync_url: str, sql: str, **params: str) -> list[tuple[object, ...]]:
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(text(sql), params)
            return [tuple(row) for row in result] if result.returns_rows else []
    finally:
        engine.dispose()


def _insert_target(sync_url: str) -> None:
    _run(
        sync_url,
        "INSERT INTO targets (id, tenant_id, name, aliases, product, host, auth_model, "
        "vpn_required, verify_tls, extras, created_at, updated_at) VALUES (:id, :tenant, "
        "'t1', '[]', 'demo', 'demo.example', 'shared_service_account', 0, 1, '{}', "
        ":ts, :ts)",
        id=_TARGET_ID,
        tenant=_TENANT_ID,
        ts=_UPDATED_AT,
    )


def test_old_rows_get_the_value_of_updated_at(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_target(sync_url)

    command.upgrade(cfg, _REVISION)

    assert _run(sync_url, "SELECT connection_changed_at = updated_at FROM targets") == [(1,)]


def test_up_and_down_keep_the_rows(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _insert_target(sync_url)

    command.upgrade(cfg, _REVISION)
    assert "connection_changed_at" in _columns(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert "connection_changed_at" not in _columns(sync_url)
    assert _run(sync_url, "SELECT name FROM targets") == [("t1",)]

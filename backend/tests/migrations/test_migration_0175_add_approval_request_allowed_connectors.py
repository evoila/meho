# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0175_add_approval_request_allowed_connectors``.

The migration adds one nullable column to ``approval_request``. Rows that
exist already keep ``NULL``. ``downgrade()`` removes the column again.
Every step targets this migration's own revision (``0175``) and its
``down_revision`` (``0174``), never ``head``.
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

_REVISION = "0175"
_DOWN_REVISION = "0174"
_COLUMN = "allowed_connectors"
_REQUEST_ID = "00000000-0000-0000-0000-000000000175"
_TENANT_ID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0175.db"
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


def _columns(sync_url: str) -> dict[str, bool]:
    engine = sa_create_engine(sync_url)
    try:
        return {c["name"]: c["nullable"] for c in inspect(engine).get_columns("approval_request")}
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


def test_up_adds_a_nullable_column_and_down_drops_it(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    assert _COLUMN not in _columns(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url)[_COLUMN] is True

    command.downgrade(cfg, _DOWN_REVISION)
    assert _COLUMN not in _columns(sync_url)


def test_a_row_from_before_keeps_null(
    alembic_cfg: tuple[Config, str], copy_migrated_db: Callable[[Config, str], None]
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    _run(
        sync_url,
        "INSERT INTO approval_request (id, tenant_id, principal_sub, op_id, connector_id, "
        "params_hash, proposed_effect, status, created_at) "
        "VALUES (:id, :tenant, 'fixture', 'x.y', 'x-1.x', 'h', '{}', 'pending', "
        "'2026-11-17 00:00:00')",
        id=_REQUEST_ID,
        tenant=_TENANT_ID,
    )

    command.upgrade(cfg, _REVISION)

    assert _run(sync_url, "SELECT id, allowed_connectors FROM approval_request") == [
        (_REQUEST_ID, None)
    ]

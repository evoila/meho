# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0121_create_ingested_auth_profile`` (#5016).

The table saves the login type an operator chose for an ingested
connector. One row per ``(tenant_id, product, version, impl_id)``, kept by
two partial unique indexes (built-in rows have ``tenant_id IS NULL``).

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
_TABLE = "ingested_auth_profile"
_TENANT_A = "11111111111111111111111111111111"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0121.db"
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


def _has_table(sync_url: str) -> bool:
    eng = sa_create_engine(sync_url)
    try:
        return _TABLE in inspect(eng).get_table_names()
    finally:
        eng.dispose()


def _insert(sync_url: str, *, row_id: str, tenant_id: str | None) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {_TABLE} (id, tenant_id, product, version, impl_id, "
                    "auth_scheme, secret_fields, operator_sub, updated_at) VALUES "
                    "(:id, :tenant_id, 'widgetco', '3.1', 'widgetco-rest', 'basic', "
                    "'[\"username\", \"password\"]', 'test-operator', '2026-10-27 00:00:00')"
                ),
                {"id": row_id, "tenant_id": tenant_id},
            )
    finally:
        eng.dispose()


def test_upgrade_creates_table(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _has_table(sync_url)


def test_one_built_in_row_per_connector(alembic_cfg: tuple[Config, str]) -> None:
    """Two built-in rows for the same connector collide (NULL tenant)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    _insert(sync_url, row_id="a" * 32, tenant_id=None)
    with pytest.raises(IntegrityError):
        _insert(sync_url, row_id="b" * 32, tenant_id=None)


def test_one_tenant_row_per_connector(alembic_cfg: tuple[Config, str]) -> None:
    """A tenant row may sit next to the built-in row, but not twice."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    _insert(sync_url, row_id="a" * 32, tenant_id=None)
    _insert(sync_url, row_id="b" * 32, tenant_id=_TENANT_A)
    with pytest.raises(IntegrityError):
        _insert(sync_url, row_id="c" * 32, tenant_id=_TENANT_A)


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _DOWN_REVISION)
    assert not _has_table(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _has_table(sync_url)

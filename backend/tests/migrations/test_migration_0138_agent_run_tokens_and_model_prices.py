# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0138_add_agent_run_tokens_and_model_prices`` (#5853).

The migration adds four nullable token columns to ``agent_run`` and the
nullable ``agent_model_prices`` column to ``tenant``. These tests pin: rows
written before the upgrade read ``NULL`` in the new columns, the new columns
take values, and ``downgrade()`` drops all five.

Every step targets this migration's own revision (``0138``) and its
``down_revision`` (``0137``), never ``head``.
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

_REVISION = "0138"
_DOWN_REVISION = "0137"
_TOKEN_COLUMNS = {"input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"}
_TENANT_ID = "a" * 32
_RUN_ID = "1" * 32


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    db_path = tmp_path / "migration_0138.db"
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


def _columns(sync_url: str, table: str) -> set[str]:
    engine = sa_create_engine(sync_url)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def test_old_rows_read_null_and_new_columns_take_values(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    engine = sa_create_engine(sync_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, 'tenant-a', 'Tenant A', '2026-11-06 00:00:00')"
                ),
                {"id": _TENANT_ID},
            )
            conn.execute(
                text(
                    "INSERT INTO agent_run (id, tenant_id, identity_sub, trigger, model_tier, "
                    "status, turns, created_at) VALUES (:id, :tenant, 'person', 'direct', "
                    "'standard', 'succeeded', 1, '2026-11-06 00:00:00')"
                ),
                {"id": _RUN_ID, "tenant": _TENANT_ID},
            )
        command.upgrade(cfg, _REVISION)
        with engine.begin() as conn:
            old = conn.execute(
                text(
                    "SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens "
                    "FROM agent_run"
                )
            ).one()
            prices = conn.execute(
                text("SELECT agent_model_prices FROM tenant WHERE id = :id"), {"id": _TENANT_ID}
            ).scalar_one()
            conn.execute(
                text(
                    "UPDATE agent_run SET input_tokens = 5000000000, output_tokens = 7, "
                    "cache_read_tokens = 0, cache_write_tokens = 0"
                )
            )
            conn.execute(
                text(
                    "UPDATE tenant SET agent_model_prices = "
                    """'{"openai:m": {"input": "0.4", "output": "1.6"}}' WHERE id = :id"""
                ),
                {"id": _TENANT_ID},
            )
            new = conn.execute(text("SELECT input_tokens, output_tokens FROM agent_run")).one()
    finally:
        engine.dispose()
    assert tuple(old) == (None, None, None, None)
    assert prices is None
    # BIGINT: a count above 2**31 fits.
    assert tuple(new) == (5_000_000_000, 7)


def test_downgrade_drops_the_columns(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url, "agent_run") >= _TOKEN_COLUMNS
    assert "agent_model_prices" in _columns(sync_url, "tenant")
    command.downgrade(cfg, _DOWN_REVISION)
    assert not (_TOKEN_COLUMNS & _columns(sync_url, "agent_run"))
    assert "agent_model_prices" not in _columns(sync_url, "tenant")

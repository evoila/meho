# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0120_add_agent_announcement_principal_index`` (#5005).

The migration adds ``agent_announcement_tenant_principal_created_at_idx``
for the caller's active-claim lookup. The tests check that ``upgrade``
creates it with the columns in filter order and that ``downgrade`` drops
it and leaves the older indexes alone. Every step targets this revision
and its ``down_revision``, never ``head``. The PostgreSQL plan check lives
in ``tests/integration/test_announce_claim_lookup_pg.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0120"
_DOWN_REVISION = "0119"
_TABLE = "agent_announcement"
_INDEX = "agent_announcement_tenant_principal_created_at_idx"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0120.db"
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


def _indexes(sync_url: str) -> dict[str, dict[str, Any]]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return {str(ix["name"]): dict(ix) for ix in inspect(conn).get_indexes(_TABLE)}
    finally:
        eng.dispose()


def test_upgrade_creates_the_index_in_filter_order(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    assert _INDEX not in _indexes(sync_url)

    command.upgrade(cfg, _REVISION)
    index = _indexes(sync_url)[_INDEX]
    assert index["column_names"] == ["tenant_id", "principal_sub", "created_at"]
    assert not index["unique"]


def test_downgrade_drops_only_this_index(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    before = set(_indexes(sync_url))
    command.upgrade(cfg, _REVISION)

    command.downgrade(cfg, _DOWN_REVISION)
    assert set(_indexes(sync_url)) == before

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0087_add_tenant_flight_recorder_agent_readable``.

#3216 flight-recorder agent read gate (F5). Additive-only: one nullable
``ADD COLUMN`` (``tenant.flight_recorder_agent_readable``, a tri-state
NULL/True/False override) -- no backfill, no ALTER of an existing column.

Idempotency pinning: every forward / round-trip step targets this migration's
own revision (``0087``) and its ``down_revision`` (``0086``), never ``head`` --
so a future head migration cannot make ``upgrade("head")`` re-run this DDL on a
schema that already has it. SQLite is the test driver; the migration uses only
generic ``ADD COLUMN`` / ``DROP COLUMN`` DDL so PG parity holds.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0087"
_DOWN_REVISION = "0086"
_NEW_COLUMN = "flight_recorder_agent_readable"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0087.db"
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
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def test_upgrade_adds_agent_read_gate_column(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0087`` adds the nullable tri-state agent-read gate column."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _NEW_COLUMN in _columns(sync_url, "tenant")


def test_column_is_nullable_tristate(alembic_cfg: tuple[Config, str]) -> None:
    """The column is nullable (NULL = inherit) so the add-column needs no backfill."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            info = {
                str(row[1]): row for row in conn.execute(text("PRAGMA table_info(tenant)")).all()
            }
    finally:
        sync_eng.dispose()
    # PRAGMA table_info row: (cid, name, type, notnull, dflt_value, pk).
    assert info[_NEW_COLUMN][3] == 0  # notnull == 0 => nullable


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0086`` drops the column; ``upgrade 0087`` restores it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _NEW_COLUMN in _columns(sync_url, "tenant")

    command.downgrade(cfg, _DOWN_REVISION)
    assert _NEW_COLUMN not in _columns(sync_url, "tenant")

    command.upgrade(cfg, _REVISION)
    assert _NEW_COLUMN in _columns(sync_url, "tenant")

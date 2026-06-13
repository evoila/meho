# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0041_add_agent_run_work_ref``.

Initiative #1654, Task #1662 (work_ref I3-T2). Adds the nullable
``agent_run.work_ref`` column + its composite ``(tenant_id, work_ref)``
b-tree index -- the durable change-ticket reference an agent run works
under, set at create time and filterable on the agent-run list. Soft-column
discipline mirrors 0039 / 0040: nullable, no server default (Python-side
``None``), reversible.

SQLite is the test driver and the migration uses only generic DDL, so PG
parity holds.
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


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0041.db"
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


def _agent_run_columns(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(agent_run)")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _agent_run_column_is_nullable(sync_url: str, column: str) -> bool:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(agent_run)")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            # PRAGMA table_info column 3 is ``notnull`` (0 == nullable).
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on agent_run")


def _agent_run_indexes(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA index_list(agent_run)")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def test_upgrade_adds_work_ref_column_and_index(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade head`` lands the nullable column + its composite index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert "work_ref" in _agent_run_columns(sync_url), (
        "migration 0041 must add agent_run.work_ref on upgrade head"
    )
    assert _agent_run_column_is_nullable(sync_url, "work_ref"), (
        "work_ref must be nullable -- NULL when the run carries no change ticket"
    )
    assert "agent_run_tenant_work_ref_idx" in _agent_run_indexes(sync_url), (
        "migration 0041 must create agent_run_tenant_work_ref_idx on upgrade head"
    )


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0040"`` drops column + index; ``upgrade head`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "work_ref" in _agent_run_columns(sync_url)
    assert "agent_run_tenant_work_ref_idx" in _agent_run_indexes(sync_url)

    command.downgrade(cfg, "0040")
    assert "work_ref" not in _agent_run_columns(sync_url), "downgrade must drop agent_run.work_ref"
    assert "agent_run_tenant_work_ref_idx" not in _agent_run_indexes(sync_url), (
        "downgrade must drop agent_run_tenant_work_ref_idx"
    )

    command.upgrade(cfg, "head")
    assert "work_ref" in _agent_run_columns(sync_url)
    assert "agent_run_tenant_work_ref_idx" in _agent_run_indexes(sync_url)

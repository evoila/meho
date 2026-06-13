# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0040_add_runbook_runs_work_ref``.

Initiative #1654, Task #1661 (work_ref I3-T1). Adds the nullable
``runbook_runs.work_ref`` column + its composite ``(tenant_id, work_ref)``
b-tree index -- the durable change-ticket reference a runbook run executes
under, pinned at start and inherited by each step's audit row. Soft-column
discipline mirrors 0039: nullable, no server default (Python-side
``None``), reversible.

Mirrors :mod:`tests.test_migration_0039_work_ref`; SQLite is the test
driver and the migration uses only generic DDL, so PG parity holds.
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
    db_path = tmp_path / "migration_0040.db"
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


def _runbook_runs_columns(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(runbook_runs)")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _runbook_runs_column_is_nullable(sync_url: str, column: str) -> bool:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(runbook_runs)")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on runbook_runs")


def _runbook_runs_indexes(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA index_list(runbook_runs)")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def test_upgrade_adds_work_ref_column_and_index(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade head`` lands the nullable column + its named index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert "work_ref" in _runbook_runs_columns(sync_url), (
        "migration 0040 must add runbook_runs.work_ref on upgrade head"
    )
    assert _runbook_runs_column_is_nullable(sync_url, "work_ref"), (
        "work_ref must be nullable -- NULL when the run carries no change ticket"
    )
    assert "runbook_runs_tenant_work_ref_idx" in _runbook_runs_indexes(sync_url), (
        "migration 0040 must create runbook_runs_tenant_work_ref_idx on upgrade head"
    )


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0039"`` drops column + index; ``upgrade head`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "work_ref" in _runbook_runs_columns(sync_url)
    assert "runbook_runs_tenant_work_ref_idx" in _runbook_runs_indexes(sync_url)

    command.downgrade(cfg, "0039")
    assert "work_ref" not in _runbook_runs_columns(sync_url), "downgrade must drop work_ref"
    assert "runbook_runs_tenant_work_ref_idx" not in _runbook_runs_indexes(sync_url), (
        "downgrade must drop runbook_runs_tenant_work_ref_idx"
    )

    command.upgrade(cfg, "head")
    assert "work_ref" in _runbook_runs_columns(sync_url)
    assert "runbook_runs_tenant_work_ref_idx" in _runbook_runs_indexes(sync_url)


def test_prior_runbook_runs_columns_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """The pre-existing runbook_runs columns + indexes survive 0040."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _runbook_runs_columns(sync_url)
    assert "assigned_to" in columns, "the assigned_to column must survive 0040"
    assert "template_slug" in columns, "the template_slug column must survive 0040"
    assert "state" in columns, "the state column must survive 0040"
    assert "work_ref" in columns

    indexes = _runbook_runs_indexes(sync_url)
    assert "runbook_runs_tenant_assigned_state_idx" in indexes
    assert "runbook_runs_tenant_template_idx" in indexes
    assert "runbook_runs_tenant_work_ref_idx" in indexes


def test_orm_field_resolves_and_defaults_none() -> None:
    """:attr:`RunbookRun.work_ref` resolves and defaults to ``None``."""
    import uuid

    from meho_backplane.db.models import RunbookRun

    assert hasattr(RunbookRun, "work_ref")
    row = RunbookRun(
        tenant_id=uuid.uuid4(),
        template_slug="d",
        template_version=1,
        assigned_to="op-smoke",
        target="n",
        started_by="op-smoke",
    )
    assert row.work_ref is None, "a RunbookRun built without work_ref must default it to None"

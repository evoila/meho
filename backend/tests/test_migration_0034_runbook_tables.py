# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0034_runbook_tables_and_audit_correlation``.

Task #1292 (G12.1-T1) under Initiative #1196. Adds three new tables
(``runbook_templates``, ``runbook_runs``, ``runbook_run_step_states``)
and two additive nullable columns on ``audit_log``
(``run_id``, ``step_id``) plus one new index
(``audit_log_run_id_idx``). Reversible; soft-column discipline mirrors
``0014`` / ``0021`` / ``0030`` for the audit columns; new-table
discipline mirrors ``0027`` for the three runbook tables.

Test matrix
-----------

1. Upgrade succeeds; all three new tables present; ``audit_log.run_id``
   + ``audit_log.step_id`` present and nullable; all four new indexes
   present.
2. Upgrade → downgrade ``0033`` → upgrade round-trip: tables dropped on
   downgrade, columns removed, all four indexes gone; upgrade restores
   them.
3. Inserting an ``audit_log`` row without ``run_id`` / ``step_id`` works;
   both columns are NULL on that row (no regression on pre-G12.1 rows).
4. Inserting a ``runbook_run_step_states`` row referencing a nonexistent
   ``run_id`` raises ``IntegrityError`` (the FK is a real constraint).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0034.db"
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


def _table_names(sync_url: str) -> set[str]:
    """Return all table names present in the database."""
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    finally:
        eng.dispose()
    return {str(row[0]) for row in rows}


def _table_columns(sync_url: str, table: str) -> set[str]:
    """Return column names for *table* via SQLite PRAGMA."""
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        eng.dispose()
    return {str(row[1]) for row in rows}


def _column_is_nullable(sync_url: str, table: str, column: str) -> bool:
    """Return True if *column* on *table* allows NULL values."""
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            # PRAGMA table_info column 3: notnull (1 = NOT NULL).
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not found on table {table!r}")


def _index_names(sync_url: str) -> set[str]:
    """Return all index names in the database."""
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).all()
    finally:
        eng.dispose()
    return {str(row[0]) for row in rows}


# ---------------------------------------------------------------------------
# Test 1 — upgrade lands all tables, columns, and indexes.
# ---------------------------------------------------------------------------


def test_upgrade_lands_tables_columns_and_indexes(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` creates all three runbook tables, both audit columns,
    and all four new indexes."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    tables = _table_names(sync_url)
    assert "runbook_templates" in tables
    assert "runbook_runs" in tables
    assert "runbook_run_step_states" in tables

    # audit_log columns present and nullable.
    audit_cols = _table_columns(sync_url, "audit_log")
    assert "run_id" in audit_cols
    assert "step_id" in audit_cols
    assert _column_is_nullable(sync_url, "audit_log", "run_id")
    assert _column_is_nullable(sync_url, "audit_log", "step_id")

    # Four new indexes present.
    indexes = _index_names(sync_url)
    assert "runbook_templates_tenant_slug_version_idx" in indexes
    assert "runbook_templates_tenant_status_idx" in indexes
    assert "runbook_runs_tenant_assigned_state_idx" in indexes
    assert "runbook_runs_tenant_template_idx" in indexes
    assert "audit_log_run_id_idx" in indexes

    # Spot-check columns on runbook_templates.
    rt_cols = _table_columns(sync_url, "runbook_templates")
    for col in (
        "id",
        "tenant_id",
        "slug",
        "version",
        "title",
        "description",
        "steps",
        "target_kind",
        "status",
        "created_by",
        "created_at",
        "edited_by",
        "edited_at",
    ):
        assert col in rt_cols, f"runbook_templates.{col} missing after upgrade"

    # Spot-check columns on runbook_runs.
    rr_cols = _table_columns(sync_url, "runbook_runs")
    for col in (
        "run_id",
        "tenant_id",
        "template_slug",
        "template_version",
        "assigned_to",
        "target",
        "params",
        "state",
        "started_by",
        "started_at",
        "completed_at",
        "abandoned_at",
    ):
        assert col in rr_cols, f"runbook_runs.{col} missing after upgrade"

    # Spot-check columns on runbook_run_step_states.
    ss_cols = _table_columns(sync_url, "runbook_run_step_states")
    for col in ("run_id", "step_id", "state", "started_at", "verified_at", "verify_response"):
        assert col in ss_cols, f"runbook_run_step_states.{col} missing after upgrade"


# ---------------------------------------------------------------------------
# Test 2 — downgrade → upgrade round-trip.
# ---------------------------------------------------------------------------


def test_downgrade_to_0033_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Downgrade to ``0033`` drops tables / columns / indexes;
    upgrade head restores them all."""
    cfg, sync_url = alembic_cfg

    command.upgrade(cfg, "head")
    assert "runbook_templates" in _table_names(sync_url)

    command.downgrade(cfg, "0033")

    # Tables dropped.
    tables = _table_names(sync_url)
    assert "runbook_templates" not in tables
    assert "runbook_runs" not in tables
    assert "runbook_run_step_states" not in tables

    # Audit columns dropped.
    audit_cols = _table_columns(sync_url, "audit_log")
    assert "run_id" not in audit_cols
    assert "step_id" not in audit_cols

    # Indexes dropped.
    indexes = _index_names(sync_url)
    assert "runbook_templates_tenant_slug_version_idx" not in indexes
    assert "runbook_templates_tenant_status_idx" not in indexes
    assert "runbook_runs_tenant_assigned_state_idx" not in indexes
    assert "runbook_runs_tenant_template_idx" not in indexes
    assert "audit_log_run_id_idx" not in indexes

    # Re-upgrade — everything restored.
    command.upgrade(cfg, "head")

    assert "runbook_templates" in _table_names(sync_url)
    assert "runbook_runs" in _table_names(sync_url)
    assert "runbook_run_step_states" in _table_names(sync_url)

    audit_cols = _table_columns(sync_url, "audit_log")
    assert "run_id" in audit_cols
    assert "step_id" in audit_cols

    indexes = _index_names(sync_url)
    assert "audit_log_run_id_idx" in indexes


# ---------------------------------------------------------------------------
# Test 3 — pre-existing audit_log rows (NULL run_id / step_id).
# ---------------------------------------------------------------------------


def test_audit_log_insert_without_run_id_step_id(
    alembic_cfg: tuple[Config, str],
) -> None:
    """An ``audit_log`` row inserted without ``run_id`` / ``step_id``
    succeeds and carries NULL on both columns (no regression)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    row_id = str(uuid.uuid4()).replace("-", "")
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO audit_log "
                    "(id, occurred_at, operator_sub, method, path, status_code, payload) "
                    "VALUES (:id, datetime('now'), 'op-test', 'DISPATCH', 'some.op', 200, '{}')"
                ),
                {"id": row_id},
            )
        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT run_id, step_id FROM audit_log WHERE id = :id"),
                {"id": row_id},
            ).one()
    finally:
        eng.dispose()

    assert row[0] is None, "run_id should be NULL for a pre-G12.1 audit row"
    assert row[1] is None, "step_id should be NULL for a pre-G12.1 audit row"


# ---------------------------------------------------------------------------
# Test 4 — FK integrity on runbook_run_step_states.
# ---------------------------------------------------------------------------


def test_runbook_run_step_states_fk_rejects_orphan_run_id(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Inserting a ``runbook_run_step_states`` row referencing a
    nonexistent ``run_id`` raises :exc:`IntegrityError` — the FK is real.

    SQLite enforces FK constraints only when ``PRAGMA foreign_keys=ON``
    is issued on every connection (the default is OFF). The test engine
    registers an event listener to enable FK enforcement so the
    integrity constraint is exercised on the dev/test driver, mirroring
    what PostgreSQL production always enforces unconditionally.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    nonexistent_run_id = str(uuid.uuid4()).replace("-", "")
    eng = sa_create_engine(sync_url)

    @event.listens_for(eng, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    try:
        with pytest.raises(IntegrityError), eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO runbook_run_step_states "
                    "(run_id, step_id, state) "
                    "VALUES (:run_id, 'step-1', 'pending')"
                ),
                {"run_id": nonexistent_run_id},
            )
    finally:
        eng.dispose()

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0085_create_dispatch_trace_store``.

#3212 flight-recorder storage + capture config (F1/F4/F6). Additive-only: two
new tables (``dispatch_trace`` header + ``dispatch_trace_span`` ordered child)
plus three ``ADD COLUMN``s (``tenant.flight_recorder_enabled``,
``tenant.flight_recorder_retention_days``, ``targets.flight_recorder_capture``).

Idempotency pinning: every forward / round-trip step targets this migration's
own revision (``0085``) and its ``down_revision`` (``0084``), never ``head`` —
so a future head migration cannot make ``upgrade("head")`` re-run this DDL on a
schema that already has it. SQLite is the test driver; the migration uses only
generic DDL + one JSONB variant so PG parity holds.
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

_REVISION = "0085"
_DOWN_REVISION = "0084"

_TRACE_TABLE = "dispatch_trace"
_SPAN_TABLE = "dispatch_trace_span"
_TRACE_COLUMNS = {
    "id",
    "audit_id",
    "tenant_id",
    "created_at",
    "expires_at",
    "redaction_uncertain",
}
_SPAN_COLUMNS = {
    "id",
    "trace_id",
    "seq",
    "span_kind",
    "name",
    "started_at",
    "duration_ms",
    "status",
    "attributes",
}
_TRACE_INDEXES = {
    "dispatch_trace_audit_id_idx",
    "dispatch_trace_expires_at_idx",
}
_SPAN_INDEXES = {"dispatch_trace_span_trace_seq_idx"}
_TENANT_NEW_COLUMNS = {"flight_recorder_enabled", "flight_recorder_retention_days"}
_TARGET_NEW_COLUMN = "flight_recorder_capture"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0085.db"
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


def _index_names(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _table_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def test_upgrade_creates_trace_tables_with_full_shape(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0085`` creates the header + span tables with their columns/indexes."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _columns(sync_url, _TRACE_TABLE) == _TRACE_COLUMNS
    assert _columns(sync_url, _SPAN_TABLE) == _SPAN_COLUMNS
    assert _index_names(sync_url, _TRACE_TABLE) >= _TRACE_INDEXES
    assert _index_names(sync_url, _SPAN_TABLE) >= _SPAN_INDEXES


def test_upgrade_adds_capture_config_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0085`` adds the per-tenant + per-target capture-config columns."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _columns(sync_url, "tenant") >= _TENANT_NEW_COLUMNS
    assert _TARGET_NEW_COLUMN in _columns(sync_url, "targets")


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0084`` drops the tables + columns; ``upgrade 0085`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _TRACE_TABLE in _table_names(sync_url)
    assert _SPAN_TABLE in _table_names(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    tables = _table_names(sync_url)
    assert _TRACE_TABLE not in tables
    assert _SPAN_TABLE not in tables
    assert not (_TENANT_NEW_COLUMNS & _columns(sync_url, "tenant"))
    assert _TARGET_NEW_COLUMN not in _columns(sync_url, "targets")

    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url, _TRACE_TABLE) == _TRACE_COLUMNS
    assert _columns(sync_url, _SPAN_TABLE) == _SPAN_COLUMNS
    assert _columns(sync_url, "tenant") >= _TENANT_NEW_COLUMNS
    assert _TARGET_NEW_COLUMN in _columns(sync_url, "targets")

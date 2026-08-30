# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0083_create_addon_step_event``.

#3027 step-event push contract. Two additive changes: the
``addon_pairing.service_account_sub`` column (the identity join key) and the
``addon_step_event`` durable log table (BIGSERIAL cursor + tenant/pairing
FKs + resume and stable-id indexes).

Idempotency pinning: every forward / round-trip step targets this
migration's own revision (``0083``) and its ``down_revision`` (``0082``),
never ``head`` — so a future head migration cannot make ``upgrade("head")``
re-run this DDL on a schema that already has it. SQLite is the test driver;
the migration uses only generic DDL so PG parity holds.
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

_REVISION = "0083"
_DOWN_REVISION = "0082"
_TABLE = "addon_step_event"
_EXPECTED_COLUMNS = {
    "seq",
    "id",
    "tenant_id",
    "pairing_id",
    "event_kind",
    "work_ref",
    "audit_id",
    "payload",
    "created_at",
}
_EXPECTED_INDEXES = {
    "addon_step_event_pairing_seq_idx",
    "addon_step_event_id_idx",
}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0083.db"
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


def test_upgrade_adds_service_account_sub_column(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0083`` adds the pairing identity join column."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert "service_account_sub" in _columns(sync_url, "addon_pairing")


def test_upgrade_creates_step_event_table(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0083`` creates the durable step-event log with its full shape."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _columns(sync_url, _TABLE) == _EXPECTED_COLUMNS
    assert _index_names(sync_url, _TABLE) >= _EXPECTED_INDEXES


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0082`` drops the table + column; ``upgrade 0083`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _TABLE in _table_names(sync_url)
    assert "service_account_sub" in _columns(sync_url, "addon_pairing")

    command.downgrade(cfg, _DOWN_REVISION)
    assert _TABLE not in _table_names(sync_url)
    assert "service_account_sub" not in _columns(sync_url, "addon_pairing")

    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url, _TABLE) == _EXPECTED_COLUMNS
    assert _index_names(sync_url, _TABLE) >= _EXPECTED_INDEXES

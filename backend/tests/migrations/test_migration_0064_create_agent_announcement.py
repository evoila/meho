# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0064_create_agent_announcement``.

Broadcast v2 Initiative #2543, Task #2547 (T2). Creates the append-only
``agent_announcement`` table -- the durable archive of every
agent-authored announcement -- plus its two indexes. This is the
initiative's **only** migration and extends the then-current single head
``0063``.

**Idempotency pinning (0049/0050/0053/0055/0057/0058 footgun).** Every
forward / round-trip step targets this migration's **own** revision
(``0064``) and its ``down_revision`` (``0063``), never ``head`` -- so a
future head migration cannot make ``upgrade("head")`` re-run this
``create_table`` on a schema that already has it. SQLite is the test
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

_REVISION = "0064"
_DOWN_REVISION = "0063"
_TABLE = "agent_announcement"
_EXPECTED_COLUMNS = {
    "id",
    "tenant_id",
    "principal_sub",
    "activity",
    "target",
    "scope",
    "targets",
    "phase",
    "planned_op_class",
    "ttl_minutes",
    "work_ref",
    "run_id",
    "created_at",
}
_EXPECTED_INDEXES = {
    "agent_announcement_tenant_created_at_idx",
    "agent_announcement_tenant_work_ref_idx",
}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0064.db"
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


def _table_info(sync_url: str, table: str) -> list[tuple[str, bool]]:
    """Return ``(column_name, is_not_null)`` pairs for *table*."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    return [(str(row[1]), int(row[3]) == 1) for row in rows]


def _columns(sync_url: str, table: str) -> set[str]:
    return {name for name, _ in _table_info(sync_url, table)}


def _not_null(sync_url: str, table: str, column: str) -> bool:
    for name, notnull in _table_info(sync_url, table):
        if name == column:
            return notnull
    raise AssertionError(f"column {column!r} not present on {table}")


def _index_names(sync_url: str, table: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    # PRAGMA index_list columns: (seq, name, unique, origin, partial).
    return {str(row[1]) for row in rows}


def _table_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def test_upgrade_creates_agent_announcement_table(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0064`` creates ``agent_announcement`` with the full column set."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _columns(sync_url, _TABLE) == _EXPECTED_COLUMNS
    # The always-populated columns are NOT NULL; optional claim fields nullable.
    for column in (
        "id",
        "tenant_id",
        "principal_sub",
        "activity",
        "targets",
        "phase",
        "created_at",
    ):
        assert _not_null(sync_url, _TABLE, column), f"{column} must be NOT NULL"
    for column in ("target", "scope", "planned_op_class", "ttl_minutes", "work_ref", "run_id"):
        assert not _not_null(sync_url, _TABLE, column), f"{column} must be nullable"


def test_upgrade_creates_indexes(alembic_cfg: tuple[Config, str]) -> None:
    """The (tenant, created_at DESC) + (tenant, work_ref) indexes are created."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _index_names(sync_url, _TABLE) >= _EXPECTED_INDEXES


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0063"`` drops the table; ``upgrade "0064"`` recreates it.

    Pinned to this migration's own revision on both legs (never ``head``)
    so a future head migration cannot break the round-trip -- the
    idempotency contract every migration test in this suite enforces.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _TABLE in _table_names(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _TABLE not in _table_names(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url, _TABLE) == _EXPECTED_COLUMNS
    assert _index_names(sync_url, _TABLE) >= _EXPECTED_INDEXES

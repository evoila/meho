# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0017_create_agent_run``.

Initiative #802 (G11.1 Agent runtime), Task #813 (T6). The migration
adds the ``agent_run`` table -- one row per LLM-agent invocation hosted
in MEHO's process; its ``id`` doubles as the ``agent_session_id`` lineage
key G11.4/C2 binds into per-tool-call audit rows.

Test matrix
-----------

* **Upgrade creates the table + columns + indexes.** ``upgrade head``
  from a clean DB leaves ``agent_run`` present with every documented
  column and its three named indexes.
* **Reversibility round-trip.** ``downgrade "0016"`` (0017's
  ``down_revision``) drops the table; a subsequent ``upgrade head``
  re-creates it. The target is the explicit revision rather than
  head-relative ``"-1"`` so the test keeps reverting *0017* even once a
  later migration becomes head.
* **audit_log untouched.** The migration must not disturb the
  pre-existing ``audit_log`` table (it only adds a new table); the 0014
  ``agent_session_id`` column survives.
* **Column nullability.** The nullable columns (``provider`` / ``model``
  / ``cost`` / ...) permit NULL; the NOT NULL columns (``tenant_id`` /
  ``identity_sub`` / ``trigger`` / ``model_tier`` / ``status`` /
  ``turns`` / ``created_at``) reject it.

The tests follow the synchronous pattern of
:mod:`tests.test_migration_0014_agent_session_id`:
``alembic.command.upgrade`` calls ``asyncio.run`` internally via env.py's
async cookbook, so the test function itself must be sync. SQLite is the
test driver; PG-side shape parity is covered by the testcontainers replay
suite in :mod:`tests.test_migration_rollback`.
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
    """Pin env, reset caches, return an Alembic config + sync URL.

    The fixture is *sync* because :func:`alembic.command.upgrade` calls
    :func:`asyncio.run` internally via the env.py async cookbook -- the
    same constraint that keeps every other migration test synchronous.
    The DB file lives under pytest's ``tmp_path`` so each test gets an
    isolated SQLite database; engine + settings caches are reset before
    and after so the alembic env reads *this* DATABASE_URL.
    """
    db_path = tmp_path / "migration_0017.db"
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
    """Return the set of table names in the SQLite DB."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def _table_columns(sync_url: str, table: str) -> set[str]:
    """Return the set of column names on *table* via ``PRAGMA``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
    return {str(row[1]) for row in rows}


def _column_is_nullable(sync_url: str, table: str, column: str) -> bool:
    """Return True when *column* on *table* permits NULL."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            # notnull is index 3: 0 => nullable, 1 => NOT NULL.
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on {table}")


def _table_indexes(sync_url: str, table: str) -> set[str]:
    """Return the set of index names declared on *table*."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    # PRAGMA index_list columns: (seq, name, unique, origin, partial)
    return {str(row[1]) for row in rows}


_EXPECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "agent_definition_id",
        "tenant_id",
        "identity_sub",
        "identity_act",
        "trigger",
        "model_tier",
        "provider",
        "model",
        "status",
        "turns",
        "cost",
        "output",
        "error",
        "parent_run_id",
        "created_at",
        "started_at",
        "ended_at",
    }
)

_EXPECTED_INDEXES: frozenset[str] = frozenset(
    {
        "agent_run_tenant_created_at_idx",
        "agent_run_status_idx",
        "agent_run_parent_run_id_idx",
    }
)


def test_upgrade_creates_agent_run_table_columns_indexes(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` lands the ``agent_run`` table with all columns + indexes."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert "agent_run" in _table_names(sync_url), (
        "migration 0017 must create the agent_run table on upgrade head"
    )

    columns = _table_columns(sync_url, "agent_run")
    assert columns == _EXPECTED_COLUMNS, (
        f"agent_run columns drifted from the documented set: "
        f"missing={_EXPECTED_COLUMNS - columns}, extra={columns - _EXPECTED_COLUMNS}"
    )

    indexes = _table_indexes(sync_url, "agent_run")
    for expected in _EXPECTED_INDEXES:
        assert expected in indexes, f"migration 0017 must create index {expected!r}"


def test_column_nullability(alembic_cfg: tuple[Config, str]) -> None:
    """NOT NULL columns reject NULL; nullable columns permit it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    not_null = (
        "tenant_id",
        "identity_sub",
        "trigger",
        "model_tier",
        "status",
        "turns",
        "created_at",
    )
    for col in not_null:
        assert not _column_is_nullable(sync_url, "agent_run", col), f"{col} must be NOT NULL"

    nullable = (
        "agent_definition_id",
        "identity_act",
        "provider",
        "model",
        "cost",
        "output",
        "error",
        "parent_run_id",
        "started_at",
        "ended_at",
    )
    for col in nullable:
        assert _column_is_nullable(sync_url, "agent_run", col), f"{col} must be nullable"


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0016"`` drops the table; ``upgrade head`` restores it.

    The reversibility contract migration 0017 inherits from 0007 / 0012 /
    0013. The downgrade target is the explicit revision ``"0016"``
    (0017's ``down_revision``) rather than head-relative ``"-1"``, so the
    moment a later migration (0018+) lands it would not silently stop
    exercising 0017's reverse -- anchoring to ``"0016"`` keeps this test
    pinned to 0017.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    # Sanity -- the upgrade landed the table before we reverse it.
    assert "agent_run" in _table_names(sync_url)

    command.downgrade(cfg, "0016")
    assert "agent_run" not in _table_names(sync_url), "downgrade must drop the agent_run table"

    # Re-upgrade -- the table comes back, proving the round-trip.
    command.upgrade(cfg, "head")
    assert "agent_run" in _table_names(sync_url)
    assert _table_columns(sync_url, "agent_run") == _EXPECTED_COLUMNS


def test_audit_log_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """The pre-existing ``audit_log`` table + its 0014 column survive 0017.

    0017 adds a new table only; it must not disturb ``audit_log``. Guards
    against an accidental edit to the wrong table.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    audit_columns = _table_columns(sync_url, "audit_log")
    assert "agent_session_id" in audit_columns, (
        "0014's agent_session_id on audit_log must survive 0017"
    )
    # The new lineage-key table and the audit table coexist.
    tables = _table_names(sync_url)
    assert "audit_log" in tables
    assert "agent_run" in tables

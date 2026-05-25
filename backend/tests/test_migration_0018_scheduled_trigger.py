# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0018_create_scheduled_trigger``.

Initiative #804 (G11.3 Scheduler), Task #822 (T1). The migration adds
the ``scheduled_trigger`` table -- the storage substrate for the
durable cron / one-off / event triggers that fire G11.1 agent runs.
This Task settles the durability-substrate fork (Option A: extend the
existing roll-our-own ``asyncio`` + ``pg_try_advisory_lock`` pattern;
see PR body for the full rationale).

Test matrix
-----------

* **Upgrade creates the table + columns + indexes.** ``upgrade head``
  from a clean DB leaves ``scheduled_trigger`` present with every
  documented column and its two named indexes.
* **Column nullability.** The NOT NULL columns (``tenant_id`` /
  ``agent_definition_id`` / ``kind`` / ``status`` / ``in_flight_policy``
  / ``created_by_sub`` / ``created_at`` / ``updated_at``) reject NULL;
  the discriminated-field columns (``cron_expr`` / ``fire_at`` /
  ``event_filter``) and the dispatcher-managed columns
  (``next_fire_at`` / ``last_fired_at``) permit NULL.
* **Reversibility round-trip.** ``downgrade "0017"`` (0018's
  ``down_revision``) drops the table; a subsequent ``upgrade head``
  re-creates it. The target is the explicit revision rather than
  head-relative ``"-1"`` so the test keeps reverting *0018* even once
  a later migration becomes head.
* **agent_run + agent_definition untouched.** The migration must not
  disturb the pre-existing tables shipped by ``0016`` / ``0017``; the
  per-tenant-name uniqueness index on ``agent_definition`` and the
  status index on ``agent_run`` both survive.

The tests follow the synchronous pattern of
:mod:`tests.test_migration_0017_agent_run`:
``alembic.command.upgrade`` calls ``asyncio.run`` internally via env.py's
async cookbook, so the test function itself must be sync. SQLite is the
test driver; PG-side shape parity is covered by the testcontainers
replay suite in :mod:`tests.test_migration_rollback`.
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
    db_path = tmp_path / "migration_0018.db"
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
        "tenant_id",
        "agent_definition_id",
        "kind",
        "cron_expr",
        "fire_at",
        "event_filter",
        "status",
        "in_flight_policy",
        "next_fire_at",
        "last_fired_at",
        "created_by_sub",
        "created_at",
        "updated_at",
    }
)

_EXPECTED_INDEXES: frozenset[str] = frozenset(
    {
        "scheduled_trigger_next_fire_at_idx",
        "scheduled_trigger_tenant_idx",
    }
)


def test_upgrade_creates_scheduled_trigger_table_columns_indexes(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` lands ``scheduled_trigger`` with all columns + indexes."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert "scheduled_trigger" in _table_names(sync_url), (
        "migration 0018 must create the scheduled_trigger table on upgrade head"
    )

    columns = _table_columns(sync_url, "scheduled_trigger")
    assert columns == _EXPECTED_COLUMNS, (
        f"scheduled_trigger columns drifted from the documented set: "
        f"missing={_EXPECTED_COLUMNS - columns}, extra={columns - _EXPECTED_COLUMNS}"
    )

    indexes = _table_indexes(sync_url, "scheduled_trigger")
    for expected in _EXPECTED_INDEXES:
        assert expected in indexes, f"migration 0018 must create index {expected!r}"


def test_column_nullability(alembic_cfg: tuple[Config, str]) -> None:
    """NOT NULL columns reject NULL; nullable columns permit it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    not_null = (
        "tenant_id",
        "agent_definition_id",
        "kind",
        "status",
        "in_flight_policy",
        "created_by_sub",
        "created_at",
        "updated_at",
    )
    for col in not_null:
        assert not _column_is_nullable(sync_url, "scheduled_trigger", col), (
            f"{col} must be NOT NULL"
        )

    nullable = (
        "cron_expr",
        "fire_at",
        "event_filter",
        "next_fire_at",
        "last_fired_at",
    )
    for col in nullable:
        assert _column_is_nullable(sync_url, "scheduled_trigger", col), f"{col} must be nullable"


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0017"`` drops the table; ``upgrade head`` restores it.

    The reversibility contract migration 0018 inherits from 0007 / 0012
    / 0013 / 0017. The downgrade target is the explicit revision
    ``"0017"`` (0018's ``down_revision``) rather than head-relative
    ``"-1"``, so the moment a later migration (0019+) lands it would
    not silently stop exercising 0018's reverse -- anchoring to
    ``"0017"`` keeps this test pinned to 0018.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    # Sanity -- the upgrade landed the table before we reverse it.
    assert "scheduled_trigger" in _table_names(sync_url)

    command.downgrade(cfg, "0017")
    assert "scheduled_trigger" not in _table_names(sync_url), (
        "downgrade must drop the scheduled_trigger table"
    )

    # Re-upgrade -- the table comes back, proving the round-trip.
    command.upgrade(cfg, "head")
    assert "scheduled_trigger" in _table_names(sync_url)
    assert _table_columns(sync_url, "scheduled_trigger") == _EXPECTED_COLUMNS


def test_sibling_tables_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """The pre-existing ``agent_run`` + ``agent_definition`` tables survive 0018.

    0018 adds a new table only; it must not disturb the
    agent-runtime tables shipped by 0016 / 0017. Guards against an
    accidental edit to the wrong table.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    tables = _table_names(sync_url)
    assert "agent_definition" in tables
    assert "agent_run" in tables
    assert "scheduled_trigger" in tables

    # The 0017 agent_run.status index survives.
    agent_run_indexes = _table_indexes(sync_url, "agent_run")
    assert "agent_run_status_idx" in agent_run_indexes

    # The 0016 unique tenant/name index on agent_definition survives.
    definition_indexes = _table_indexes(sync_url, "agent_definition")
    assert "agent_definition_tenant_name_idx" in definition_indexes

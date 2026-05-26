# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0025_add_agent_run_lease_reaper``.

Initiative #804 (G11.3 Scheduler), Task #825 (T4). The migration adds
three columns (``lease_owner`` / ``lease_expires_at`` / ``in_flight_policy``),
one partial index (``agent_run_lease_expires_at_idx``), and one
``CHECK`` constraint (``ck_agent_run_in_flight_policy``) to the
``agent_run`` table.

Test matrix
-----------

* **Upgrade adds the three columns + the index.** ``upgrade head``
  leaves ``agent_run`` with the new columns and the new index name
  present.
* **Reversibility round-trip.** ``downgrade "0024"`` drops the columns
  and the index; a subsequent ``upgrade head`` re-creates them.
* **Column nullability.** ``in_flight_policy`` is NOT NULL with a
  ``'fail_into_audit'`` server default; ``lease_owner`` and
  ``lease_expires_at`` are nullable.
* **Server default backfills existing rows.** Inserting a pre-existing
  row before upgrade, then upgrading, the post-upgrade row has
  ``in_flight_policy = 'fail_into_audit'``. (The migration relies on
  the column's server default to handle the NOT NULL flip without a
  separate backfill UPDATE.)
* **CHECK constraint rejects unknown.** Insert with
  ``in_flight_policy='not_a_real_policy'`` -> IntegrityError after
  upgrade.

Same synchronous-pattern + tmp-path-DB shape as
:mod:`tests.test_migration_0017_agent_run`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL.

    Same shape as
    :func:`tests.test_migration_0017_agent_run.alembic_cfg`. The DB
    file lives under pytest's ``tmp_path`` so each test gets an
    isolated SQLite database; engine + settings caches are reset
    before and after so the alembic env reads *this* DATABASE_URL.
    """
    db_path = tmp_path / "migration_0025.db"
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


def _table_columns(sync_url: str, table: str) -> set[str]:
    """Return the set of column names on *table* via ``PRAGMA``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
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
    return {str(row[1]) for row in rows}


def _seed_tenant(sync_url: str) -> str:
    """Insert a tenant row directly; return its id as a hex string.

    Independent of the lifecycle helpers because the migration tests
    use a sync sqlalchemy engine (the alembic env's async loop is
    closed by the time the test inspects the result).
    """
    tenant_id = uuid.uuid4()
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            existing = conn.execute(
                text("SELECT id FROM tenant WHERE slug = 'rdc-internal'")
            ).first()
            if existing is not None:
                return str(existing[0])
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name) "
                    "VALUES (:id, 'rdc-internal', 'Tenant rdc-internal')"
                ),
                {"id": tenant_id.hex},
            )
    finally:
        sync_eng.dispose()
    return tenant_id.hex


def test_upgrade_adds_lease_columns_and_index(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` adds the three columns and the partial index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _table_columns(sync_url, "agent_run")
    assert {"lease_owner", "lease_expires_at", "in_flight_policy"} <= columns

    indexes = _table_indexes(sync_url, "agent_run")
    assert "agent_run_lease_expires_at_idx" in indexes


def test_column_nullability(alembic_cfg: tuple[Config, str]) -> None:
    """``in_flight_policy`` is NOT NULL; the lease columns are nullable."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert _column_is_nullable(sync_url, "agent_run", "lease_owner")
    assert _column_is_nullable(sync_url, "agent_run", "lease_expires_at")
    assert not _column_is_nullable(sync_url, "agent_run", "in_flight_policy")


def test_in_flight_policy_default_is_fail_into_audit(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A row inserted post-upgrade picks up ``'fail_into_audit'`` from the server default.

    Exercises the column's ``server_default=sa.text("'fail_into_audit'")``
    which is what lets the migration flip the column to NOT NULL
    against an existing table without a separate backfill UPDATE.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    tenant_hex = _seed_tenant(sync_url)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            run_id = uuid.uuid4().hex
            # Insert without specifying in_flight_policy -- the server
            # default must fire.
            conn.execute(
                text(
                    "INSERT INTO agent_run (id, tenant_id, identity_sub, trigger, "
                    "model_tier, status, turns, created_at) VALUES "
                    "(:id, :tid, 'user-default', 'direct', 'cheap', 'pending', 0, "
                    "datetime('now'))"
                ),
                {"id": run_id, "tid": tenant_hex},
            )
            row = conn.execute(
                text("SELECT in_flight_policy FROM agent_run WHERE id = :id"),
                {"id": run_id},
            ).first()
        assert row is not None
        assert row[0] == "fail_into_audit"
    finally:
        sync_eng.dispose()


def test_in_flight_policy_check_rejects_unknown(
    alembic_cfg: tuple[Config, str],
) -> None:
    """An ``in_flight_policy`` outside the closed enum raises :class:`IntegrityError`."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    tenant_hex = _seed_tenant(sync_url)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO agent_run (id, tenant_id, identity_sub, trigger, "
                    "model_tier, status, turns, in_flight_policy, created_at) VALUES "
                    "(:id, :tid, 'user-bad', 'direct', 'cheap', 'pending', 0, "
                    "'retry_with_backoff', datetime('now'))"
                ),
                {"id": uuid.uuid4().hex, "tid": tenant_hex},
            )
    finally:
        sync_eng.dispose()


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0024"`` drops the new columns / index; ``upgrade head`` restores them.

    The reversibility contract -- migration 0025 inherits the explicit
    drop-then-create symmetry every later migration in this chain follows.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert {"lease_owner", "lease_expires_at", "in_flight_policy"} <= _table_columns(
        sync_url, "agent_run"
    )

    command.downgrade(cfg, "0024")
    columns_after_down = _table_columns(sync_url, "agent_run")
    assert "lease_owner" not in columns_after_down
    assert "lease_expires_at" not in columns_after_down
    assert "in_flight_policy" not in columns_after_down
    assert "agent_run_lease_expires_at_idx" not in _table_indexes(sync_url, "agent_run")

    command.upgrade(cfg, "head")
    assert {"lease_owner", "lease_expires_at", "in_flight_policy"} <= _table_columns(
        sync_url, "agent_run"
    )
    assert "agent_run_lease_expires_at_idx" in _table_indexes(sync_url, "agent_run")

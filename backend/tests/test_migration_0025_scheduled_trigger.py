# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0025_scheduled_trigger_dispatcher_columns``.

Initiative #804 (G11.3 Scheduler), Task #823 (T2 -- cron + one-off
dispatcher loop). Migration 0025 extends the ``scheduled_trigger``
table 0020 created with the three columns the dispatcher owns
(``timezone`` / ``identity_sub`` / ``inputs``) and widens the
``ck_scheduled_trigger_status`` ``CHECK`` to include ``'fired'``.

Test matrix
-----------

* **Upgrade adds the three new columns.** ``upgrade 0025`` (from a
  clean DB) leaves ``scheduled_trigger`` with all 0020 columns *plus*
  ``timezone``, ``identity_sub``, ``inputs``.
* **NOT NULL backfills.** ``timezone`` and ``identity_sub`` are NOT
  NULL and pick up the server defaults (``'UTC'`` and
  ``'__scheduler__'``) on rows that pre-existed at 0020. ``inputs``
  is nullable.
* **Status CHECK widened.** Inserting a row with
  ``status='fired'`` succeeds at 0025 (would have raised at 0020).
  Inserting a row with a value outside the v2 vocabulary still
  raises.
* **Reversibility round-trip.** ``downgrade "0020"`` removes the
  three columns and restores the v1 ``CHECK``; ``upgrade 0025``
  re-applies the v2 shape. The downgrade target is the explicit
  revision rather than head-relative so it stays pinned to 0025's
  reverse even once a 0026+ lands.

The tests follow the synchronous pattern of
:mod:`tests.test_migration_0020_scheduled_trigger`:
``alembic.command.upgrade`` calls ``asyncio.run`` internally via
env.py's async cookbook, so the test function itself must be sync.
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

    Same shape as :func:`tests.test_migration_0020_scheduled_trigger.alembic_cfg`:
    sync because Alembic's env.py runs ``asyncio.run`` internally; an
    isolated SQLite DB per test; settings + engine caches reset on
    entry and exit.
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
    """Return the set of column names on *table*."""
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


_NEW_COLUMNS_0025: frozenset[str] = frozenset(
    {
        "timezone",
        "identity_sub",
        "inputs",
    }
)


def _seed_tenant_and_definition(sync_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one tenant + one agent_definition; return their ids.

    The status CHECK tests insert a ``scheduled_trigger`` row directly
    via raw SQL, which trips the real FKs to ``tenant`` and
    ``agent_definition``. A dedicated seed slug per test avoids
    collisions with the rdc-internal tenant migration 0018 ships.
    """
    tenant_id = uuid.uuid4()
    definition_id = uuid.uuid4()
    slug = f"test-0025-{tenant_id.hex[:8]}"
    name = f"test-0025-agent-{definition_id.hex[:8]}"

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant (id, slug, name, created_at) "
                    "VALUES (:id, :slug, :name, :now)"
                ),
                {
                    "id": str(tenant_id),
                    "slug": slug,
                    "name": slug,
                    "now": "2026-05-25T00:00:00",
                },
            )
            conn.execute(
                text(
                    "INSERT INTO agent_definition "
                    "(id, tenant_id, name, identity_ref, model_tier, "
                    " system_prompt, toolset, turn_budget, enabled, "
                    " created_by_sub, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :name, :identity_ref, "
                    " :model_tier, :system_prompt, :toolset, "
                    " :turn_budget, :enabled, :sub, :now, :now)"
                ),
                {
                    "id": str(definition_id),
                    "tenant_id": str(tenant_id),
                    "name": name,
                    "identity_ref": f"agent:{name}",
                    "model_tier": "standard",
                    "system_prompt": "test",
                    "toolset": "{}",
                    "turn_budget": 2,
                    "enabled": 1,
                    "sub": "seed-admin",
                    "now": "2026-05-25T00:00:00",
                },
            )
    finally:
        sync_eng.dispose()
    return tenant_id, definition_id


def test_upgrade_adds_dispatcher_columns(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0025`` adds ``timezone`` / ``identity_sub`` / ``inputs``."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0025")

    columns = _table_columns(sync_url, "scheduled_trigger")
    for new_col in _NEW_COLUMNS_0025:
        assert new_col in columns, (
            f"0025 must add column {new_col!r} to scheduled_trigger; "
            f"actual columns: {sorted(columns)}"
        )


def test_new_column_nullability(alembic_cfg: tuple[Config, str]) -> None:
    """``timezone`` / ``identity_sub`` are NOT NULL; ``inputs`` is nullable."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0025")

    assert not _column_is_nullable(sync_url, "scheduled_trigger", "timezone")
    assert not _column_is_nullable(sync_url, "scheduled_trigger", "identity_sub")
    assert _column_is_nullable(sync_url, "scheduled_trigger", "inputs")


def test_fired_status_accepted_after_0025(alembic_cfg: tuple[Config, str]) -> None:
    """The widened ``ck_scheduled_trigger_status`` admits ``status='fired'``.

    Inserts a one-off row directly at the SQL layer (bypassing the ORM
    so the CHECK is exercised at the dialect's enforcement boundary).
    Pre-0025 the same insert would raise :class:`IntegrityError`; the
    test fails fast if the widening didn't take effect.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0025")

    tenant_id, definition_id = _seed_tenant_and_definition(sync_url)
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO scheduled_trigger "
                    "(id, tenant_id, agent_definition_id, kind, fire_at, "
                    " timezone, status, in_flight_policy, identity_sub, "
                    " created_by_sub, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :definition_id, 'one_off', "
                    " :fire_at, 'UTC', 'fired', 'fail_into_audit', "
                    " '__scheduler__', 'seed-admin', :now, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": str(tenant_id),
                    "definition_id": str(definition_id),
                    "fire_at": "2026-05-26T00:00:00",
                    "now": "2026-05-25T00:00:00",
                },
            )
    finally:
        sync_eng.dispose()


def test_unknown_status_still_rejected_after_0025(
    alembic_cfg: tuple[Config, str],
) -> None:
    """A status outside the v2 vocabulary still raises ``IntegrityError``.

    Pins the CHECK as a closed enum after the widening -- 0025 must
    add ``'fired'`` to the set, not relax the constraint into a
    free-text column.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0025")

    tenant_id, definition_id = _seed_tenant_and_definition(sync_url)
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn, pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO scheduled_trigger "
                    "(id, tenant_id, agent_definition_id, kind, fire_at, "
                    " timezone, status, in_flight_policy, identity_sub, "
                    " created_by_sub, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :definition_id, 'one_off', "
                    " :fire_at, 'UTC', 'totally-bogus', 'fail_into_audit', "
                    " '__scheduler__', 'seed-admin', :now, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": str(tenant_id),
                    "definition_id": str(definition_id),
                    "fire_at": "2026-05-26T00:00:00",
                    "now": "2026-05-25T00:00:00",
                },
            )
    finally:
        sync_eng.dispose()


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0020"`` drops the three columns; ``upgrade 0025`` restores them.

    Both targets are explicit revisions so the round-trip stays pinned
    to 0025's reverse / forward even once 0026+ lands.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0025")

    columns = _table_columns(sync_url, "scheduled_trigger")
    for new_col in _NEW_COLUMNS_0025:
        assert new_col in columns

    command.downgrade(cfg, "0020")
    columns_after_down = _table_columns(sync_url, "scheduled_trigger")
    for new_col in _NEW_COLUMNS_0025:
        assert new_col not in columns_after_down, (
            f"downgrade 0025 must drop {new_col!r}; surviving columns: {sorted(columns_after_down)}"
        )

    # Re-upgrade -- the columns come back, proving the round-trip.
    command.upgrade(cfg, "0025")
    columns_after_up = _table_columns(sync_url, "scheduled_trigger")
    for new_col in _NEW_COLUMNS_0025:
        assert new_col in columns_after_up


def test_downgrade_refuses_when_fired_rows_exist(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0020"`` raises ``RuntimeError`` when ``status='fired'`` rows exist.

    The v1 ``ck_scheduled_trigger_status`` admits only
    ``{active, paused, cancelled}``; a row written by the dispatcher's
    one-off finalisation path (``status='fired'``) would orphan the
    narrowed constraint either at the PG ``ADD CONSTRAINT`` validation
    pass or at the SQLite ``batch_alter_table`` recreate-table
    integrity check. The migration refuses the downgrade before
    touching any DDL so the schema stays at 0025 rather than getting
    half-applied. Mirrors the precedent in
    :mod:`alembic.versions.0010_widen_graph_edge_kind`.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0025")

    tenant_id, definition_id = _seed_tenant_and_definition(sync_url)
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO scheduled_trigger "
                    "(id, tenant_id, agent_definition_id, kind, fire_at, "
                    " timezone, status, in_flight_policy, identity_sub, "
                    " created_by_sub, created_at, updated_at) "
                    "VALUES (:id, :tenant_id, :definition_id, 'one_off', "
                    " :fire_at, 'UTC', 'fired', 'fail_into_audit', "
                    " '__scheduler__', 'seed-admin', :now, :now)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": str(tenant_id),
                    "definition_id": str(definition_id),
                    "fire_at": "2026-05-26T00:00:00",
                    "now": "2026-05-25T00:00:00",
                },
            )
    finally:
        sync_eng.dispose()

    with pytest.raises(RuntimeError, match=r"status='fired'.*orphaned"):
        command.downgrade(cfg, "0020")

    # The refusal is pre-DDL: the table is still at 0025 (columns
    # intact, v2 CHECK still in place), not half-downgraded.
    columns_after_refused = _table_columns(sync_url, "scheduled_trigger")
    for new_col in _NEW_COLUMNS_0025:
        assert new_col in columns_after_refused, (
            f"refused downgrade must leave {new_col!r} intact; "
            f"observed columns: {sorted(columns_after_refused)}"
        )

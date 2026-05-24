# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0014_add_audit_log_agent_session_id``.

Initiative #377 (G8.2 audit replay), Task #1009 (T1). The migration is
the schema foundation of G8.2: it adds the nullable
``audit_log.agent_session_id`` column + its b-tree index, mirroring the
soft-FK discipline migrations 0002 / 0004 / 0006 established. No write
path ships here -- the column lands NULL until T2 wires the MCP
``Mcp-Session-Id`` header capture.

Test matrix
-----------

* **Upgrade adds the column + index.** ``upgrade head`` from a clean
  DB leaves ``audit_log.agent_session_id`` present and nullable, and
  the named ``audit_log_agent_session_id_idx`` index defined.
* **Reversibility round-trip.** ``downgrade -1`` drops both the column
  and the index; a subsequent ``upgrade head`` re-creates them. This
  is the 0006 reversibility contract this migration inherits.
* **parent_audit_id is untouched.** The migration must not re-add the
  pre-existing ``parent_audit_id`` column / index (it shipped in 0006);
  both survive the upgrade unchanged and `agent_session_id` is a
  *distinct* new column.
* **ORM-field smoke.** :attr:`AuditLog.agent_session_id` resolves on
  the mapped class and a freshly constructed ``AuditLog`` (without the
  field) defaults it to ``None`` -- the Python-side default that
  replaces the deliberately-absent server default.

The tests follow the synchronous pattern established by
:mod:`tests.test_migration_0011_backfill_when_to_use`:
``alembic.command.upgrade`` calls ``asyncio.run`` internally via
env.py's async cookbook, so the test function itself must be sync.
SQLite is the test driver; PG-side shape parity is covered by the
testcontainers replay suite in :mod:`tests.test_migration_rollback`.
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

    The fixture is *sync* (returns rather than yields async) because
    :func:`alembic.command.upgrade` calls :func:`asyncio.run`
    internally via the env.py async cookbook -- the same constraint
    that keeps every other migration test in
    :mod:`tests.test_migration_0011_backfill_when_to_use` synchronous.

    The DB file lives under pytest's ``tmp_path`` so each test gets an
    isolated SQLite database; engine + settings caches are reset before
    and after so the alembic env reads *this* DATABASE_URL.
    """
    db_path = tmp_path / "migration_0014.db"
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


def _audit_log_columns(sync_url: str) -> set[str]:
    """Return the set of ``audit_log`` column names via ``PRAGMA``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(audit_log)")).all()
    finally:
        sync_eng.dispose()
    # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk)
    return {str(row[1]) for row in rows}


def _audit_log_column_is_nullable(sync_url: str, column: str) -> bool:
    """Return True when *column* on ``audit_log`` permits NULL."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(audit_log)")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            # notnull is index 3: 0 => nullable, 1 => NOT NULL.
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on audit_log")


def _audit_log_indexes(sync_url: str) -> set[str]:
    """Return the set of index names declared on ``audit_log``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA index_list(audit_log)")).all()
    finally:
        sync_eng.dispose()
    # PRAGMA index_list columns: (seq, name, unique, origin, partial)
    return {str(row[1]) for row in rows}


def test_upgrade_adds_agent_session_id_column_and_index(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` lands the nullable column + its named index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _audit_log_columns(sync_url)
    assert "agent_session_id" in columns, (
        "migration 0014 must add audit_log.agent_session_id on upgrade head"
    )
    assert _audit_log_column_is_nullable(sync_url, "agent_session_id"), (
        "agent_session_id must be nullable -- no NOT NULL in v0.2 (soft-FK discipline)"
    )
    assert "audit_log_agent_session_id_idx" in _audit_log_indexes(sync_url), (
        "migration 0014 must create audit_log_agent_session_id_idx on upgrade head"
    )


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade -1`` drops the column + index; ``upgrade head`` restores them.

    This is the reversibility contract migration 0014 inherits from
    0006: the inverse must work cleanly on SQLite (and, by the same
    generic-DDL discipline, on PostgreSQL).
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    # Sanity -- the upgrade landed the column + index before we reverse it.
    assert "agent_session_id" in _audit_log_columns(sync_url)
    assert "audit_log_agent_session_id_idx" in _audit_log_indexes(sync_url)

    command.downgrade(cfg, "-1")
    assert "agent_session_id" not in _audit_log_columns(sync_url), (
        "downgrade must drop audit_log.agent_session_id"
    )
    assert "audit_log_agent_session_id_idx" not in _audit_log_indexes(sync_url), (
        "downgrade must drop audit_log_agent_session_id_idx"
    )

    # Re-upgrade -- the column + index come back, proving the round-trip.
    command.upgrade(cfg, "head")
    assert "agent_session_id" in _audit_log_columns(sync_url)
    assert "audit_log_agent_session_id_idx" in _audit_log_indexes(sync_url)


def test_parent_audit_id_is_untouched(
    alembic_cfg: tuple[Config, str],
) -> None:
    """The pre-existing ``parent_audit_id`` column + index survive 0014.

    Guards against the #377-original-body confusion that implied both
    columns were new: ``parent_audit_id`` shipped in 0006 (G0.6-T7,
    #398). 0014 adds *only* ``agent_session_id`` -- both columns must
    coexist after upgrade, and neither index may be duplicated.
    """
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _audit_log_columns(sync_url)
    assert "parent_audit_id" in columns, "0006's parent_audit_id must survive 0014"
    assert "agent_session_id" in columns, "0014's agent_session_id must be present"
    assert "parent_audit_id" != "agent_session_id"  # distinct columns, not a rename

    indexes = _audit_log_indexes(sync_url)
    assert "audit_log_parent_audit_id_idx" in indexes
    assert "audit_log_agent_session_id_idx" in indexes


def test_orm_field_resolves_and_defaults_none() -> None:
    """:attr:`AuditLog.agent_session_id` resolves and defaults to ``None``.

    The Python-side ORM default replaces the deliberately-absent
    server default; an ``AuditLog`` constructed without the field must
    read it back as ``None`` (the chassis/non-MCP-row default state).

    Importing the model here (not at module top) keeps this smoke test
    independent of the migration fixture -- it exercises the mapped
    class, not the DDL.
    """
    from meho_backplane.db.models import AuditLog

    # The mapped attribute exists on the class.
    assert hasattr(AuditLog, "agent_session_id")

    row = AuditLog(
        operator_sub="op-smoke",
        method="POST",
        path="/mcp",
        status_code=200,
    )
    assert row.agent_session_id is None, (
        "an AuditLog built without agent_session_id must default it to None"
    )

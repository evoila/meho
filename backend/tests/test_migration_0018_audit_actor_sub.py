# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0019_add_audit_log_actor_sub``.

G11.2-T2 (#816) adds the nullable ``audit_log.actor_sub`` column and its
b-tree index. The migration mirrors the soft-FK discipline established by
0002 / 0004 / 0006 / 0014.

Test matrix
-----------

* **Upgrade adds the column + index.** ``upgrade head`` from a clean DB
  leaves ``audit_log.actor_sub`` present and nullable, and the named
  ``audit_log_actor_sub_idx`` index defined.
* **Reversibility round-trip.** ``downgrade "0018"`` drops both; a
  subsequent ``upgrade head`` re-creates them. The target is the explicit
  revision ``"0018"`` (0019's ``down_revision``) so this test keeps
  exercising 0019's reverse even when later migrations land at head.
* **agent_session_id is untouched.** The pre-existing
  ``agent_session_id`` column + index (added in 0014) must survive 0019
  unchanged. Guards against accidental DDL collisions.
* **ORM-field smoke.** :attr:`AuditLog.actor_sub` resolves on the mapped
  class and a freshly constructed ``AuditLog`` (without the field)
  defaults it to ``None`` — the Python-side default that replaces the
  deliberately-absent server default.

The tests follow the synchronous pattern established by
:mod:`tests.test_migration_0014_agent_session_id`: ``alembic.command.upgrade``
calls ``asyncio.run`` internally, so the test functions are sync.
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
from meho_backplane.db.models import AuditLog
from meho_backplane.settings import get_settings


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env vars, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0019.db"
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


# ---------------------------------------------------------------------------
# SQLite introspection helpers (mirrors test_migration_0014 pattern)
# ---------------------------------------------------------------------------


def _audit_log_columns(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(audit_log)")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _audit_log_column_is_nullable(sync_url: str, column: str) -> bool:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA table_info(audit_log)")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on audit_log")


def _audit_log_indexes(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("PRAGMA index_list(audit_log)")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_upgrade_adds_actor_sub_column_and_index(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade head`` lands the nullable actor_sub column + its named index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _audit_log_columns(sync_url)
    assert "actor_sub" in columns, "migration 0019 must add audit_log.actor_sub on upgrade head"
    assert _audit_log_column_is_nullable(sync_url, "actor_sub"), (
        "actor_sub must be nullable — soft-FK discipline, NULL for non-delegated tokens"
    )
    assert "audit_log_actor_sub_idx" in _audit_log_indexes(sync_url), (
        "migration 0019 must create audit_log_actor_sub_idx on upgrade head"
    )


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0018"`` drops column + index; ``upgrade head`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert "actor_sub" in _audit_log_columns(sync_url)
    assert "audit_log_actor_sub_idx" in _audit_log_indexes(sync_url)

    command.downgrade(cfg, "0018")
    assert "actor_sub" not in _audit_log_columns(sync_url), (
        "downgrade must drop audit_log.actor_sub"
    )
    assert "audit_log_actor_sub_idx" not in _audit_log_indexes(sync_url), (
        "downgrade must drop audit_log_actor_sub_idx"
    )

    command.upgrade(cfg, "head")
    assert "actor_sub" in _audit_log_columns(sync_url)
    assert "audit_log_actor_sub_idx" in _audit_log_indexes(sync_url)


def test_agent_session_id_is_untouched(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Pre-existing ``agent_session_id`` column + index survive migration 0019."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert "agent_session_id" in _audit_log_columns(sync_url), (
        "agent_session_id must survive 0019 unchanged"
    )
    assert "audit_log_agent_session_id_idx" in _audit_log_indexes(sync_url), (
        "audit_log_agent_session_id_idx must survive 0019 unchanged"
    )


def test_orm_field_defaults_to_none() -> None:
    """AuditLog.actor_sub resolves on the class and defaults to None."""
    # The field must exist on the mapped class — a missing attribute
    # would raise AttributeError (not cause a silent NULL).
    field = AuditLog.actor_sub
    assert field is not None, "AuditLog.actor_sub must be a mapped attribute"

    # Constructing without the field yields None (the Python-side default).
    import uuid
    from datetime import UTC, datetime
    from decimal import Decimal

    row = AuditLog(
        id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        operator_sub="op-1",
        method="GET",
        path="/test",
        status_code=200,
        duration_ms=Decimal("1.00"),
        payload={},
    )
    assert row.actor_sub is None, "actor_sub must default to None when not supplied"

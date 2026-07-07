# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0021_add_audit_log_actor_sub``.

Initiative #803 (G11.2), Task #816 (T2). Adds the nullable
``audit_log.actor_sub`` column + its b-tree index — the RFC 8693 actor
(acting agent) recorded alongside ``operator_sub`` on a user-initiated
agent run's audit rows. Soft-column discipline mirrors 0014: nullable, no
server default (Python-side ``None``), reversible.

Mirrors :mod:`tests.test_migration_0014_agent_session_id`; SQLite is the
test driver and the migration uses only generic DDL, so PG parity holds.
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
    """Pin env, reset caches, return an Alembic config + sync URL (sync fixture)."""
    db_path = tmp_path / "migration_0021.db"
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


def test_upgrade_adds_actor_sub_column_and_index(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade head`` lands the nullable column + its named index."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    assert "actor_sub" in _audit_log_columns(sync_url), (
        "migration 0021 must add audit_log.actor_sub on upgrade head"
    )
    assert _audit_log_column_is_nullable(sync_url, "actor_sub"), (
        "actor_sub must be nullable -- NULL for direct/autonomous rows"
    )
    assert "audit_log_actor_sub_idx" in _audit_log_indexes(sync_url), (
        "migration 0021 must create audit_log_actor_sub_idx on upgrade head"
    )


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade "0020"`` drops column + index; ``upgrade head`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "actor_sub" in _audit_log_columns(sync_url)
    assert "audit_log_actor_sub_idx" in _audit_log_indexes(sync_url)

    command.downgrade(cfg, "0020")
    assert "actor_sub" not in _audit_log_columns(sync_url), "downgrade must drop actor_sub"
    assert "audit_log_actor_sub_idx" not in _audit_log_indexes(sync_url), (
        "downgrade must drop audit_log_actor_sub_idx"
    )

    command.upgrade(cfg, "head")
    assert "actor_sub" in _audit_log_columns(sync_url)
    assert "audit_log_actor_sub_idx" in _audit_log_indexes(sync_url)


def test_agent_session_id_is_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """The pre-existing ``agent_session_id`` column + index survive 0021."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _audit_log_columns(sync_url)
    assert "agent_session_id" in columns, "0014's agent_session_id must survive 0021"
    assert "actor_sub" in columns
    indexes = _audit_log_indexes(sync_url)
    assert "audit_log_agent_session_id_idx" in indexes
    assert "audit_log_actor_sub_idx" in indexes


def test_orm_field_resolves_and_defaults_none() -> None:
    """:attr:`AuditLog.actor_sub` resolves and defaults to ``None``."""
    from meho_backplane.db.models import AuditLog

    assert hasattr(AuditLog, "actor_sub")
    row = AuditLog(
        operator_sub="op-smoke",
        method="POST",
        path="/api/v1/agent-runs",
        status_code=200,
    )
    assert row.actor_sub is None, "an AuditLog built without actor_sub must default it to None"

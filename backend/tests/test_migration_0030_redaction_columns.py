# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0030_add_audit_log_redaction_columns``.

Initiative #805 (G11.4), Task #1071 (T2). Adds the nullable
``audit_log.raw_payload`` + ``audit_log.redaction_manifest`` JSON
columns -- the connector-boundary redaction middleware writes both
on every dispatch row, but pre-G11.4 rows stay NULL. Soft-column
discipline mirrors 0014 / 0021: nullable, no server default,
reversible. The migration uses only generic ``sa.JSON`` so PG and
SQLite parity holds (the ORM pins ``JSONB`` on PG via
``with_variant``).
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
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0030.db"
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
            # SQLite PRAGMA: column 3 is notnull (1 = NOT NULL).
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on audit_log")


def test_upgrade_adds_both_columns_nullable(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade head`` lands both JSON columns as nullable."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _audit_log_columns(sync_url)
    assert "raw_payload" in columns
    assert "redaction_manifest" in columns
    assert _audit_log_column_is_nullable(sync_url, "raw_payload")
    assert _audit_log_column_is_nullable(sync_url, "redaction_manifest")


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0029`` drops both columns; ``upgrade head`` restores them."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")
    assert "raw_payload" in _audit_log_columns(sync_url)
    assert "redaction_manifest" in _audit_log_columns(sync_url)

    command.downgrade(cfg, "0029")
    columns = _audit_log_columns(sync_url)
    assert "raw_payload" not in columns
    assert "redaction_manifest" not in columns

    command.upgrade(cfg, "head")
    columns = _audit_log_columns(sync_url)
    assert "raw_payload" in columns
    assert "redaction_manifest" in columns


def test_existing_columns_untouched(alembic_cfg: tuple[Config, str]) -> None:
    """Pre-0030 columns survive: actor_sub (0021), agent_session_id (0014),
    parent_audit_id (0006), target_id (0004), tenant_id (0002)."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "head")

    columns = _audit_log_columns(sync_url)
    for column in (
        "actor_sub",
        "agent_session_id",
        "parent_audit_id",
        "target_id",
        "tenant_id",
        "payload",
    ):
        assert column in columns, f"pre-0030 column {column!r} must survive"


def test_orm_fields_resolve_and_default_none() -> None:
    """:attr:`AuditLog.raw_payload` + :attr:`AuditLog.redaction_manifest`
    resolve and default to ``None``."""
    from meho_backplane.db.models import AuditLog

    assert hasattr(AuditLog, "raw_payload")
    assert hasattr(AuditLog, "redaction_manifest")
    row = AuditLog(
        operator_sub="op-smoke",
        method="DISPATCH",
        path="some.op",
        status_code=200,
    )
    assert row.raw_payload is None
    assert row.redaction_manifest is None

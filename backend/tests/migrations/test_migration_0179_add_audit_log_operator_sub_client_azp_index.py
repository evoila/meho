# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0179_add_audit_log_operator_sub_client_azp_index``.

* The upgrade adds one index on ``audit_log``: ``(operator_sub, the login
  client of the row's client block, occurred_at)``. No column changes.
* The last-use query uses the index (SQLite query plan).
* The downgrade drops the index again.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, select, text
from sqlalchemy.dialects import sqlite

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.db.models import AUDIT_LOG_CLIENT_AZP, AuditLog
from meho_backplane.settings import get_settings

_REVISION = "0179"
_DOWN_REVISION = "0178"
_INDEX = "audit_log_operator_sub_client_azp_occurred_at_idx"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0179.db"
    async_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_engine_for_testing()

    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", async_url)
    try:
        yield cfg, f"sqlite:///{db_path}"
    finally:
        get_settings.cache_clear()
        reset_engine_for_testing()


def _audit_log(sync_url: str) -> tuple[list[str], dict[str, str]]:
    """Return the columns of ``audit_log`` and its indexes with their SQL.

    Read from ``sqlite_master``: the SQLAlchemy inspector leaves out an
    index on an expression.
    """
    eng = sa_create_engine(sync_url)
    try:
        columns = [col["name"] for col in inspect(eng).get_columns("audit_log")]
        with eng.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'index' AND tbl_name = 'audit_log' AND sql IS NOT NULL"
                )
            )
            indexes = {str(name): str(sql) for name, sql in rows}
        return columns, indexes
    finally:
        eng.dispose()


def test_upgrade_adds_the_person_client_and_time_index(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _DOWN_REVISION)
    columns_before, indexes_before = _audit_log(sync_url)
    assert _INDEX not in indexes_before

    command.upgrade(cfg, _REVISION)
    columns_after, indexes_after = _audit_log(sync_url)

    assert columns_after == columns_before
    assert set(indexes_after) - set(indexes_before) == {_INDEX}
    assert indexes_after[_INDEX].endswith(
        "(operator_sub, ((payload -> 'client') ->> 'azp'), occurred_at)"
    )


def test_last_use_query_uses_the_index(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    """The query of ``last_use_by_client`` has the same expression as the index."""
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    stmt = (
        select(AuditLog.occurred_at)
        .where(
            AuditLog.operator_sub == "person",
            AUDIT_LOG_CLIENT_AZP == "meho-cli",
            AuditLog.occurred_at >= datetime(2026, 1, 1, tzinfo=UTC),
        )
        .order_by(AuditLog.occurred_at.desc())
        .limit(1)
    )
    sql = str(stmt.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            plan = " ".join(str(row[-1]) for row in conn.execute(text(f"EXPLAIN QUERY PLAN {sql}")))
    finally:
        eng.dispose()
    assert f"INDEX {_INDEX} (operator_sub=? AND <expr>=? AND occurred_at>?)" in plan
    assert "TEMP B-TREE" not in plan


def test_downgrade_drops_the_index(
    alembic_cfg: tuple[Config, str],
    copy_migrated_db: Callable[[Config, str], None],
) -> None:
    cfg, sync_url = alembic_cfg
    copy_migrated_db(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    assert _INDEX not in _audit_log(sync_url)[1]

# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0106_add_audit_log_event_class`` (#3733).

The migration adds ``audit_log.event_class``: NOT NULL, server default
``legacy_request``, and a CHECK that allows only the five known classes.
The tests check that:

* a row written before the migration reads as ``legacy_request`` after it,
  even when its ``method`` / ``path`` look like a dispatch (no guessing);
* the database rejects a class that is not in the list;
* ``downgrade`` removes the column and the pair round-trips.

SQLite is the test driver. The migration uses ``batch_alter_table``, so the
same DDL runs on PostgreSQL.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0106"
_DOWN_REVISION = "0105"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0106.db"
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


def _insert_old_row(sync_url: str, *, method: str, path: str) -> str:
    """Insert one audit row with the pre-0106 columns only; return its id."""
    row_id = uuid.uuid4().hex
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, "
                    "status_code, payload) VALUES (:id, '2026-01-01 00:00:00', 'op-old', "
                    ":method, :path, 200, '{}')"
                ),
                {"id": row_id, "method": method, "path": path},
            )
    finally:
        eng.dispose()
    return row_id


def _event_class_of(sync_url: str, row_id: str) -> str:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return str(
                conn.execute(
                    text("SELECT event_class FROM audit_log WHERE id = :id"), {"id": row_id}
                ).scalar_one()
            )
    finally:
        eng.dispose()


def _audit_log_columns(sync_url: str) -> dict[str, dict[str, object]]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return {c["name"]: dict(c) for c in inspect(conn).get_columns("audit_log")}
    finally:
        eng.dispose()


def test_old_rows_read_as_legacy_request_without_guessing(
    alembic_cfg: tuple[Config, str],
) -> None:
    """Rows written before 0106 all read ``legacy_request`` after it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    # One row looks like a dispatch, one like an approval, one like HTTP. The
    # migration must not use method / path to pick a class.
    ids = [
        _insert_old_row(sync_url, method="DISPATCH", path="vault.kv.read"),
        _insert_old_row(sync_url, method="APPROVAL", path="approval.decision"),
        _insert_old_row(sync_url, method="GET", path="/api/v1/healthz"),
    ]

    command.upgrade(cfg, _REVISION)

    assert [_event_class_of(sync_url, row_id) for row_id in ids] == ["legacy_request"] * 3
    assert _audit_log_columns(sync_url)["event_class"]["nullable"] is False


def test_database_rejects_an_unknown_class(alembic_cfg: tuple[Config, str]) -> None:
    """The CHECK constraint refuses a class outside the closed list."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    eng = sa_create_engine(sync_url)
    try:
        with (
            pytest.raises(IntegrityError, match="ck_audit_log_event_class"),
            eng.begin() as conn,
        ):
            conn.execute(
                text(
                    "INSERT INTO audit_log (id, occurred_at, operator_sub, method, path, "
                    "status_code, payload, event_class) VALUES (:id, "
                    "'2026-01-01 00:00:00', 'op', 'DISPATCH', 'x.y', 200, '{}', 'bogus')"
                ),
                {"id": uuid.uuid4().hex},
            )
    finally:
        eng.dispose()


def test_downgrade_drops_the_column_and_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0105`` drops ``event_class``; ``upgrade 0106`` restores it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    row_id = _insert_old_row(sync_url, method="GET", path="/api/v1/targets")

    command.downgrade(cfg, _DOWN_REVISION)
    assert "event_class" not in _audit_log_columns(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _event_class_of(sync_url, row_id) == "legacy_request"

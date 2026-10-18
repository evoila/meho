# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0109_add_convention_history_provenance`` (#3747).

The migration adds two nullable columns to ``tenant_convention_history``:
``change_kind`` (with a CHECK for ``created`` / ``updated`` / ``deleted``)
and ``request_id``. The tests check that:

* a row written before the migration keeps its ``audit_id`` and reads
  ``NULL`` for both new columns (nothing is guessed);
* the database rejects an unknown ``change_kind``;
* ``downgrade`` removes the columns and the pair round-trips.

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

_REVISION = "0109"
_DOWN_REVISION = "0108"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0109.db"
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


def _insert_old_row(sync_url: str, *, audit_id: str) -> str:
    """Insert one history row with the pre-0109 columns only; return its id."""
    row_id = uuid.uuid4().hex
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenant_convention_history (id, convention_id, body_before, "
                    "body_after, actor_sub, ts, audit_id) VALUES (:id, :cid, NULL, 'body', "
                    "'op-old', '2026-01-01 00:00:00', :audit_id)"
                ),
                {"id": row_id, "cid": uuid.uuid4().hex, "audit_id": audit_id},
            )
    finally:
        eng.dispose()
    return row_id


def _row(sync_url: str, row_id: str) -> dict[str, object]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT audit_id, change_kind, request_id FROM tenant_convention_history "
                    "WHERE id = :id"
                ),
                {"id": row_id},
            )
            return dict(result.mappings().one())
    finally:
        eng.dispose()


def _columns(sync_url: str) -> set[str]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return {c["name"] for c in inspect(conn).get_columns("tenant_convention_history")}
    finally:
        eng.dispose()


def test_old_rows_keep_audit_id_and_get_no_guessed_kind(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    audit_id = uuid.uuid4().hex
    row_id = _insert_old_row(sync_url, audit_id=audit_id)

    command.upgrade(cfg, _REVISION)

    row = _row(sync_url, row_id)
    assert str(row["audit_id"]).replace("-", "") == audit_id
    assert row["change_kind"] is None
    assert row["request_id"] is None


def test_database_rejects_an_unknown_change_kind(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    eng = sa_create_engine(sync_url)
    try:
        with (
            pytest.raises(IntegrityError, match="ck_tenant_convention_history_change_kind"),
            eng.begin() as conn,
        ):
            conn.execute(
                text(
                    "INSERT INTO tenant_convention_history (id, convention_id, body_after, "
                    "actor_sub, ts, change_kind) VALUES (:id, :cid, 'body', 'op', "
                    "'2026-01-01 00:00:00', 'renamed')"
                ),
                {"id": uuid.uuid4().hex, "cid": uuid.uuid4().hex},
            )
    finally:
        eng.dispose()


def test_downgrade_drops_the_columns_and_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    row_id = _insert_old_row(sync_url, audit_id=uuid.uuid4().hex)

    command.downgrade(cfg, _DOWN_REVISION)
    assert {"change_kind", "request_id"}.isdisjoint(_columns(sync_url))

    command.upgrade(cfg, _REVISION)
    assert _row(sync_url, row_id)["change_kind"] is None

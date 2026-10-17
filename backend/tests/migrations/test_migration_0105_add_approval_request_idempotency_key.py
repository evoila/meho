# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for Alembic migration ``0105_add_approval_request_idempotency_key``.

#3486 adds one nullable ``idempotency_key`` column to ``approval_request``
and a partial unique index on ``(tenant_id, principal_sub,
idempotency_key)`` for rows that have a key and are ``pending`` or
``approved``. The tests check the column and index after ``upgrade``, that
both are gone after ``downgrade``, and that the index allows a key again
once the earlier row is rejected. SQLite is the test driver; the partial
predicate uses the same ``sqlite_where`` / ``postgresql_where`` pair as
migrations 0072 and 0073.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import inspect

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0105"
_DOWN_REVISION = "0104"
_TABLE = "approval_request"
_COLUMN = "idempotency_key"
_INDEX = "approval_request_idempotency_key_idx"
_TENANT = "00000000-0000-0000-0000-000000003486"


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0105.db"
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


def _schema(sync_url: str) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    """Return ``(columns, indexes)`` of ``approval_request`` keyed by name."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            insp = inspect(conn)
            columns = {c["name"]: dict(c) for c in insp.get_columns(_TABLE)}
            indexes = {str(i["name"]): dict(i) for i in insp.get_indexes(_TABLE)}
    finally:
        sync_eng.dispose()
    return columns, indexes


def _insert(conn: sa.Connection, *, row_id: str, status: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO approval_request (id, tenant_id, principal_sub, op_id, "
            "connector_id, params_hash, proposed_effect, status, created_at, "
            "idempotency_key) VALUES (:id, :tenant, 'addon', 'op', 'c-1', 'h', '{}', "
            ":status, '2026-10-16 00:00:00', 'key-1')"
        ),
        {"id": row_id, "tenant": _TENANT, "status": status},
    )


def test_upgrade_adds_column_and_partial_unique_index(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    columns, indexes = _schema(sync_url)
    assert columns[_COLUMN]["nullable"] is True
    assert indexes[_INDEX]["unique"]
    assert indexes[_INDEX]["column_names"] == ["tenant_id", "principal_sub", _COLUMN]


def test_index_blocks_two_live_rows_but_frees_the_key_after_reject(
    alembic_cfg: tuple[Config, str],
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.begin() as conn:
            _insert(conn, row_id="00000000-0000-0000-0000-00000000a001", status="pending")
        with pytest.raises(sa.exc.IntegrityError), sync_eng.begin() as conn:
            _insert(conn, row_id="00000000-0000-0000-0000-00000000a002", status="approved")
        with sync_eng.begin() as conn:
            conn.execute(sa.text("UPDATE approval_request SET status = 'rejected'"))
            _insert(conn, row_id="00000000-0000-0000-0000-00000000a003", status="pending")
    finally:
        sync_eng.dispose()


def test_downgrade_drops_column_and_index(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)

    columns, indexes = _schema(sync_url)
    assert _COLUMN not in columns
    assert _INDEX not in indexes


def test_upgrade_round_trips_after_clean_downgrade(alembic_cfg: tuple[Config, str]) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    command.downgrade(cfg, _DOWN_REVISION)
    command.upgrade(cfg, _REVISION)

    columns, indexes = _schema(sync_url)
    assert _COLUMN in columns
    assert _INDEX in indexes

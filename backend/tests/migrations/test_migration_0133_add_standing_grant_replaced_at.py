# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``0133_add_standing_grant_replaced_at`` (#5424).

* **Upgrade** adds ``replaced_at`` to both standing-grant tables and builds
  the two unique indexes again with ``replaced_at IS NULL``. A replaced row
  no longer holds its key; two live rows with one key are still refused.
* **Downgrade** marks a replaced row as revoked, so the old indexes can be
  built next to its new grant, and drops the column.

Every step targets this migration's own revision (``0133``) and its
``down_revision`` (``0132``), never ``head``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meho_backplane.db.engine import reset_engine_for_testing
from meho_backplane.db.migrations import alembic_config
from meho_backplane.settings import get_settings

_REVISION = "0133"
_DOWN_REVISION = "0132"
_TABLES = ("service_principal_grant", "sandbox_delete_grant")
_TENANT = "00000000000000000000000000000001"


@pytest.fixture
def alembic_cfg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0133.db"
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


def _fetch(sync_url: str, sql: str) -> list[Any]:
    eng = sa_create_engine(sync_url)
    try:
        with eng.connect() as conn:
            return list(conn.execute(text(sql)).all())
    finally:
        eng.dispose()


def _execute(sync_url: str, sql: str) -> None:
    eng = sa_create_engine(sync_url)
    try:
        with eng.begin() as conn:
            conn.execute(text(sql))
    finally:
        eng.dispose()


def _columns(sync_url: str, table: str) -> set[str]:
    return {str(row[1]) for row in _fetch(sync_url, f"PRAGMA table_info({table})")}


def _index_sql(sync_url: str, table: str) -> dict[str, str]:
    rows = _fetch(
        sync_url,
        f"SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = '{table}' "
        "AND name LIKE 'uq_%'",
    )
    return {str(name): str(sql) for name, sql in rows}


def _insert_grant(
    sync_url: str,
    table: str,
    grant_id: str,
    *,
    target_id: str | None = None,
    expires_at: str | None = None,
    replaced_at: str | None = None,
) -> None:
    columns = (
        "id, tenant_id, principal_sub, op_id, connector_id, target_id, reason, "
        "created_by_sub, created_at, expires_at"
    )
    target_sql = f"'{target_id}'" if target_id else "NULL"
    expires_sql = f"'{expires_at}'" if expires_at else "NULL"
    values = (
        f"'{grant_id}', '{_TENANT}', 'svc:bot', 'op.a', 'conn-1.0', {target_sql}, 'r', "
        f"'op-admin', '2026-11-01 00:00:00', {expires_sql}"
    )
    if replaced_at is not None:
        columns += ", replaced_at"
        values += f", '{replaced_at}'"
    _execute(sync_url, f"INSERT INTO {table} ({columns}) VALUES ({values})")


@pytest.mark.parametrize("table", _TABLES)
def test_upgrade_adds_the_column_and_frees_the_key_of_a_replaced_row(
    alembic_cfg: tuple[Config, str], table: str
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _DOWN_REVISION)
    assert "replaced_at" not in _columns(sync_url, table)

    command.upgrade(cfg, _REVISION)
    assert "replaced_at" in _columns(sync_url, table)
    indexes = _index_sql(sync_url, table)
    assert set(indexes) == {f"uq_{table}_targeted", f"uq_{table}_targetless"}
    for sql in indexes.values():
        assert "revoked_at IS NULL AND replaced_at IS NULL" in sql

    target = "000000000000000000000000000000aa"
    for target_id, prefix in ((None, "1"), (target, "2")):
        _insert_grant(
            sync_url,
            table,
            f"{prefix}0000000000000000000000000000001",
            target_id=target_id,
            expires_at="2026-10-01 00:00:00",
            replaced_at="2026-11-01 00:00:00",
        )
        _insert_grant(
            sync_url, table, f"{prefix}0000000000000000000000000000002", target_id=target_id
        )
        with pytest.raises(IntegrityError):
            _insert_grant(
                sync_url, table, f"{prefix}0000000000000000000000000000003", target_id=target_id
            )


@pytest.mark.parametrize("table", _TABLES)
def test_downgrade_revokes_replaced_rows_and_builds_the_old_indexes(
    alembic_cfg: tuple[Config, str], table: str
) -> None:
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    _insert_grant(
        sync_url,
        table,
        "30000000000000000000000000000001",
        expires_at="2026-10-01 00:00:00",
        replaced_at="2026-11-01 00:00:00",
    )
    _insert_grant(sync_url, table, "30000000000000000000000000000002")

    command.downgrade(cfg, _DOWN_REVISION)

    assert "replaced_at" not in _columns(sync_url, table)
    rows = _fetch(sync_url, f"SELECT id, revoked_at, revoked_reason FROM {table} ORDER BY id")
    assert [(row[1] is not None, row[2]) for row in rows] == [
        (True, "replaced after it ended"),
        (False, None),
    ]
    indexes = _index_sql(sync_url, table)
    assert set(indexes) == {f"uq_{table}_targeted", f"uq_{table}_targetless"}
    for sql in indexes.values():
        assert "revoked_at IS NULL" in sql
        assert "replaced_at" not in sql
    with pytest.raises(IntegrityError):
        _insert_grant(sync_url, table, "30000000000000000000000000000003")

    command.upgrade(cfg, _REVISION)
    assert "replaced_at" in _columns(sync_url, table)

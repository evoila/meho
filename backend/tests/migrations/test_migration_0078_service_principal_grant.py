# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0078_create_service_principal_grant``.

#3151 / #3152. Creates the ``service_principal_grant`` table — the durable
store of operator-issued standing scoped auto-approval grants for service
principals — plus its lookup index and two **partial** unique indexes
(targeted / targetless, both scoped to ``revoked_at IS NULL``).

Idempotency pinning: every forward / round-trip step targets this
migration's own revision (``0078``) and its ``down_revision`` (``0077``),
never ``head`` — so a future head migration cannot make ``upgrade("head")``
re-run this ``create_table`` on a schema that already has it. SQLite is the
test driver and the migration uses only generic DDL (plus SQLite/PG partial
indexes), so PG parity holds.
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

_REVISION = "0078"
_DOWN_REVISION = "0077"
_TABLE = "service_principal_grant"
_EXPECTED_COLUMNS = {
    "id",
    "tenant_id",
    "principal_sub",
    "op_id",
    "connector_id",
    "target_id",
    "reason",
    "created_by_sub",
    "created_at",
    "expires_at",
    "revoked_at",
    "revoked_by_sub",
}
_EXPECTED_INDEXES = {
    "service_principal_grant_lookup_idx",
    "uq_service_principal_grant_targeted",
    "uq_service_principal_grant_targetless",
    "service_principal_grant_expires_at_idx",
}


@pytest.fixture
def alembic_cfg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[tuple[Config, str]]:
    """Pin env, reset caches, return an Alembic config + sync URL."""
    db_path = tmp_path / "migration_0078.db"
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


def _table_info(sync_url: str, table: str) -> list[tuple[str, bool]]:
    """Return ``(column_name, is_not_null)`` pairs for *table*."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    return [(str(row[1]), int(row[3]) == 1) for row in rows]


def _columns(sync_url: str, table: str) -> set[str]:
    return {name for name, _ in _table_info(sync_url, table)}


def _not_null(sync_url: str, table: str, column: str) -> bool:
    for name, notnull in _table_info(sync_url, table):
        if name == column:
            return notnull
    raise AssertionError(f"column {column!r} not present on {table}")


def _index_meta(sync_url: str, table: str) -> list[tuple[str, bool, bool]]:
    """Return ``(name, is_unique, is_partial)`` for every index on *table*."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    # PRAGMA index_list columns: (seq, name, unique, origin, partial).
    return [(str(row[1]), int(row[2]) == 1, int(row[4]) == 1) for row in rows]


def _index_names(sync_url: str, table: str) -> set[str]:
    return {name for name, _, _ in _index_meta(sync_url, table)}


def _table_names(sync_url: str) -> set[str]:
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def test_upgrade_creates_table_with_full_column_set(alembic_cfg: tuple[Config, str]) -> None:
    """``upgrade 0078`` creates the table with every column at the right nullability."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    assert _columns(sync_url, _TABLE) == _EXPECTED_COLUMNS
    for column in (
        "id",
        "tenant_id",
        "principal_sub",
        "op_id",
        "connector_id",
        "reason",
        "created_by_sub",
        "created_at",
    ):
        assert _not_null(sync_url, _TABLE, column), f"{column} must be NOT NULL"
    for column in ("target_id", "expires_at", "revoked_at", "revoked_by_sub"):
        assert not _not_null(sync_url, _TABLE, column), f"{column} must be nullable"


def test_upgrade_creates_partial_unique_indexes(alembic_cfg: tuple[Config, str]) -> None:
    """Lookup index + two partial unique indexes are created with the right flags."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)

    meta = {name: (unique, partial) for name, unique, partial in _index_meta(sync_url, _TABLE)}
    assert set(meta) >= _EXPECTED_INDEXES
    # Both uniqueness indexes must be UNIQUE and PARTIAL (revoked_at IS NULL scope).
    for name in ("uq_service_principal_grant_targeted", "uq_service_principal_grant_targetless"):
        unique, partial = meta[name]
        assert unique, f"{name} must be UNIQUE"
        assert partial, f"{name} must be a PARTIAL index"


def test_downgrade_then_upgrade_round_trips(alembic_cfg: tuple[Config, str]) -> None:
    """``downgrade 0077`` drops the table; ``upgrade 0078`` recreates it identically."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, _REVISION)
    assert _TABLE in _table_names(sync_url)

    command.downgrade(cfg, _DOWN_REVISION)
    assert _TABLE not in _table_names(sync_url)

    command.upgrade(cfg, _REVISION)
    assert _columns(sync_url, _TABLE) == _EXPECTED_COLUMNS
    assert _index_names(sync_url, _TABLE) >= _EXPECTED_INDEXES

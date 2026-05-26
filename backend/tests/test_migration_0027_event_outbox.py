# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for Alembic migration ``0027_create_event_outbox``.

Initiative #804 (G11.3 Scheduler P2), Task #824 (T3). The migration
adds the ``event_outbox`` table -- the durable substrate for the
event-subscription trigger (an agent run reaching a terminal state,
audit predicates, connector alerts). The transactional outbox is the
replica-safe alternative to plain ``LISTEN/NOTIFY`` (which loses
notifications when no listener is connected).

Test matrix
-----------

* **Upgrade creates the table + columns + indexes.** ``upgrade 0027``
  from a clean DB leaves ``event_outbox`` present with every documented
  column and its two named indexes.
* **Column nullability.** The NOT NULL columns (``tenant_id`` /
  ``event_kind`` / ``payload`` / ``created_at``) reject NULL; the
  drain-managed columns (``claimed_at`` / ``claimed_by`` /
  ``processed_at``) permit NULL.
* **Reversibility round-trip.** ``downgrade "0026"`` drops the table;
  a subsequent ``upgrade 0027`` re-creates it.

Mirrors the structure of
:mod:`tests.test_migration_0020_scheduled_trigger`: synchronous test
functions, SQLite test driver, head-revision target pinned to
``"0027"`` so the column matrix stays the 0027 snapshot regardless of
how many later migrations land.
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
    db_path = tmp_path / "migration_0027.db"
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


def _table_names(sync_url: str) -> set[str]:
    """Return the set of table names in the SQLite DB."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'")).all()
    finally:
        sync_eng.dispose()
    return {str(row[0]) for row in rows}


def _table_columns(sync_url: str, table: str) -> set[str]:
    """Return the set of column names on *table* via ``PRAGMA``."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


def _column_is_nullable(sync_url: str, table: str, column: str) -> bool:
    """Return True when *column* on *table* permits NULL."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})")).all()
    finally:
        sync_eng.dispose()
    for row in rows:
        if str(row[1]) == column:
            return int(row[3]) == 0
    raise AssertionError(f"column {column!r} not present on {table}")


def _table_indexes(sync_url: str, table: str) -> set[str]:
    """Return the set of index names declared on *table*."""
    sync_eng = sa_create_engine(sync_url)
    try:
        with sync_eng.connect() as conn:
            rows = conn.execute(text(f"PRAGMA index_list({table})")).all()
    finally:
        sync_eng.dispose()
    return {str(row[1]) for row in rows}


_EXPECTED_COLUMNS: frozenset[str] = frozenset(
    {
        "event_id",
        "tenant_id",
        "event_kind",
        "payload",
        "claimed_at",
        "claimed_by",
        "processed_at",
        "created_at",
    }
)

_EXPECTED_INDEXES: frozenset[str] = frozenset(
    {
        "event_outbox_tenant_unprocessed_idx",
        "event_outbox_unprocessed_idx",
    }
)


def test_upgrade_creates_event_outbox_table_columns_indexes(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``upgrade 0027`` lands ``event_outbox`` with the documented schema."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0027")

    assert "event_outbox" in _table_names(sync_url), (
        "migration 0027 must create the event_outbox table on upgrade"
    )

    columns = _table_columns(sync_url, "event_outbox")
    assert columns == _EXPECTED_COLUMNS, (
        f"event_outbox columns drifted from the documented set: "
        f"missing={_EXPECTED_COLUMNS - columns}, extra={columns - _EXPECTED_COLUMNS}"
    )

    indexes = _table_indexes(sync_url, "event_outbox")
    for expected in _EXPECTED_INDEXES:
        assert expected in indexes, f"migration 0027 must create index {expected!r}"


def test_column_nullability(alembic_cfg: tuple[Config, str]) -> None:
    """NOT NULL columns reject NULL; drain-managed columns permit it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0027")

    not_null = (
        "tenant_id",
        "event_kind",
        "payload",
        "created_at",
    )
    for col in not_null:
        assert not _column_is_nullable(sync_url, "event_outbox", col), f"{col} must be NOT NULL"

    nullable = (
        "claimed_at",
        "claimed_by",
        "processed_at",
    )
    for col in nullable:
        assert _column_is_nullable(sync_url, "event_outbox", col), f"{col} must be nullable"


def test_downgrade_then_upgrade_round_trips(
    alembic_cfg: tuple[Config, str],
) -> None:
    """``downgrade "0026"`` drops the table; ``upgrade 0027`` restores it."""
    cfg, sync_url = alembic_cfg
    command.upgrade(cfg, "0027")
    assert "event_outbox" in _table_names(sync_url)

    command.downgrade(cfg, "0026")
    assert "event_outbox" not in _table_names(sync_url), (
        "downgrade must drop the event_outbox table"
    )

    command.upgrade(cfg, "0027")
    assert "event_outbox" in _table_names(sync_url), (
        "re-upgrade must restore the event_outbox table"
    )
    assert _table_columns(sync_url, "event_outbox") == _EXPECTED_COLUMNS, (
        "post-round-trip columns must match the 0027 snapshot"
    )
